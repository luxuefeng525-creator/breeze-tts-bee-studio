"""REQ-20261127-02：自定义 LLM 模型链路（base_url 生效 + 自定义 id 可作 provider）。

取证方式：本地起假 OpenAI 兼容端点抓出站请求体，走完整 script-gen 路由；
内置 provider 行为必须与改动前一致（回归保护）。
"""
from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from api import router


CUSTOM_ID = "custom-probe-llm"


class _FakeLLM:
    """冒充 OpenAI chat/completions，记录请求体，可按 status 返回错误。"""

    def __init__(self, *, status: int = 200, shots: list[dict] | None = None):
        self.requests: list[dict] = []
        self.status = status
        self.shots = shots if shots is not None else [
            {"scene_no": 1, "title": "镜头1", "char": "主角", "emotion": "紧张",
             "duration_sec": 5, "prompt": "霓虹雨巷", "voice_prompt": "台词一"},
        ]
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(n).decode("utf-8")))
                if outer.status != 200:
                    body = json.dumps({"error": {"message": "invalid key"}}).encode()
                else:
                    body = json.dumps({
                        "choices": [{"message": {"content": json.dumps(
                            {"title": "自定义模型剧本", "genre": "", "shots": outer.shots},
                            ensure_ascii=False)}}]
                    }).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
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

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"   # 故意带 /v1，验证不重复拼路径


class _FakeH:
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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把注册表与 DB 都指到临时目录，避免污染真实数据。"""
    reg = tmp_path / "model_registry.json"
    reg.write_text(json.dumps([{
        "id": CUSTOM_ID, "name": "探针模型", "type": "llm",
        "provider": "custom", "model_name": "glm-probe-1",
        "base_url": "http://placeholder.local/v1", "builtin": False,
    }], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(router, "_MODEL_REGISTRY_FILE", reg)
    monkeypatch.setattr(router, "_DB_PATH", tmp_path / "probe.db")
    # _db_ready 是进程级标志：换 DB 路径后必须重置，否则不会在新库上建表
    monkeypatch.setattr(router, "_db_ready", False)
    monkeypatch.setattr(router, "_API_KEYS", {})
    return reg


def _post(h, **over):
    body = {
        "topic": "赛博都市", "shots_count": 1, "audience": "",
        "provider": CUSTOM_ID, "api_key": "probe-key-abcdefgh",
        "project_id": "proj-custom-probe",
    }
    body.update(over)
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    assert router.handle_post(h, "/api/llm/script-gen", raw, "application/json") is True
    return h.json()


def test_base_url_and_model_are_used(env):
    with _FakeLLM() as fake:
        # 把注册表的 base_url 换成假端点
        reg = json.loads(env.read_text(encoding="utf-8"))
        reg[0]["base_url"] = fake.base_url
        env.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")

        resp = _post(_FakeH())
        assert resp["used_mock"] is False, resp
        assert resp["generated_by"] == CUSTOM_ID
        assert resp["style_id"]

        assert len(fake.requests) == 1
        req = fake.requests[0]
        assert req["model"] == "glm-probe-1"
        assert req["messages"][-1]["role"] == "user"
        assert "赛博都市" in req["messages"][-1]["content"]


def test_missing_key_returns_402_without_calling(env):
    with _FakeLLM() as fake:
        h = _FakeH()
        resp = _post(h, api_key="")
        assert h.code == 402, resp
        assert resp.get("need_key") is True
        assert fake.requests == []      # 没 Key 就不许发请求


def test_call_failure_keeps_mock_with_reason(env):
    with _FakeLLM(status=401) as fake:
        reg = json.loads(env.read_text(encoding="utf-8"))
        reg[0]["base_url"] = fake.base_url
        env.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")

        resp = _post(_FakeH())
        assert resp["used_mock"] is True, resp
        assert resp["custom_model_error"] is True
        assert "401" in resp["mock_reason"]


def test_invalid_or_empty_base_url_rejected(env):
    for bad in ("", "ftp://nope", "not-a-url"):
        reg = json.loads(env.read_text(encoding="utf-8"))
        reg[0]["base_url"] = bad
        env.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        resp = _post(_FakeH())
        assert resp.get("error"), resp


def test_unknown_provider_rejected(env):
    resp = _post(_FakeH(), provider="no-such-model")
    assert resp.get("error")


def test_builtin_provider_not_affected(env, tmp_path, monkeypatch):
    # 注册表指向不存在的路径也不该影响内置 provider 的解析
    monkeypatch.setattr(router, "_MODEL_REGISTRY_FILE", tmp_path / "absent.json")
    assert router._resolve_llm("longcat") == ("", "")
    assert router._resolve_llm("openai") == ("", "")


def test_script_persisted_with_custom_provider(env):
    with _FakeLLM() as fake:
        reg = json.loads(env.read_text(encoding="utf-8"))
        reg[0]["base_url"] = fake.base_url
        env.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        _post(_FakeH())
        row = router._script_read("proj-custom-probe")
        assert row is not None
        assert row["shots"][0]["prompt"] == "霓虹雨巷"
