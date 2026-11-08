# coding: utf-8
"""Qwen3-TTS 适配层契约测试（2026-11-07 新增）。

替换引擎后，旧 tests/ 里 86 条引擎相关用例 import 的仍是 Breeze 代码，
会「全绿但测不到实际服务」。本文件直接针对 qwen_adapter 的路由、字段、
响应头与降级语义做确定性断言。

设计约束：
* 不加载任何模型 —— 用 FakeModel / 覆盖 ensure() 的 Engine 桩顶替 GPU 推理；
* 不访问真实 7860 —— 用 FastAPI TestClient 在进程内跑，避免与
  test_panel.py 的真实服务并发撞 409；
* 保留 Engine.synthesize 的真实决策逻辑，只把 ensure() 换成桩。
"""
from __future__ import annotations

import importlib.util
import io
import sys
import wave
from pathlib import Path
from urllib.parse import unquote

import numpy as np
import pytest
from fastapi.testclient import TestClient

import panel as P

_ADAPTER = Path(__file__).resolve().parents[2] / "qwen-adapter" / "qwen_adapter.py"


def _wav_bytes(frames: int = 2400) -> bytes:
    """最小合法 24kHz 单声道 WAV，供 soundfile 解码。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


_REF_WAV = _wav_bytes()


@pytest.fixture(scope="module")
def qa():
    assert _ADAPTER.is_file(), f"adapter not found: {_ADAPTER}"
    spec = importlib.util.spec_from_file_location("qwen_adapter_contract", _ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeModel:
    """记录调用参数，返回可解码的伪 PCM；batch 按文本条数返回对应段数。"""

    def __init__(self):
        self.calls = []

    @staticmethod
    def _count(kw):
        t = kw.get("text")
        return len(t) if isinstance(t, list) else 1

    def _wavs(self, kw, name):
        self.calls.append((name, kw))
        return [np.zeros(2400, dtype=np.float32)
                for _ in range(self._count(kw))], 24000

    def create_voice_clone_prompt(self, ref_audio=None, ref_text=None,
                                  x_vector_only_mode=False):
        self.calls.append(("clone_prompt", {
            "ref_text": ref_text, "x_vector_only_mode": x_vector_only_mode,
        }))
        return {"fake": "prompt"}

    def generate_voice_clone(self, **kw):
        return self._wavs(kw, "voice_clone")

    def generate_voice_design(self, **kw):
        return self._wavs(kw, "voice_design")

    def generate_custom_voice(self, **kw):
        return self._wavs(kw, "custom_voice")


def make_fake_engine(qa):
    """继承真 Engine，只覆盖 ensure()：路由/降级/缓存决策全部走真实代码。"""
    class _FakeEngine(qa.Engine):
        def ensure(self, route):
            # 不碰 GPU、不切换显存，仅记录目标路由
            self.current = route
            self.ready = True
            self.error = None
            if getattr(self, "_fake_model", None) is None:
                self._fake_model = FakeModel()
            self.model = self._fake_model
            return self.model

    eng = _FakeEngine(Path("."), Path("."), Path("."), "auto", "cpu")
    eng.current = "design"
    eng.ready = True
    eng._fake_model = FakeModel()
    eng.model = eng._fake_model
    return eng


@pytest.fixture
def client(qa, monkeypatch):
    fake = make_fake_engine(qa)
    monkeypatch.setattr(qa, "engine", fake, raising=False)
    # 不用 with TestClient(...)，避免触发 lifespan 的 warmup 线程
    return TestClient(qa.app), fake


def _speech(client, **fields):
    data = {"text": "今天天气不错", "cfg_scale": "1", "seed": "42"}
    data.update({k: str(v) for k, v in fields.items()})
    return client.post("/v1/audio/speech", data=data)


# ---------- 1. 契约骨架 ----------

def test_routes_and_model_info(qa, client):
    c, _ = client
    paths = {r.path for r in qa.app.routes if r.path.startswith("/")}
    assert {"/health", "/v1/model", "/v1/audio/speech",
            "/v1/audio/speech/batch"} <= paths
    h = c.get("/health")
    assert h.status_code == 200 and h.json()["sample_rate"] == 24000
    m = c.get("/v1/model").json()
    assert m["engine"] == "qwen3-tts"
    assert m["default_language"] == "Auto"
    assert m["sample_rate"] == 24000
    assert "ready" in m and "error" in m
    assert "chinese" in m["supported_languages"] and "auto" in m["supported_languages"]
    assert "dylan" in m["preset_speakers"] and "eric" in m["preset_speakers"]


def test_new_form_fields_exist(qa):
    import inspect
    for fn in (qa.speech, qa.speech_batch):
        params = inspect.signature(fn).parameters
        assert {"language", "speaker", "instruct_policy"} <= set(params), fn.__name__


# ---------- 2. P0-2 语种 ----------

def test_language_defaults_to_auto(client):
    c, fake = client
    r = _speech(c)
    assert r.status_code == 200, r.text[:200]
    call = [x for x in fake.model.calls if x[0] == "voice_design"][-1][1]
    assert call["language"] == "Auto", "默认必须用官方 Auto，不能启发式硬猜"


def test_explicit_language_is_forwarded(client):
    c, fake = client
    r = _speech(c, language="German", text="Guten Tag, wie geht es Ihnen?")
    assert r.status_code == 200, r.text[:200]
    call = [x for x in fake.model.calls if x[0] == "voice_design"][-1][1]
    assert call["language"] == "German", "德语不得再被误判成 English"


def test_unknown_language_degrades_to_auto(client):
    c, fake = client
    r = _speech(c, language="Klingon")
    assert r.status_code == 200, r.text[:200]
    call = [x for x in fake.model.calls if x[0] == "voice_design"][-1][1]
    assert call["language"] == "Auto"


def test_detect_language_heuristic_removed_from_path(qa):
    """启发式函数若还被调用，德/法/意会静默变 English。"""
    assert qa._normalize_language("German") == "German"
    assert qa._normalize_language("") == "Auto"
    assert qa._normalize_language("Auto") == "Auto"


# ---------- 3. P0-3 cfg 与占位符 ----------

def test_cfg_scale_affects_every_route(qa):
    clone_lo = qa._gen_kwargs(1.0, "clone", 2048)["temperature"]
    clone_hi = qa._gen_kwargs(8.0, "clone", 2048)["temperature"]
    assert clone_lo == 0.9, "cfg=1 应保持官方基线，不回归现有克隆音质"
    assert clone_hi > clone_lo, "克隆路由不得再忽略 cfg_scale"
    design_lo = qa._gen_kwargs(1.0, "design", 2048)["temperature"]
    design_hi = qa._gen_kwargs(8.0, "design", 2048)["temperature"]
    assert design_hi > design_lo
    assert qa._gen_kwargs(8.0, "clone", 99999)["max_new_tokens"] == qa.MAX_NEW_TOKENS


def test_placeholder_set_covers_panel(qa):
    """适配层必须是 panel.py 占位符集合的超集，否则 x_vector_only_mode 漏触发。"""
    panel_set = {str(x).lower() for x in P._PLACEHOLDER_REF_TEXT}
    adapter_set = {str(x).lower() for x in qa._PLACEHOLDER_REF_TEXT}
    missing = panel_set - adapter_set
    assert not missing, f"适配层漏了面板占位符：{sorted(missing)}"


def test_placeholder_triggers_x_vector_only(client):
    c, fake = client
    r = c.post("/v1/audio/speech", data={
        "text": "测试", "ref_text": "参考文字稿", "cfg_scale": "1",
    }, files={"ref_audio": ("ref.wav", _REF_WAV, "audio/wav")})
    assert r.status_code == 200, r.text[:300]
    cp = [x for x in fake.model.calls if x[0] == "clone_prompt"][-1][1]
    assert cp["x_vector_only_mode"] is True, "面板占位符文稿应触发纯声纹模式"


def test_placeholder_variant_prefix_also_triggers(client):
    """与 panel._is_placeholder_ref_text 同口径：「参考文字稿XX」这类
    前缀变体也必须触发，否则两侧判定漂移、克隆相似度悄悄下降。"""
    c, fake = client
    r = c.post("/v1/audio/speech", data={
        "text": "测试", "ref_text": "参考文字稿（待补全）", "cfg_scale": "1",
    }, files={"ref_audio": ("ref.wav", _REF_WAV, "audio/wav")})
    assert r.status_code == 200, r.text[:300]
    cp = [x for x in fake.model.calls if x[0] == "clone_prompt"][-1][1]
    assert cp["x_vector_only_mode"] is True


def test_speaker_is_normalized_to_config_form(client):
    """用户传 UNCLE_FU / Uncle_Fu 都应规范化为 config spk_id 原形。"""
    c, fake = client
    r = _speech(c, speaker="UNCLE_FU")
    assert r.status_code == 200, r.text[:300]
    call = [x for x in fake.model.calls if x[0] == "custom_voice"][-1][1]
    assert call["speaker"] == "uncle_fu"


def test_max_new_tokens_matches_official_ceiling(qa):
    """官方 generate 上限 8192；2048 会把长句静默截断（审计 P1 项）。"""
    assert qa.MAX_NEW_TOKENS == 8192


# ---------- 4. P0-1 克隆与指令冲突：不再静默 ----------

def test_clone_with_instruction_reports_degradation(client):
    c, fake = client
    r = c.post("/v1/audio/speech", data={
        "text": "测试", "instruction": "语气温柔亲切", "ref_text": "你要相信，你会越来越好",
        "cfg_scale": "4", "instruct_policy": "voice",
    }, files={"ref_audio": ("ref.wav", _REF_WAV, "audio/wav")})
    assert r.status_code == 200, r.text[:300]
    note = unquote(r.headers.get("X-Adapter-Note", ""))
    assert "克隆音色不支持自然语言指令" in note, note
    assert r.headers.get("X-Adapter-Route") == "clone"
    assert [x[0] for x in fake.model.calls][-1] == "voice_clone"


def test_instruct_policy_switches_to_preset_speaker(client):
    c, fake = client
    r = c.post("/v1/audio/speech", data={
        "text": "测试", "instruction": "语气温柔亲切", "ref_text": "你要相信，你会越来越好",
        "cfg_scale": "4", "instruct_policy": "instruct",
    }, files={"ref_audio": ("ref.wav", _REF_WAV, "audio/wav")})
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get("X-Adapter-Route") == "custom"
    assert "放弃克隆音色" in unquote(r.headers.get("X-Adapter-Note", ""))
    call = [x for x in fake.model.calls if x[0] == "custom_voice"][-1][1]
    assert call["instruct"] == "语气温柔亲切", "指令必须真正下发"
    assert call["speaker"] == "serena", "应改用默认预设音色（config spk_id 原形）"


def test_invalid_instruct_policy_is_400(client):
    c, _ = client
    r = _speech(c, instruct_policy="magic")
    assert r.status_code == 400


def test_unknown_speaker_is_400(client):
    c, _ = client
    r = _speech(c, speaker="Gandalf")
    assert r.status_code == 400


# ---------- 5. P0-4 原生方言 speaker ----------

@pytest.mark.parametrize("instruction,speaker", [
    ("用浓重的北京话说话，明显的儿化音和松弛京腔", "dylan"),
    ("用浓重的四川方言说话，明显的川渝语调和韵味", "eric"),
])
def test_dialect_maps_to_native_speaker(client, instruction, speaker):
    c, fake = client
    r = _speech(c, instruction=instruction)
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get("X-Adapter-Route") == "custom"
    call = [x for x in fake.model.calls if x[0] == "custom_voice"][-1][1]
    assert call["speaker"] == speaker
    assert call["instruct"] == instruction, "方言指令仍要作为 instruct 下发"


def test_non_native_dialect_stays_on_voice_design(client):
    c, _ = client
    r = _speech(c, instruction="用浓重的粤语说话，地道广东话")
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get("X-Adapter-Route") == "design", "粤语无原生 speaker，不应假装原生"


def test_explicit_speaker_wins(client):
    c, fake = client
    r = _speech(c, speaker="Vivian", instruction="用浓重的四川方言说话")
    assert r.status_code == 200, r.text[:300]
    call = [x for x in fake.model.calls if x[0] == "custom_voice"][-1][1]
    # speaker 入参大小写不敏感，统一归一到 config 里的原形（见
    # test_speaker_is_normalized_to_config_form）；这里只验「显式指定优先于
    # 方言推断」，不验大小写保留。
    assert call["speaker"] == "vivian"


# ---------- 6. 错误码 / 并发 / batch ----------

def test_ref_pair_validation(client):
    c, _ = client
    r = c.post("/v1/audio/speech", data={"text": "测试", "ref_text": "有文稿无音频"})
    assert r.status_code == 400
    assert "ref_audio and ref_text" in r.text


def test_busy_lock_returns_409(client):
    c, fake = client
    assert fake.lock.acquire(blocking=False)
    try:
        r = _speech(c)
        assert r.status_code == 409
    finally:
        fake.lock.release()


def test_batch_headers_and_language(client):
    c, fake = client
    r = c.post("/v1/audio/speech/batch", data={
        "texts": '["第一句", "第二句"]', "language": "Chinese", "cfg_scale": "1",
    })
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get("X-Sample-Rate") == "24000"
    assert len(r.headers.get("X-Segment-Bytes", "").split(",")) == 2
    assert r.headers.get("X-Adapter-Route") == "design"
    call = [x for x in fake.model.calls if x[0] == "voice_design"][-1][1]
    assert call["language"] == ["Chinese", "Chinese"]


def test_batch_texts_validation(client):
    c, _ = client
    assert c.post("/v1/audio/speech/batch", data={"texts": "[]"}).status_code == 400
    assert c.post("/v1/audio/speech/batch", data={"texts": "not json"}).status_code == 400


def test_health_reports_error_state(qa, client, monkeypatch):
    c, fake = client
    fake.error = "CUDA OOM"
    r = c.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "error"
