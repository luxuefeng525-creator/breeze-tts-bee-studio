"""C2 剧本风格模板库：加载回退、建议镜头数、prompt 注入（纯 CPU）。"""
from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api import style_templates as st
from api.router import build_script_prompts


def test_file_templates_have_required_fields():
    tpls = st.load_templates()
    assert len(tpls) >= 6
    ids = [t["id"] for t in tpls]
    assert len(ids) == len(set(ids)), "风格 id 不能重复"
    for t in tpls:
        assert t["name"], t
        assert t["prompt_hint"], t
        assert t["shot_seconds"] > 0, t


def test_missing_file_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_FILE", tmp_path / "nope.json")
    assert st.load_templates() == st._builtin()


def test_broken_file_falls_back(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(st, "_FILE", bad)
    assert st.load_templates() == st._builtin()


def test_unknown_template_returns_default():
    assert st.get_template("no-such")["id"] == st.DEFAULT_STYLE_ID
    assert st.get_template(None)["id"] == st.DEFAULT_STYLE_ID
    assert st.get_template("")["id"] == st.DEFAULT_STYLE_ID
    assert st.get_template("cyberpunk")["id"] == "cyberpunk"


def test_suggest_shot_count_table():
    assert st.suggest_shot_count(15, 6) == 3
    assert st.suggest_shot_count(30, 6) == 5
    assert st.suggest_shot_count(60, 6) == 10
    assert st.suggest_shot_count(90, 6) == 15


def test_suggest_shot_count_invalid_keeps_none():
    assert st.suggest_shot_count(0, 6) is None
    assert st.suggest_shot_count(-5, 6) is None
    assert st.suggest_shot_count("abc", 6) is None
    assert st.suggest_shot_count(30, 0) is None
    assert st.suggest_shot_count(None, None) is None


def test_style_prompt_injected_into_system():
    tpl = st.get_template("cyberpunk")
    system, user = build_script_prompts("雨夜决定", 5, "都市青年", tpl)
    assert tpl["prompt_hint"][:20] in system
    assert tpl["name"] in user
    assert tpl["description"] in user


def test_unknown_style_still_builds_prompt():
    tpl = st.get_template("nope")
    system, user = build_script_prompts("主题", 4, "", tpl)
    assert tpl["prompt_hint"][:20] in system
    assert "主题" in user


# ------------------------------------------------------------------
# 出站链路取证：风格约束必须真的出现在发往 LLM 的请求体里
# 做法：本地起一个假 OpenAI 端点，把 longcat 的 endpoint 指过去；
#       走完整 POST /api/llm/script-gen（真 JSON 解析 + schema 兜底 + SQLite 落库）。
# 为什么需要：单测只证明「构造函数拼对了」，证明不了「路由真的把它发出去了」。
# ------------------------------------------------------------------

class _FakeLLM:
    """冒充 OpenAI chat/completions 的最小服务端，记录收到的 messages。"""

    def __init__(self, reply_shots: list[dict]):
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(n).decode("utf-8")))
                body = json.dumps({
                    "choices": [{"message": {"content": json.dumps(
                        {"title": "风格取证剧本", "genre": "", "shots": reply_shots},
                        ensure_ascii=False)}}]
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # 静音
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()


class _FakeH:
    """router._h_json 只需要这四个方法。"""

    def __init__(self):
        self.wfile = io.BytesIO()
        self.code = 0

    def send_response(self, code, *a):
        self.code = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass

    def json(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_style_constraint_reaches_llm_request(monkeypatch, tmp_path):
    from api import llm_client, router

    shots = [
        {"scene_no": 1, "title": "镜头1", "char": "主角", "emotion": "紧张",
         "duration_sec": 5, "prompt": "霓虹雨巷", "voice_prompt": "台词一"},
        {"scene_no": 2, "title": "镜头2", "char": "主角", "emotion": "爆发",
         "duration_sec": 5, "prompt": "天台对决", "voice_prompt": "台词二"},
    ]
    with _FakeLLM(shots) as fake:
        monkeypatch.setitem(llm_client.LLM_PROVIDERS["longcat"]["endpoints"],
                            "openai", f"http://127.0.0.1:{fake.port}")
        monkeypatch.setattr(router, "_DB_PATH", tmp_path / "probe.db")

        h = _FakeH()
        body = json.dumps({
            "topic": "赛博都市", "shots_count": 2, "audience": "青年男性",
            "style_id": "cyberpunk", "provider": "longcat",
            "api_key": "probe-key", "project_id": "proj-style-probe-1",
        }).encode("utf-8")
        assert router.handle_post(h, "/api/llm/script-gen", body, "application/json") is True

        resp = h.json()
        assert h.code == 200, resp
        assert resp["used_mock"] is False, resp
        assert resp["style_id"] == "cyberpunk"
        assert [s["style_id"] for s in resp["shots"]] == ["cyberpunk", "cyberpunk"]
        assert resp["shots"][0]["prompt"] == "霓虹雨巷"

        # 出站请求体：system 带风格约束，user 带风格名
        assert len(fake.requests) == 1
        msgs = fake.requests[0]["messages"]
        system = next(m["content"] for m in msgs if m["role"] == "system")
        user = next(m["content"] for m in msgs if m["role"] == "user")
        hint = st.get_template("cyberpunk")["prompt_hint"]
        assert hint[:20] in system, system
        assert st.get_template("cyberpunk")["name"] in user

    # 落库取证：临时 DB 里确实写了带 style_id 的剧本；tmp_path 自动清理，零残留
    row = router._script_read("proj-style-probe-1")
    assert row is not None
    assert [s["style_id"] for s in row["shots"]] == ["cyberpunk", "cyberpunk"]
    assert row["prompt"] == "赛博都市"
