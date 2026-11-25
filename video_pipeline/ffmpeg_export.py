"""ffmpeg 本地合成管线（切片 D）。

输入：shots 列表，每段带：
  video_path   —— 本地视频文件路径（mp4），可空=黑屏占位
  start_sec    —— 入点（秒）
  end_sec      —— 出点（秒）
  voice_path   —— 配音音频（wav），可空=静音
  subtitle     —— 字幕文本（SRT 用）

输出：
  output_dir/project/{project_id}.mp4   —— 最终合成（H.264 / AAC / 内嵌字幕）
  output_dir/project/{project_id}.zip   —— 各分镜原视频 + 配音 + 剧本 + SRT
"""
from __future__ import annotations
import json, os, subprocess, zipfile, tempfile, shutil, datetime
from dataclasses import dataclass
from pathlib import Path


FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


@dataclass
class ShotSegment:
    shot_id: str
    video_path: str = ""
    start_sec: float = 0.0
    end_sec: float = 5.0
    voice_path: str = ""
    subtitle: str = ""
    scene_no: int = 1


# ------------------------------------------------------------------
# 子进程封装（核心：所有 ffmpeg 命令的 cwd 都设为 tmpdir，
# 这样 subtitles filter 在 Windows 下看不到盘符，不会崩溃）
# ------------------------------------------------------------------
def _run(cmd: list[str], timeout: float = 600.0, cwd: str | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", cwd=cwd)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-5:]
        raise RuntimeError(f"ffmpeg 失败 (rc={r.returncode}): {' '.join(cmd[:3])}\n"
                           + "\n".join(tail))
    return r


def _ffprobe_duration(path: str) -> float:
    try:
        r = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=noprint_wrappers=1:nokey=1", path])
        return float(r.stdout.strip() or "0")
    except Exception:
        return 0.0


def _is_media(path: str) -> bool:
    """可读媒体：本地真实文件，或 http(s) 直链（ffmpeg 能直接拉流）。

    只判 os.path.isfile 会把网络 URL 当成"没有素材"，整段静音/黑屏。
    """
    if not path:
        return False
    return os.path.isfile(path) or path.startswith(("http://", "https://"))


# ------------------------------------------------------------------
# 所有内部函数都用相对路径（cwd = tmpdir）
# ------------------------------------------------------------------
def build_concat_list(shots: list[ShotSegment], tmpdir: Path,
                      cwd: Path) -> tuple[Path, list[float]]:
    list_path = tmpdir / "concat.txt"
    durations: list[float] = []
    lines: list[str] = []

    for i, s in enumerate(shots):
        clip_rel = f"clip_{i:03d}.mp4"
        clip_abs = tmpdir / clip_rel
        dur = max(0.5, round(s.end_sec - s.start_sec, 2))
        durations.append(dur)

        if not _is_media(s.video_path):
            # 黑屏 + 静音
            _run([FFMPEG, "-y",
                 "-f", "lavfi", "-i", f"color=c=black:s=1280x720:d={dur}",
                 "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100:duration={dur}",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", clip_rel], cwd=str(cwd))
        else:
            ss = f"{s.start_sec:.3f}"
            t = f"{dur:.3f}"
            _run([FFMPEG, "-y",
                 "-ss", ss, "-i", s.video_path,
                 "-t", t, "-c:v", "libx264", "-preset", "veryfast",
                 "-c:a", "aac", "-strict", "experimental", clip_rel],
                 cwd=str(cwd))
        lines.append(f"file '{clip_rel}'")

    list_path.write_text("\n".join(lines), encoding="utf-8")
    return list_path, durations


def build_audio_mix(shots: list[ShotSegment], durations: list[float],
                    tmpdir: Path, cwd: Path) -> Path:
    seg_list: list[str] = []

    for i, s in enumerate(shots):
        d = durations[i]
        seg_rel = f"voice_{i:03d}.wav"
        if _is_media(s.voice_path):
            _run([FFMPEG, "-y", "-i", s.voice_path,
                 "-t", f"{d:.3f}", "-ac", "2", "-ar", "44100", seg_rel],
                 cwd=str(cwd))
        else:
            _run([FFMPEG, "-y",
                 "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100:duration={d:.3f}",
                 seg_rel], cwd=str(cwd))
        seg_list.append(seg_rel)

    concat_audio = tmpdir / "audio_concat.txt"
    concat_audio.write_text("\n".join(f"file '{p}'" for p in seg_list), encoding="utf-8")
    _run([FFMPEG, "-y", "-f", "concat", "-safe", "0",
          "-i", "audio_concat.txt", "-c", "copy", "mixed_audio.wav"],
         cwd=str(cwd))
    return tmpdir / "mixed_audio.wav"


def build_srt(shots: list[ShotSegment], durations: list[float],
              out_dir: Path) -> Path:
    """在给定目录下生成 srt 文件（相对路径友好）。"""
    srt_path = out_dir / "subtitles.srt"
    t0 = 0.0

    def _fmt(t: float) -> str:
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    with open(srt_path, "w", encoding="utf-8") as srt:
        for i, s in enumerate(shots):
            if not s.subtitle.strip():
                t0 += durations[i]
                continue
            t1 = t0 + durations[i]
            srt.write(f"{i+1}\n")
            srt.write(f"{_fmt(t0)} --> {_fmt(t1)}\n")
            srt.write(f"{s.subtitle.strip()}\n\n")
            t0 = t1
    return srt_path


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------
def compose(shots: list[ShotSegment], output_dir: Path,
            resolution: str = "720p", preset: str = "medium") -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="bee-export-"))

    try:
        concat_list, durations = build_concat_list(shots, tmpdir, tmpdir)
        build_audio_mix(shots, durations, tmpdir, tmpdir)
        build_srt(shots, durations, tmpdir)

        # 串视频
        _run([FFMPEG, "-y", "-f", "concat", "-safe", "0",
              "-i", "concat.txt",
              "-c:v", "libx264", "-preset", preset, "-crf", "23",
              "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
              "-pix_fmt", "yuv420p", "full_video.mp4"],
             cwd=str(tmpdir))

        # 最终合成：视频 + 音频 + 烧字幕（全部相对路径 + cwd=tmpdir）
        has_subtitle = any(s.subtitle.strip() for s in shots)
        if has_subtitle:
            cmd = [FFMPEG, "-y",
                   "-i", "full_video.mp4", "-i", "mixed_audio.wav",
                   "-map", "0:v", "-map", "1:a",
                   "-c:v", "libx264", "-preset", preset, "-crf", "23",
                   "-vf", "subtitles=subtitles.srt",
                   "-c:a", "aac", "-b:a", "192k", "-shortest",
                   str(output_dir / "final.mp4")]
        else:
            cmd = [FFMPEG, "-y",
                   "-i", "full_video.mp4", "-i", "mixed_audio.wav",
                   "-map", "0:v", "-map", "1:a",
                   "-c:v", "libx264", "-preset", preset, "-crf", "23",
                   "-c:a", "aac", "-b:a", "192k", "-shortest",
                   str(output_dir / "final.mp4")]
        _run(cmd, cwd=str(tmpdir))

        # 6. ZIP 打包（tmpdir 全部中间产物 + output_dir 最终 mp4）
        zip_path = output_dir / "export.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # tmpdir 全部
            for root, _, files in os.walk(tmpdir):
                for fname in files:
                    fpath = Path(root) / fname
                    zf.write(fpath, f"assets/{fname}")
            # 最终产物（从 output_dir 打一份副本，避免跟 assets 目录混）
            final_path = output_dir / "final.mp4"
            if final_path.is_file():
                zf.write(final_path, "final.mp4")
            # manifest
            manifest = {
                "exported_at": datetime.datetime.now().isoformat(),
                "duration_total_sec": sum(durations),
                "shots_count": len(shots),
                "shots": [
                    {"scene_no": s.scene_no, "shot_id": s.shot_id,
                     "start_sec": s.start_sec, "end_sec": s.end_sec,
                     "subtitle": s.subtitle, "video_path": s.video_path,
                     "voice_path": s.voice_path}
                    for s in shots
                ]
            }
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        return {
            "mp4_path": str(output_dir / "final.mp4"),
            "zip_path": str(zip_path),
            "duration_total": round(sum(durations), 2),
            "shots_count": len(shots),
            "error": "",
        }
    except subprocess.TimeoutExpired:
        return {"mp4_path": "", "zip_path": "", "error": "ffmpeg 超时（>600s）"}
    except Exception as e:
        return {"mp4_path": "", "zip_path": "", "error": str(e)[:300]}
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
