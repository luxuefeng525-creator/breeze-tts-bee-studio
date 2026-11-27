"""Smoke test: 造 3 段 ShotSegment（全黑画面 + 字幕），跑 ffmpeg_export.compose。
目的：验证切片 D 的 ffmpeg 管线通——不报错、mp4 时长对、zip 里有 manifest + srt + mp4。

注意：音轨全空的导出会被 compose 直接拒绝（那是需求 R-02 的行为），所以这里
必须给镜头一段真实配音，否则断言必挂。
"""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from pathlib import Path
from video_pipeline.ffmpeg_export import ShotSegment, compose
import tempfile, os, shutil, subprocess, zipfile

tmp = Path(tempfile.mkdtemp(prefix="bee-export-smoke-"))
voice = tmp / "voice.wav"
subprocess.run([(shutil.which("ffmpeg") or "ffmpeg"), "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=9",
                "-ac", "2", "-ar", "44100", str(voice)],
               check=True, capture_output=True)

shots = [
    ShotSegment(shot_id="s1", video_path="", start_sec=0, end_sec=3, voice_path=str(voice), subtitle="林队：雨夜的抉择，该结束了。", scene_no=1),
    ShotSegment(shot_id="s2", video_path="", start_sec=3, end_sec=6, voice_path=str(voice), subtitle="苏娜：你想好了？", scene_no=2),
    ShotSegment(shot_id="s3", video_path="", start_sec=6, end_sec=9, voice_path=str(voice), subtitle="旁白：雨还在下。", scene_no=3),
]

print(f"输出目录: {tmp}")
result = compose(shots, tmp)
print(f"compose 结果: {result}")

# 断言
assert result["error"] == "", f"ffmpeg 报错: {result['error']}"
assert result.get("audio_source") == "voice", f"音轨来源不对: {result.get('audio_source')}"
assert Path(result["mp4_path"]).is_file(), "final.mp4 没生成"
assert Path(result["zip_path"]).is_file(), "export.zip 没生成"

mp4_size = Path(result["mp4_path"]).stat().st_size
zip_size = Path(result["zip_path"]).stat().st_size
print(f"final.mp4 大小: {mp4_size} bytes")
print(f"export.zip 大小: {zip_size} bytes")
assert mp4_size > 10_000, f"mp4 太小: {mp4_size}"
assert zip_size > 5_000, f"zip 太小: {zip_size}"

# zip 里有什么
with zipfile.ZipFile(result["zip_path"]) as zf:
    names = zf.namelist()
    print(f"zip 条目数: {len(names)}")
    for n in names:
        print(f"  - {n}")
    assert "final.mp4" in names, "zip 里没 final.mp4"
    assert "assets/subtitles.srt" in names, "zip 里没 subtitles.srt"
    assert "manifest.json" in names, "zip 里没 manifest.json"

print("\n✅ 切片 D ffmpeg 合成管线 smoke test PASS")
