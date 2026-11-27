"""REQ-20261127-01 导出音轨用例（AC-01/02/03）。

背景：旧管线 compose() 只把 voice 拼接成音轨，mix.wav（含 ambience/BGM 铺底）
根本没进成片，实测 volumedetect = -91dB（纯静音）。这里用真实 ffmpeg 造素材、
真跑 compose，逐条验收：
  AC-01 有 mix 时音轨来自 mix，音量可测
  AC-02 全片无任何可用音频时必须拒绝导出，不允许静默出哑片
  AC-03 部分镜头缺素材时 silent_shots 计数正确，且不拖垮整次导出
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from video_pipeline.ffmpeg_export import (  # noqa: E402
    ShotSegment, compose, measure_volume,
)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="机器上没有 ffmpeg")


def _make_tone(path: Path, seconds: float = 1.5, freq: int = 440) -> Path:
    """独立于被测模块造一段正弦波素材（ ffmpeg lavfi ）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}",
         "-ac", "2", "-ar", "44100", str(path)],
        capture_output=True, check=True,
    )
    return path


def test_measure_volume_读懂音量():
    """音量探测本身要准：有声素材读出正常 dB，静音读出极低声。"""
    from video_pipeline.ffmpeg_export import _run
    tmp = Path(__import__("tempfile").mkdtemp(prefix="bee-vol-"))
    try:
        tone = _make_tone(tmp / "tone.wav")
        assert measure_volume(str(tone)) is not None
        assert measure_volume(str(tone)) > -60

        silent = tmp / "silent.wav"
        _run([FFMPEG, "-y", "-f", "lavfi",
              "-i", "anullsrc=channel_layout=stereo:sample_rate=44100:duration=1",
              str(silent)])
        assert measure_volume(str(silent)) < -60
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ac01_有混音时音轨取mix且音量正常(tmp_path):
    tone = _make_tone(tmp_path / "mix.wav")
    shots = [
        ShotSegment(shot_id="a", start_sec=0, end_sec=1, mix_path=str(tone), scene_no=1),
        ShotSegment(shot_id="b", start_sec=1, end_sec=2, mix_path=str(tone), scene_no=1),
    ]
    out = compose(shots, tmp_path / "proj")

    assert out["error"] == "", out
    assert Path(out["mp4_path"]).is_file()
    assert out["audio_source"] == "mix"
    assert out["silent_shots"] == 0
    # 关键回归点：成片必须真的有声音（旧代码这里会是 -91dB）
    assert out["audio_level_db"] is not None
    assert out["audio_level_db"] > -60, f"成片仍是哑的: {out['audio_level_db']}dB"


def test_ac01b_只有配音时也能出声(tmp_path):
    """没配铺底的老项目不能因为这次改动变哑——voice 兜底。"""
    tone = _make_tone(tmp_path / "voice.wav")
    shots = [ShotSegment(shot_id="a", start_sec=0, end_sec=1, voice_path=str(tone), scene_no=1)]
    out = compose(shots, tmp_path / "proj")
    assert out["error"] == "", out
    assert out["audio_source"] == "voice"
    assert out["audio_level_db"] is not None and out["audio_level_db"] > -60


def test_ac02_全空素材必须拒绝导出(tmp_path):
    shots = [
        ShotSegment(shot_id="a", start_sec=0, end_sec=1, scene_no=1),
        ShotSegment(shot_id="b", start_sec=1, end_sec=2, scene_no=2),
    ]
    out = compose(shots, tmp_path / "proj")

    assert out["error"], "没有任何音轨却报成功就是哑片事故"
    assert "无可用配音" in out["error"]
    assert out["mp4_path"] == "" and out["zip_path"] == ""
    assert out["audio_source"] == "none"
    assert out["silent_shots"] == len(shots)
    assert not (tmp_path / "proj" / "final.mp4").exists()


def test_ac03_部分镜头缺素材仍出片并计数(tmp_path):
    tone = _make_tone(tmp_path / "mix.wav")
    shots = [
        ShotSegment(shot_id="a", start_sec=0, end_sec=1, mix_path=str(tone), scene_no=1),
        ShotSegment(shot_id="b", start_sec=1, end_sec=2, scene_no=1),          # 缺素材
        ShotSegment(shot_id="c", start_sec=2, end_sec=3, voice_path=str(tone), scene_no=2),
    ]
    out = compose(shots, tmp_path / "proj")

    assert out["error"] == "", out
    assert Path(out["mp4_path"]).is_file()
    assert out["audio_source"] == "mix"
    assert out["silent_shots"] == 1


def test_素材短于镜头时补静音且不截短画面(tmp_path):
    """需求边界反例：mix 比镜头短，只许该段后半静音，不许把画面截短。

    旧实现 -t 只截不补 + 最终 -shortest 会把整片拖短到音频时长。
    """
    short = _make_tone(tmp_path / "short.wav", seconds=0.3)
    shots = [ShotSegment(shot_id="a", start_sec=0, end_sec=1, mix_path=str(short), scene_no=1)]
    out = compose(shots, tmp_path / "proj")
    assert out["error"] == "", out

    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", out["mp4_path"]],
                       capture_output=True, text=True)
    dur = float(r.stdout.strip() or "0")
    assert dur >= 0.9, f"画面被音轨拖短了: {dur}s"
    assert out["audio_level_db"] is not None

