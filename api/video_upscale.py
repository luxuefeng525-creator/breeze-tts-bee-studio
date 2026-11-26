"""视频超分 —— ffmpeg 拆帧 + 本机 ComfyUI Real-ESRGAN 模型逐帧放大 + 回封装。

为什么走「拆帧」而不是找一条视频超分工作流：
  本机 ComfyUI 没有装 VideoHelperSuite 之类的视频节点（custom_nodes 为空），
  官方 SeedVR2 视频超分模板要额外下 3.7GB 模型；
  而 ComfyUI 自带的 UpscaleModelLoader + ImageUpscaleWithModel 是可用的
  （/object_info 实拉确认），配上 63.8MB 的 RealESRGAN_x4plus 就能做真超分。
  代价是帧数决定耗时：8GB 卡上约 1–2 秒/帧。

实测数据（2026-11-26）：
  模型文件 ComfyUI/models/upscale_models/RealESRGAN_x4plus.safetensors 63.8MB
  来自 hf-mirror.com/Comfy-Org/Real-ESRGAN（2.48MB/s，26 秒下完）

零残留：每帧用 ComfyUI 的 input/output 目录搬运，处理完立刻删除；
临时目录在 finally 里整个清掉。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import requests

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
COMFY_URL = (os.environ.get("BREEZE_COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")
COMFY_INPUT = Path(os.environ.get("BREEZE_COMFY_INPUT") or r"F:\ComfyUI-core\input")
COMFY_OUTPUT = Path(os.environ.get("BREEZE_COMFY_OUTPUT") or r"F:\ComfyUI-core\output")
MODEL_NAME = os.environ.get("BREEZE_UPSCALE_MODEL") or "RealESRGAN_x4plus.safetensors"

MAX_FRAMES = 241          # 约 10 秒 @24fps：再长就不是「等一下」而是「跑一夜」
FRAME_TIMEOUT = 180
_JOB_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}


class UpscaleError(RuntimeError):
    """可直接展示给用户的中文错误。"""


def _data_dir() -> Path:
    return Path(os.environ.get("BREEZE_DATA")
                or (Path(__file__).resolve().parent.parent / "data")).resolve()


def videos_dir() -> Path:
    d = _data_dir() / "videos"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------
# 媒体探测 / 列表
# ------------------------------------------------------------------
def _run(cmd: list[str], timeout: float) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        raise UpscaleError(f"ffmpeg 超时（{int(timeout)} 秒）") from e
    if r.returncode != 0:
        raise UpscaleError(f"ffmpeg 失败：{(r.stderr or '')[-300:]}")
    return r.stdout or ""


def probe(path: Path) -> dict:
    """读宽高 / 帧率 / 帧数 / 时长 / 是否有音轨。"""
    out = _run([FFPROBE, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path)], timeout=60)
    try:
        meta = json.loads(out)
    except ValueError as e:
        raise UpscaleError(f"读不出视频信息：{e}") from e
    v = next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise UpscaleError("这个文件里没有视频轨")
    fps = 0.0
    for key in ("r_frame_rate", "avg_frame_rate"):
        try:
            fps = float(Fraction(str(v.get(key) or "0/1")))
            if fps > 0:
                break
        except (ValueError, ZeroDivisionError):
            continue
    duration = float(v.get("duration") or meta.get("format", {}).get("duration") or 0)
    frames = int(v.get("nb_frames") or 0) or (int(round(duration * fps)) if fps else 0)
    return {
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": round(fps, 3),
        "frames": frames,
        "duration": round(duration, 2),
        "has_audio": any(s.get("codec_type") == "audio" for s in meta.get("streams", [])),
    }


def list_videos() -> list[dict]:
    items = []
    for p in sorted(videos_dir().glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
            info = probe(p)
        except (OSError, UpscaleError):
            continue
        items.append({
            "file": p.name, "url": f"/api/videos/{p.name}",
            "bytes": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "width": info["width"], "height": info["height"],
            "frames": info["frames"], "fps": info["fps"], "duration": info["duration"],
            "upscaled": "-x2" in p.stem or "-x4" in p.stem,
        })
    return items


def _safe_video(name: str) -> Path:
    name = str(name or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name or not name.lower().endswith(".mp4"):
        raise UpscaleError("视频文件名不合法")
    p = (videos_dir() / name).resolve()
    if not p.is_file() or p.parent != videos_dir().resolve():
        raise UpscaleError("视频不存在")
    return p


# ------------------------------------------------------------------
# 单帧超分
# ------------------------------------------------------------------
def _object_info_ok() -> None:
    try:
        r = requests.get(f"{COMFY_URL}/object_info/UpscaleModelLoader", timeout=10)
        if r.status_code != 200:
            raise UpscaleError(f"ComfyUI 状态异常（HTTP {r.status_code}）")
        opts = ((r.json() or {}).get("UpscaleModelLoader", {})
                .get("input", {}).get("required", {}).get("model_name") or [None, {}])[1].get("options") or []
    except requests.RequestException as e:
        raise UpscaleError(f"本地 ComfyUI 没启动（{COMFY_URL}）：{e}") from e
    except (ValueError, AttributeError, IndexError):
        opts = []
    if MODEL_NAME not in opts:
        raise UpscaleError(
            f"超分模型 {MODEL_NAME} 未加载：请确认文件在 ComfyUI/models/upscale_models/"
            f"并重启 ComfyUI（当前可选：{', '.join(opts) or '无'}）")


def _workflow(input_name: str, prefix: str) -> dict:
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": input_name}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": MODEL_NAME}},
        "3": {"class_type": "ImageUpscaleWithModel",
              "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "SaveImage",
              "inputs": {"images": ["3", 0], "filename_prefix": prefix}},
    }


def _upscale_one(src_png: Path, token: str, index: int, out_dir: Path, deadline: float) -> None:
    """把一帧送进 ComfyUI 放大，拉到 out_dir。用完即删两侧中间文件。"""
    in_name = f"{token}_{index:06d}.png"
    in_path = COMFY_INPUT / in_name
    try:
        COMFY_INPUT.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_png, in_path)
    except OSError as e:
        raise UpscaleError(f"把帧送进 ComfyUI 失败：{e}") from e

    prefix = f"{token}_{index:06d}"
    out_file: Path | None = None
    try:
        try:
            resp = requests.post(f"{COMFY_URL}/prompt",
                                 json={"prompt": _workflow(in_name, prefix),
                                       "client_id": uuid.uuid4().hex}, timeout=30)
        except requests.RequestException as e:
            raise UpscaleError(f"连不上 ComfyUI：{e}") from e
        if resp.status_code != 200:
            raise UpscaleError(f"ComfyUI 拒绝超分工作流（HTTP {resp.status_code}）：{resp.text[:200]}")
        pid = (resp.json() or {}).get("prompt_id")
        if not pid:
            raise UpscaleError("ComfyUI 没返回 prompt_id")

        while True:
            if time.time() > deadline:
                raise UpscaleError("单帧超时")
            try:
                h = requests.get(f"{COMFY_URL}/history/{pid}", timeout=15)
                entry = (h.json() or {}).get(pid) if h.status_code == 200 else None
            except (requests.RequestException, ValueError):
                entry = None
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    detail = ""
                    for m in status.get("messages") or []:
                        if isinstance(m, (list, tuple)) and len(m) > 1 and m[0] == "execution_error":
                            detail = str((m[1] or {}).get("exception_message") or "")[:200]
                    raise UpscaleError(detail or "ComfyUI 执行超分出错")
                for node_out in (entry.get("outputs") or {}).values():
                    for item in (node_out or {}).get("images") or []:
                        if isinstance(item, dict) and item.get("filename"):
                            out_file = COMFY_OUTPUT / item.get("subfolder", "") / item["filename"]
                            break
                    if out_file:
                        break
                if out_file:
                    break
            time.sleep(0.4)

        if not out_file.is_file():
            raise UpscaleError(f"找不到超分产物 {out_file}")
        shutil.copyfile(out_file, out_dir / f"{index:06d}.png")
    finally:
        try:
            in_path.unlink(missing_ok=True)
        except OSError:
            pass
        if out_file:
            try:
                out_file.unlink(missing_ok=True)
            except OSError:
                pass


# ------------------------------------------------------------------
# 任务（后台线程 + 进度）
# ------------------------------------------------------------------
def _set(jid: str, **kw) -> None:
    with _JOB_LOCK:
        job = _JOBS.get(jid)
        if job:
            job.update(kw)


def start(src_name: str, scale: int = 2) -> dict:
    src = _safe_video(src_name)
    scale = 2 if int(scale or 2) not in (2, 4) else int(scale)
    info = probe(src)
    if info["frames"] <= 0:
        raise UpscaleError("读不出视频帧数，无法拆分")
    if info["frames"] > MAX_FRAMES:
        raise UpscaleError(
            f"这段视频 {info['frames']} 帧，超过单次上限 {MAX_FRAMES} 帧"
            f"（约 10 秒）。请先在视频工作台剪短再超分。")
    _object_info_ok()
    with _JOB_LOCK:
        if any(j["status"] == "running" for j in _JOBS.values()):
            raise UpscaleError("已有一段视频在超分，请等它完成")
        jid = uuid.uuid4().hex[:12]
        _JOBS[jid] = {
            "job_id": jid, "status": "running", "file": src.name, "scale": scale,
            "done": 0, "total": info["frames"], "error": "", "started": time.time(),
            "width": info["width"], "height": info["height"],
            "fps": info["fps"], "duration": info["duration"],
        }
        # 只保留最近 20 条记录，避免长跑内存里堆死数据
        if len(_JOBS) > 20:
            for old in sorted(_JOBS.values(), key=lambda x: x["started"])[:len(_JOBS) - 20]:
                _JOBS.pop(old["job_id"], None)
    threading.Thread(target=_worker, args=(jid, src, info, scale), daemon=True).start()
    return dict(_JOBS[jid])


def status(jid: str) -> dict:
    with _JOB_LOCK:
        job = _JOBS.get(str(jid or ""))
        if not job:
            raise UpscaleError("任务不存在或已过期")
        out = dict(job)
        out["elapsed"] = round(time.time() - job["started"], 1)
        return out


def _worker(jid: str, src: Path, info: dict, scale: int) -> None:
    work = _data_dir() / "_tmp" / f"up-{jid}"
    frames_in = work / "in"
    frames_out = work / "out"
    token = f"beeup{datetime.now().strftime('%H%M%S')}{jid[:4]}"
    try:
        frames_in.mkdir(parents=True, exist_ok=True)
        frames_out.mkdir(parents=True, exist_ok=True)
        # 抽帧：PNG 无损，避免先压一次再放大。
        # ffmpeg 9 已移除 -vsync，改用 -fps_mode passthrough（等价于保留原始帧）。
        _run([FFMPEG, "-y", "-i", str(src), "-fps_mode", "passthrough",
              str(frames_in / "%06d.png")], timeout=600)
        frames = sorted(frames_in.glob("*.png"))
        if not frames:
            raise UpscaleError("抽帧结果为空")
        _set(jid, total=len(frames))

        deadline_base = time.time()
        for i, f in enumerate(frames, 1):
            _upscale_one(f, token, i, frames_out, deadline_base + FRAME_TIMEOUT * i)
            _set(jid, done=i)
            f.unlink(missing_ok=True)

        fps = info["fps"] or 24.0
        dest = videos_dir() / f"{src.stem}-x{scale}.mp4"
        cmd = [FFMPEG, "-y", "-framerate", f"{fps:g}",
               "-i", str(frames_out / "%06d.png"), "-i", str(src),
               "-map", "0:v:0"]
        if info.get("has_audio"):
            cmd += ["-map", "1:a:0", "-c:a", "copy"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p"]
        if scale == 2:
            # 模型固定 x4：2 倍档先超到 4 倍再降一半，边缘比直接 x2 更干净
            cmd += ["-vf", "scale=trunc(iw/4)*2:trunc(ih/4)*2"]
        else:
            # 4 倍档只保证偶数尺寸（H.264 要求）
            cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        cmd += ["-movflags", "+faststart", str(dest)]
        _run(cmd, timeout=900)

        out_info = probe(dest)
        _set(jid, status="done", result={
            "file": dest.name, "url": f"/api/videos/{dest.name}",
            "bytes": dest.stat().st_size, "width": out_info["width"],
            "height": out_info["height"], "frames": out_info["frames"],
            "seconds": round(time.time() - _JOBS.get(jid, {}).get("started", time.time()), 1),
        })
        # 让出显存，避免把后续 TTS 合成挤爆
        try:
            requests.post(f"{COMFY_URL}/free",
                          json={"unload_models": True, "free_memory": True}, timeout=30)
        except requests.RequestException:
            pass
    except Exception as e:  # 任何失败都要落到任务状态里，前端才知道为什么停
        _set(jid, status="failed", error=str(e)[:400])
    finally:
        shutil.rmtree(work, ignore_errors=True)
