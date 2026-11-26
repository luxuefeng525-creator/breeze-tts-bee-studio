# coding: utf-8
"""ComfyUI provider 的「任务丢失」判定。

背景（2026-11-26）：ComfyUI 的任务队列只存在内存里，守护脚本自愈重启后，
之前提交的 prompt 会彻底消失 —— /history 永远为空、/queue 里也没有。
旧实现遇到这种情况会一直报 running，前端傻等满 30 分钟才超时，
用户看到的就是「轮询中 #332 · running」这种假活着。

这里用假 response 覆盖三条分支，不连真实 8188。
"""
from __future__ import annotations

import time

import pytest

from api.providers import GenRequest, GenResult, ProviderError
from api.providers.comfyui import ComfyUIProvider


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _provider():
    return ComfyUIProvider(base_url="http://127.0.0.1:8188")


def _patch_get(monkeypatch, table):
    """按 URL 后缀分发假响应；值为异常实例时直接抛。"""
    def fake_get(url, *a, **kw):
        for suffix, payload in table.items():
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return FakeResp(payload)
        raise AssertionError("unexpected url: %s" % url)
    monkeypatch.setattr("api.providers.comfyui.requests.get", fake_get)


def test_queue_has_detects_running_and_pending(monkeypatch):
    p = _provider()
    _patch_get(monkeypatch, {"/queue": {
        "queue_running": [[1, "job-a", {}, {}, []]],
        "queue_pending": [[2, "job-b", {}, {}, []]],
    }})
    assert p._queue_has("job-a") is True
    assert p._queue_has("job-b") is True
    assert p._queue_has("job-c") is False


def test_queue_has_returns_none_when_unreachable(monkeypatch):
    import requests
    p = _provider()
    _patch_get(monkeypatch, {"/queue": requests.ConnectionError("refused")})
    assert p._queue_has("job-a") is None


def test_lost_needs_grace_period(monkeypatch):
    p = _provider()
    _patch_get(monkeypatch, {"/queue": {"queue_running": [], "queue_pending": []}})
    fresh = GenResult(job_id="job-a", status="running")
    assert p._lost(fresh, "job-a") is False, "刚提交 90 秒内不许判丢"
    old = GenResult(job_id="job-a", status="running")
    old._bee_first_poll = time.time() - 120
    assert p._lost(old, "job-a") is True


def test_lost_false_while_still_queued(monkeypatch):
    p = _provider()
    _patch_get(monkeypatch, {"/queue": {
        "queue_running": [[1, "job-a", {}, {}, []]], "queue_pending": [],
    }})
    old = GenResult(job_id="job-a", status="running")
    old._bee_first_poll = time.time() - 300
    assert p._lost(old, "job-a") is False


def test_poll_reports_lost_job_as_failed(monkeypatch):
    p = _provider()
    _patch_get(monkeypatch, {
        "/history/job-a": {},
        "/queue": {"queue_running": [], "queue_pending": []},
    })
    old = GenResult(job_id="job-a", status="running")
    old._bee_first_poll = time.time() - 300
    r = p.poll(old, "job-a")
    assert r.status == "failed"
    assert "丢失" in r.error


def test_poll_keeps_running_within_grace(monkeypatch):
    p = _provider()
    _patch_get(monkeypatch, {
        "/history/job-a": {},
        "/queue": {"queue_running": [], "queue_pending": []},
    })
    fresh = GenResult(job_id="job-a", status="running")
    assert p.poll(fresh, "job-a").status == "running"


