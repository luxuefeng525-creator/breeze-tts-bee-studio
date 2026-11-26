"""SD1.5 文生图 / ControlNet 结构控制 / IP-Adapter 参考图 —— 本机 ComfyUI 出图能力。

工作流节点与参数全部取自本机 /object_info 实拉（2026-11-26）：
  基础：CheckpointLoaderSimple -> CLIPTextEncode(正/负) -> EmptyLatentImage
        -> KSampler -> VAEDecode -> SaveImage
  结构：LoadImage -> Canny -> ControlNetLoader -> ControlNetApplyAdvanced
        （ControlNetApplyAdvanced 返回 (positive, negative) 两个条件，KSampler 分别接 0/1）
  参考：LoadImage -> IPAdapterModelLoader + CLIPVisionLoader -> IPAdapterAdvanced
        （输出的是 MODEL，替换 KSampler.model，不改提示词）

模型（本机实测已就位）：
  checkpoints/v1-5-pruned-emaonly.ckpt               4067.8MB
  controlnet/control_v11p_sd15_canny_fp16.safetensors 689.1MB
  ipadapter/ip-adapter_sd15.safetensors                42.6MB
  clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

_DEFAULT_URL = "http://127.0.0.1:8188"
_CKPT = os.environ.get("BREEZE_SD15_CKPT") or "v1-5-pruned-emaonly.ckpt"
_CONTROL_MODEL = os.environ.get("BREEZE_CONTROLNET") or "control_v11p_sd15_canny_fp16.safetensors"
_IP_MODEL = os.environ.get("BREEZE_IPADAPTER") or "ip-adapter_sd15.safetensors"
_CLIP_VISION = os.environ.get("BREEZE_CLIP_VISION") or "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"

DEFAULT_NEGATIVE = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
    "fewer digits, cropped, worst quality, low quality, normal quality, "
    "jpeg artifacts, signature, watermark, username, blurry"
)

_MIN_SIDE, _MAX_SIDE, _SIDE_STEP = 256, 1024, 8
_MIN_FREE_VRAM_MB = 1500
_NEED_CONTROL_MB = 2400
_NEED_IP_MB = 4200          # SD1.5 + CLIP ViT-H 视觉编码器 + 注意力补丁，8GB 卡要腾干净
_DEFAULT_TIMEOUT = 300
_MAX_REF_BYTES = 12 * 1024 * 1024

_RUN_LOCK = threading.Lock()


class ImageGenError(RuntimeError):
    """可直接展示给用户的中文错误。"""


def _data_dir() -> Path:
    root = Path(os.environ.get("BREEZE_DATA")
                or (Path(__file__).resolve().parent.parent / "data"))
    return root.resolve()


def images_dir() -> Path:
    d = _data_dir() / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def refs_control_dir() -> Path:
    d = _data_dir() / "refs" / "control"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _comfy_input() -> Path:
    return Path(os.environ.get("BREEZE_COMFY_INPUT") or r"F:\ComfyUI-core\input")


def _base_url() -> str:
    return (os.environ.get("BREEZE_COMFYUI_URL") or _DEFAULT_URL).rstrip("/")


def _clamp_side(value, default: int) -> int:
    """宽高必须是 8 的倍数：SD1.5 的 VAE 下采样 8 倍，非整数会被 ComfyUI 拒绝。"""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        n = default
    n = max(_MIN_SIDE, min(_MAX_SIDE, n))
    return n - (n % _SIDE_STEP)


# ------------------------------------------------------------------
# 参考图落盘（ControlNet / IP-Adapter 共用）
# ------------------------------------------------------------------
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)


def _sniff_ext(data: bytes) -> str | None:
    for magic, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def save_reference(filename: str, data: bytes) -> dict:
    """保存上传的结构/人物参考图，返回相对 DATA 的路径。"""
    if not data:
        raise ImageGenError("参考图内容为空")
    if len(data) > _MAX_REF_BYTES:
        raise ImageGenError("参考图超过 12MB，请压缩后再传")
    ext = _sniff_ext(data)
    if not ext:
        raise ImageGenError("只接受 PNG / JPEG / WEBP 图片")
    stem = Path(str(filename or "ref")).stem[:40] or "ref"
    safe = "".join(ch for ch in stem if ch.isalnum() or ch in "-_") or "ref"
    name = f"{safe}-{uuid.uuid4().hex[:10]}{ext}"
    target = refs_control_dir() / name
    try:
        target.write_bytes(data)
    except OSError as e:
        raise ImageGenError(f"参考图写入失败：{e}") from e
    rel = f"refs/control/{name}"
    return {"path": rel, "url": "/api/" + rel, "bytes": len(data)}


def _resolve_ref(raw: str) -> Path:
    """把 'refs/control/x.png' 或 data 目录内的绝对路径解析成真实文件。"""
    s = str(raw or "").strip().replace("\\", "/")
    if not s:
        raise ImageGenError("参考图路径为空")
    base = _data_dir()
    p = Path(s)
    target = (p if p.is_absolute() else (base / s)).resolve()
    if target != base and base not in target.parents:
        raise ImageGenError("参考图路径越界（只接受 data 目录内文件）")
    if not target.is_file():
        raise ImageGenError(f"参考图不存在：{target}")
    if target.stat().st_size > _MAX_REF_BYTES:
        raise ImageGenError("参考图超过 12MB，请压缩后再传")
    return target


def _stage_ref(path: Path) -> str:
    """把参考图复制进 ComfyUI 的 input 目录，返回 LoadImage 可用的文件名。"""
    name = f"bee-ref-{uuid.uuid4().hex[:12]}{path.suffix.lower() or '.png'}"
    dest = _comfy_input() / name
    try:
        _comfy_input().mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    except OSError as e:
        raise ImageGenError(f"把参考图送进 ComfyUI 失败：{e}") from e
    return name


# ------------------------------------------------------------------
# 能力探测
# ------------------------------------------------------------------
def _node_inputs(name: str) -> dict | None:
    """取单个节点的输入契约；节点不存在返回 None。"""
    try:
        r = requests.get(f"{_base_url()}/object_info/{name}", timeout=15)
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get(name)
        return (data or {}).get("input") if isinstance(data, dict) else None
    except (requests.RequestException, ValueError):
        return None


def _options(node_input: dict | None, key: str) -> list[str]:
    """取下拉选项。

    ComfyUI 有两种返回格式，实测同一台机器上并存：
      旧版（ControlNetLoader / CLIPVisionLoader）: [[\"a.pt\", \"b.pt\"]]
      新版（UpscaleModelLoader）:                  [\"COMBO\", {\"options\": [...]}]
    只认后一种会把「模型明明在、却判成不可用」写进界面，必须都兼容。
    """
    try:
        spec = (node_input or {}).get("required", {}).get(key) or []
        if not isinstance(spec, (list, tuple)) or not spec:
            return []
        first = spec[0]
        if isinstance(first, (list, tuple)):
            return [str(x) for x in first]
        if len(spec) > 1 and isinstance(spec[1], dict):
            return [str(x) for x in (spec[1].get("options") or [])]
    except (IndexError, AttributeError, TypeError):
        pass
    return []


def capabilities() -> dict:
    """真话口径：节点能拉到、模型在列表里才算可用。"""
    ctrl_node = _node_inputs("ControlNetLoader")
    ip_node = _node_inputs("IPAdapterModelLoader")
    clip_node = _node_inputs("CLIPVisionLoader")
    ctrl_models = _options(ctrl_node, "control_net_name")
    ip_models = _options(ip_node, "ipadapter_file")
    clip_models = _options(clip_node, "clip_name")
    return {
        "controlnet": {
            "available": bool(ctrl_node) and _CONTROL_MODEL in ctrl_models,
            "model": _CONTROL_MODEL,
            "models": ctrl_models,
            "node": bool(ctrl_node),
        },
        "ipadapter": {
            "available": bool(ip_node) and _IP_MODEL in ip_models and _CLIP_VISION in clip_models,
            "model": _IP_MODEL,
            "clip_vision": _CLIP_VISION,
            "models": ip_models,
            "clip_models": clip_models,
            "node": bool(ip_node),
        },
    }


# ------------------------------------------------------------------
# 工作流
# ------------------------------------------------------------------
def build_workflow(prompt: str, negative: str = "", width: int = 512,
                   height: int = 512, steps: int = 20, cfg: float = 7.0,
                   seed: int | None = None, sampler: str = "euler",
                   scheduler: str = "normal",
                   control_name: str = "", control_strength: float = 0.8,
                   control_low: float = 0.4, control_high: float = 0.8,
                   ip_name: str = "", ip_weight: float = 0.8) -> dict:
    """构造 API 格式工作流；control_name / ip_name 是 ComfyUI input 里的文件名。"""
    seed = int(seed if seed is not None else int(time.time() * 1000)) % (2 ** 31)
    wf: dict[str, dict] = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": _CKPT}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative or DEFAULT_NEGATIVE, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"images": ["6", 0], "filename_prefix": "bee-img"}},
    }
    positive: list = ["2", 0]
    negative_out: list = ["3", 0]
    model_ref: list = ["1", 0]

    if control_name:
        wf["8"] = {"class_type": "LoadImage", "inputs": {"image": control_name}}
        wf["9"] = {"class_type": "Canny",
                   "inputs": {"image": ["8", 0], "low_threshold": control_low,
                              "high_threshold": control_high}}
        wf["10"] = {"class_type": "ControlNetLoader",
                    "inputs": {"control_net_name": _CONTROL_MODEL}}
        wf["11"] = {"class_type": "ControlNetApplyAdvanced",
                    "inputs": {"positive": positive, "negative": negative_out,
                               "control_net": ["10", 0], "image": ["9", 0],
                               "strength": control_strength,
                               "start_percent": 0.0, "end_percent": 1.0}}
        # 该节点返回 (positive, negative)，对应输出槽 0 / 1
        positive, negative_out = ["11", 0], ["11", 1]

    if ip_name:
        wf["12"] = {"class_type": "LoadImage", "inputs": {"image": ip_name}}
        wf["13"] = {"class_type": "IPAdapterModelLoader",
                    "inputs": {"ipadapter_file": _IP_MODEL}}
        wf["14"] = {"class_type": "CLIPVisionLoader",
                    "inputs": {"clip_name": _CLIP_VISION}}
        wf["15"] = {"class_type": "IPAdapterAdvanced",
                    "inputs": {"model": model_ref, "ipadapter": ["13", 0],
                               "image": ["12", 0], "weight": ip_weight,
                               "weight_type": "linear", "combine_embeds": "concat",
                               "start_at": 0.0, "end_at": 1.0,
                               "embeds_scaling": "V only",
                               "clip_vision": ["14", 0]}}
        model_ref = ["15", 0]

    wf["5"] = {"class_type": "KSampler",
               "inputs": {"model": model_ref, "seed": seed, "steps": steps,
                          "cfg": cfg, "sampler_name": sampler,
                          "scheduler": scheduler, "positive": positive,
                          "negative": negative_out, "latent_image": ["4", 0],
                          "denoise": 1.0}}
    return wf


# ------------------------------------------------------------------
# 显存 / 在线
# ------------------------------------------------------------------
def _require_online(need_mb: int = _MIN_FREE_VRAM_MB) -> None:
    try:
        r = requests.get(f"{_base_url()}/system_stats", timeout=5)
        if r.status_code != 200:
            raise ImageGenError(f"本地 ComfyUI 状态异常（HTTP {r.status_code}）")
    except requests.RequestException as e:
        raise ImageGenError(
            f"本地 ComfyUI 没启动（{_base_url()}）。请先运行 ComfyUI 再出图。原始错误：{e}") from e

    from api.providers.comfyui import _gpu_free_mb, _free_tts_vram
    free = _gpu_free_mb()
    if free is not None and free < need_mb:
        note = _free_tts_vram(need_mb)
        free = _gpu_free_mb()
        if free is not None and free < need_mb:
            raise ImageGenError(
                f"显存不足：当前只剩 {free} MB，这次至少需要 {need_mb} MB"
                f"（{note}）。请先关掉占显卡的程序，或改用纯文生图（512×512 约 1.5GB）。")


def _pick_image(outputs: dict) -> dict | None:
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for item in node_out.get("images") or []:
            if isinstance(item, dict) and item.get("filename"):
                return item
    return None


def _release_models() -> None:
    """出图完把权重请出显存，否则 TTS 下一次合成会因显存不足换页/失败。"""
    try:
        q = requests.get(f"{_base_url()}/queue", timeout=8).json() or {}
        if (q.get("queue_running") or []) or (q.get("queue_pending") or []):
            return
        requests.post(f"{_base_url()}/free",
                      json={"unload_models": True, "free_memory": True}, timeout=30)
    except (requests.RequestException, ValueError):
        pass


# ------------------------------------------------------------------
# 出图
# ------------------------------------------------------------------
def generate(prompt: str, negative: str = "", width: int = 512,
             height: int = 512, steps: int = 20, cfg: float = 7.0,
             seed: int | None = None, sampler: str = "euler",
             scheduler: str = "normal",
             control_path: str = "", control_strength: float = 0.8,
             control_low: float = 0.4, control_high: float = 0.8,
             ip_path: str = "", ip_weight: float = 0.8,
             timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """同步出图：提交 -> 轮询 -> 下载到 data/images/ -> 返回本项目 URL。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ImageGenError("提示词不能为空")
    if not _RUN_LOCK.acquire(blocking=False):
        raise ImageGenError("上一张还在生成中，请等它完成再点（SD1.5 单卡串行）")

    staged: list[Path] = []
    try:
        caps = capabilities()
        if control_path and not caps["controlnet"]["available"]:
            raise ImageGenError(
                f"ControlNet 模型未就绪（需要 {_CONTROL_MODEL}）。"
                "请确认模型在 ComfyUI/models/controlnet 并重启 ComfyUI。")
        if ip_path and not caps["ipadapter"]["available"]:
            raise ImageGenError(
                f"IP-Adapter 未就绪（需要 {_IP_MODEL} + {_CLIP_VISION} 和 IPAdapter 自定义节点）。"
                "请确认模型与节点就位后重启 ComfyUI。")

        need = _MIN_FREE_VRAM_MB
        if control_path:
            need = max(need, _NEED_CONTROL_MB)
        if ip_path:
            need = max(need, _NEED_IP_MB)
        _require_online(need)

        width = _clamp_side(width, 512)
        height = _clamp_side(height, 512)
        steps = max(1, min(50, int(float(steps or 20))))
        cfg = max(1.0, min(20.0, float(cfg or 7.0)))
        control_strength = max(0.0, min(2.0, float(control_strength)))
        ip_weight = max(0.0, min(2.0, float(ip_weight)))

        control_name = ""
        if control_path:
            control_name = _stage_ref(_resolve_ref(control_path))
            staged.append(_comfy_input() / control_name)
        ip_name = ""
        if ip_path:
            ip_name = _stage_ref(_resolve_ref(ip_path))
            staged.append(_comfy_input() / ip_name)

        wf = build_workflow(prompt, negative, width, height, steps, cfg, seed,
                            sampler, scheduler, control_name, control_strength,
                            control_low, control_high, ip_name, ip_weight)
        started = time.time()
        try:
            resp = requests.post(f"{_base_url()}/prompt",
                                 json={"prompt": wf, "client_id": uuid.uuid4().hex},
                                 timeout=30)
        except requests.RequestException as e:
            raise ImageGenError(f"连不上本地 ComfyUI：{e}") from e
        if resp.status_code != 200:
            raise ImageGenError(
                f"ComfyUI 拒绝了工作流（HTTP {resp.status_code}）：{resp.text[:300]}")
        pid = (resp.json() or {}).get("prompt_id")
        if not pid:
            raise ImageGenError("ComfyUI 没返回 prompt_id")

        info = _wait(pid, timeout)
        url, name, size = _download(pid, info)
        meta = {
            "file": name, "url": url, "bytes": size,
            "prompt": prompt, "negative": negative or DEFAULT_NEGATIVE,
            "width": width, "height": height, "steps": steps, "cfg": cfg,
            "seed": wf["5"]["inputs"]["seed"], "sampler": sampler,
            "scheduler": scheduler, "seconds": round(time.time() - started, 1),
            "created_at": datetime.now().isoformat(timespec="seconds"), "job_id": pid,
            "controlnet": {"enabled": bool(control_path), "strength": control_strength,
                           "model": _CONTROL_MODEL if control_path else ""},
            "ipadapter": {"enabled": bool(ip_path), "weight": ip_weight,
                          "model": _IP_MODEL if ip_path else ""},
        }
        try:
            (images_dir() / (Path(name).stem + ".json")).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        _release_models()
        return meta
    finally:
        for p in staged:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        _RUN_LOCK.release()


def _wait(pid: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{_base_url()}/history/{pid}", timeout=15)
            entry = (r.json() or {}).get(pid) if r.status_code == 200 else None
        except (requests.RequestException, ValueError):
            entry = None
        if entry:
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                detail = ""
                for m in status.get("messages") or []:
                    if isinstance(m, (list, tuple)) and len(m) > 1 and m[0] == "execution_error":
                        detail = str((m[1] or {}).get("exception_message") or "")[:300]
                raise ImageGenError(detail or "ComfyUI 执行出错")
            info = _pick_image(entry.get("outputs") or {})
            if info:
                return info
        time.sleep(1.0)
    raise ImageGenError(
        f"出图超时（{timeout} 秒）。ComfyUI 可能还在跑，也可能是显存不足在换页——"
        "请稍后重试或换 512×512。")


def _download(pid: str, info: dict) -> tuple[str, str, int]:
    params = {
        "filename": info.get("filename", ""),
        "subfolder": info.get("subfolder", ""),
        "type": info.get("type", "output"),
    }
    name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{pid[:8]}.png"
    target = images_dir() / name
    try:
        with requests.get(f"{_base_url()}/view", params=params, stream=True,
                          timeout=120) as r:
            if r.status_code != 200:
                raise ImageGenError(f"下载图片失败（HTTP {r.status_code}）")
            with open(target, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        raise ImageGenError(f"下载图片网络错误：{e}") from e
    size = target.stat().st_size
    if size < 1024:
        try:
            target.unlink()
        except OSError:
            pass
        raise ImageGenError("产物异常（小于 1KB），可能是空图")
    return f"/api/images/{name}", name, size


def list_images(limit: int = 24) -> list[dict]:
    """相册：按侧车 JSON 的 created_at 倒序，只列 PNG 还在的。"""
    d = images_dir()
    items: list[dict] = []
    try:
        metas = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return items
    for meta_path in metas[: max(1, min(200, limit * 3))]:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        png = d / str(meta.get("file") or "")
        if not png.is_file():
            continue
        meta["url"] = f"/api/images/{png.name}"
        meta["bytes"] = png.stat().st_size
        items.append(meta)
        if len(items) >= limit:
            break
    return items


def delete_image(name: str) -> bool:
    """删一张图（含侧车）。只接受纯文件名，挡掉路径穿越。"""
    name = str(name or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    d = images_dir()
    png = d / name
    if not png.is_file():
        return False
    try:
        png.unlink()
    except OSError:
        return False
    try:
        (d / (png.stem + ".json")).unlink(missing_ok=True)
    except OSError:
        pass
    return True