def test_resume_pending_restarts_poll_after_restart(tmp_path, monkeypatch):
    """面板重启后，停在 running 的任务要自动接回轮询。

    背景（2026-11-26）：GET /api/video/poll 只读数据库，真正轮询的是进程内的
    后台线程。面板一重启线程就没了，ComfyUI 其实早就出完片，DB 却永远 running，
    界面一直转圈。这里断言 resume 会把任务推进到终态。
    """
    import api.router as R

    monkeypatch.setattr(R, "_DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(R, "_db_ready", False)
    monkeypatch.setattr(R.time, "sleep", lambda _s: None)  # 跳过 5 秒间隔

    class FakeProv:
        def poll(self, result, job_id):
            return GenResult(job_id=job_id, status="succeeded",
                             video_url="/api/videos/x.mp4")

    monkeypatch.setattr(R, "_make_provider", lambda _name: FakeProv())

    R._video_job_write({
        "job_id": "zombie-1", "provider": "comfyui", "provider_label": "本地",
        "status": "running", "video_url": "", "actual_cost": 0.0, "error": "",
        "prompt": "x", "resolution": "480p", "duration": 5.0, "poll_count": 0,
    })
    R.resume_pending_video_jobs()

    for _ in range(100):
        if R._video_job_read("zombie-1")["status"] == "succeeded":
            break
        time.sleep(0.01)

    job = R._video_job_read("zombie-1")
    assert job["status"] == "succeeded"
    assert job["video_url"] == "/api/videos/x.mp4"
    # 已完成的任务不该被重复挂线程
    R.resume_pending_video_jobs()
    assert R._video_job_read("zombie-1")["status"] == "succeeded"


def test_generate_rejects_remote_image_url(monkeypatch):
    """本地只吃 file: 路径；外链不下载、不静默忽略，明确报错。"""
    from api.providers import GenRequest, ProviderError
    p = _provider()
    monkeypatch.setattr(p, "_require_online", lambda *a, **kw: None)
    try:
        p.generate(GenRequest(prompt="一个人站在雨里",
                              image_url="https://example.com/face.png"))
    except ProviderError as e:
        assert "只接受本机参考图" in str(e)
    else:
        raise AssertionError("外链参考图应当被拒绝")


def test_generate_reports_missing_local_image(monkeypatch, tmp_path):
    """file: 指向的文件没了，要报「参考图文件不存在」而不是随便出一片。"""
    from api.providers import GenRequest, ProviderError
    p = _provider()
    monkeypatch.setattr(p, "_require_online", lambda *a, **kw: None)
    missing = tmp_path / "nope.png"
    try:
        p.generate(GenRequest(prompt="x", image_url="file:" + str(missing)))
    except ProviderError as e:
        assert "参考图文件不存在" in str(e)
    else:
        raise AssertionError("缺文件应当报错")


def test_size_for_22_uses_official_multiple_of_32():
    """5B 官方档：宽高必须 32 的倍数，720p 是 1280x704。"""
    assert ComfyUIProvider._size_for_22("480p") == (832, 480)
    assert ComfyUIProvider._size_for_22("720p") == (1280, 704)
    forgot = (832, 480)
    assert forgot[0] % 32 == 0 and forgot[1] % 32 == 0


def test_length_for_22_is_4n_plus_1_at_24fps():
    """24fps、4n+1、上限 121 帧（5 秒）。"""
    assert ComfyUIProvider._length_for_22(5) == 121
    assert ComfyUIProvider._length_for_22(10) == 121, "超长要夹到 121"
    assert ComfyUIProvider._length_for_22(1) == 25
    for frames in (ComfyUIProvider._length_for_22(d) for d in (0.2, 1, 2.5, 5)):
        assert (frames - 1) % 4 == 0


def test_prepare_image_crops_to_target_ratio(tmp_path):
    """竖图必须裁成目标比例，否则 Wan 内部拉伸会把脸压扁。"""
    from PIL import Image
    src = tmp_path / "portrait.png"
    Image.new("RGB", (480, 832), (200, 30, 30)).save(src)
    out = ComfyUIProvider._prepare_image(src, 832, 480)
    try:
        with Image.open(out) as im:
            assert abs(im.width / im.height - 832 / 480) < 0.02, im.size
            assert im.height == 480
    finally:
        out.unlink(missing_ok=True)


def test_release_if_idle_frees_when_queue_empty(monkeypatch):
    """出片完队列空 → 释放显存，别把语音合成挤到换页。"""
    p = _provider()
    calls = []

    def fake_get(url, *a, **kw):
        if url.endswith("/queue"):
            return FakeResp({"queue_running": [], "queue_pending": []})
        raise AssertionError(url)

    def fake_post(url, *a, **kw):
        calls.append(url)
        return FakeResp({})

    monkeypatch.setattr("api.providers.comfyui.requests.get", fake_get)
    monkeypatch.setattr("api.providers.comfyui.requests.post", fake_post)
    p._release_if_idle()
    assert calls == [p.base_url + "/free"]


def test_release_if_idle_skips_when_jobs_pending(monkeypatch):
    """队列里还有任务 → 不释放，避免下一单重新加载模型。"""
    p = _provider()
    calls = []

    def fake_get(url, *a, **kw):
        return FakeResp({"queue_running": [], "queue_pending": [[1, "job-b", {}, {}, []]]})

    def fake_post(url, *a, **kw):
        calls.append(url)
        return FakeResp({})

    monkeypatch.setattr("api.providers.comfyui.requests.get", fake_get)
    monkeypatch.setattr("api.providers.comfyui.requests.post", fake_post)
    p._release_if_idle()
    assert calls == []


def test_svd_workflow_uses_checkpoint_slots_and_25_frames():
    """SVD 快出档：三段模型都从同一个 checkpoint 拆，槽位必须是 0/1/2。"""
    p = _provider()
    wf = p._build_workflow_svd(GenRequest(prompt="", duration=5), "ref.png")

    assert wf["1"]["class_type"] == "ImageOnlyCheckpointLoader"
    assert wf["1"]["inputs"]["ckpt_name"].startswith("svd_")
    cond = wf["3"]["inputs"]
    assert cond["clip_vision"] == ["1", 1]   # CLIP_VISION
    assert cond["vae"] == ["1", 2]           # VAE
    assert cond["init_image"] == ["2", 0]
    assert cond["video_frames"] == 25        # SVD 训练上限
    assert (cond["width"], cond["height"]) == (1024, 576)
    assert cond["fps"] == 5                  # 25 帧 / 5 秒
    ks = wf["5"]["inputs"]
    assert ks["positive"] == ["3", 0]
    assert ks["negative"] == ["3", 1]
    assert ks["latent_image"] == ["3", 2]
    assert wf["7"]["inputs"]["fps"] == 5.0


def test_svd_fps_derived_from_duration_keeps_length_honest():
    """播放帧率按时长反推：25 帧 / fps 必须接近请求时长，不能要 5 秒给 4.1 秒。"""
    p = _provider()
    for dur, fps in ((1.0, 24), (2.0, 12), (5.0, 5)):
        wf = p._build_workflow_svd(GenRequest(prompt="", duration=dur), "ref.png")
        assert wf["3"]["inputs"]["fps"] == fps, f"duration={dur}"
        assert abs(25 / fps - dur) <= 0.4, f"duration={dur} 实际 {25 / fps:.2f}s"


def test_svd_clamps_to_router_bounds_so_length_never_lies():
    """上下限与路由器的 5 秒红线一致：超界请求被钳制，绝不静默改短/拉长。

    短于 1 秒时 25 帧无法在 24fps 内 honest 还原（25/24≈1.04 秒），
    所以路由器直接 400；这里只保证万一漏进来也不会给出翻倍的时长。
    """
    p = _provider()
    short = p._build_workflow_svd(GenRequest(prompt="", duration=0.5), "ref.png")
    assert short["3"]["inputs"]["fps"] == 24          # 25 / 24 ≈ 1.04s，是物理下限
    long_ = p._build_workflow_svd(GenRequest(prompt="", duration=9), "ref.png")
    assert long_["3"]["inputs"]["fps"] == 5           # 钳到 5 秒，不偷偷出 8 秒


def test_svd_allows_empty_prompt_but_5b_does_not(monkeypatch):
    """SVD 没有文本条件，空提示词合法；5B 仍然必须给提示词。"""
    p = _provider()
    _patch_get(monkeypatch, {"/system_stats": {}})
    monkeypatch.setattr("api.providers.comfyui.requests.post",
                        lambda *a, **kw: FakeResp({}))
    monkeypatch.setattr("api.providers.comfyui._gpu_free_mb", lambda: 8000)

    with pytest.raises(ProviderError) as e:
        p.generate(GenRequest(prompt="  ", image_url="file:F:/nope/x.png"))
    assert "提示词" in str(e.value)

    with pytest.raises(ProviderError) as e:
        p.generate(GenRequest(prompt="", image_url="file:F:/nope/x.png", engine="svd"))
    assert "参考图文件不存在" in str(e.value)


def test_vram_gate_matches_measured_reality(monkeypatch):
    """显存门槛必须贴着实测，不能按「装得下」卡，也不能低到必然换页卡死。

    实测（2026-11-26）：
      * I2V 5B：空闲 5742MB → 2.03 秒/步；1154MB → 8 秒/步、307 秒出片；
        742MB → 33.6 秒/步，30 分钟卡在 VAE 解码（失败）。
      * T2V 1.3B：空闲 808MB 时 25 帧 / 4 步 158.8 秒，能出片。
    所以 I2V 门槛要落在「742MB 必然失败」之上，又不能高到清完缓存也点不动。
    """
    from api.providers import ProviderError
    from api.providers import comfyui as C

    # 常量本身也要钉住：谁调回 900 以下（会重演 742MB 卡死）或调到 4000 以上都红
    assert 1000 <= C._MIN_FREE_VRAM_MB_I2V <= 4000, (
        f"I2V 门槛 {C._MIN_FREE_VRAM_MB_I2V}MB 必须高于实测失败值 742MB，"
        "且不高于清缓存后 8GB 卡的实际可用量")
    assert C._MIN_FREE_VRAM_MB <= 900, (
        f"T2V 门槛 {C._MIN_FREE_VRAM_MB}MB 高于实测可用值 808MB")

    p = _provider()
    _patch_get(monkeypatch, {"/system_stats": {}})

    # 刚好够门槛：两档都要放行
    monkeypatch.setattr(C, "_gpu_free_mb", lambda: C._MIN_FREE_VRAM_MB_I2V)
    p._require_online(C._MIN_FREE_VRAM_MB_I2V)
    monkeypatch.setattr(C, "_gpu_free_mb", lambda: C._MIN_FREE_VRAM_MB)
    p._require_online(C._MIN_FREE_VRAM_MB)

    # 真的快没空间才拦
    monkeypatch.setattr(C, "_gpu_free_mb", lambda: 100)
    monkeypatch.setattr(C, "_free_tts_vram", lambda *a, **kw: "语音服务未响应")
    try:
        p._require_online(C._MIN_FREE_VRAM_MB)
    except ProviderError as e:
        assert "显存不足" in str(e)
    else:
        raise AssertionError("只剩 100MB 应当拦住")
