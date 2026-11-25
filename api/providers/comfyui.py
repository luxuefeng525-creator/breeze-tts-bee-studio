"""本地 ComfyUI provider —— 用 Wan2.1 T2V 1.3B 在本机 GPU 免费出片。

为什么要它：四家云端 provider（Kling/即梦/Vidu/万相）全部要先充值才有额度，
客户没 Key 就出不了片。本地 ComfyUI 用开源模型跑在自己显卡上，零单价，
用来兜底「没有预算也要能出片」的场景。

依赖：本机另起一个 ComfyUI 服务（默认 http://127.0.0.1:8188），
模型三件套放在 ComfyUI/models/{diffusion_models,text_encoders,vae} 下。

协议（ComfyUI 原生 HTTP API，不是各家云的私有协议）：
  POST /prompt            body {"prompt": <API 格式 workflow>}  -> {"prompt_id": "..."}
  GET  /history/{id}      未完成返回 {}；完成返回 {id: {status, outputs}}
  GET  /view?filename=&subfolder=&type=output                    -> 视频字节流

注意：视频 URL 是 ComfyUI 自己管理的临时路径，这里会立刻下载到
data/videos/ 并返回本项目自己的 /api/videos/<name>，不把外部路径透给前端。
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

import requests

from . import BaseProvider, GenRequest, GenResult, ProviderError

_DEFAULT_URL = "http://127.0.0.1:8188"
_UNET = "wan2.1_t2v_1.3B_fp16.safetensors"
_TEXT_ENCODER = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
_VAE = "wan_2.1_vae.safetensors"
# 采样步数：30 步在 8GB 卡上单条 5 秒要约 15 分钟，25 步是质量/耗时折中
_STEPS = int(os.environ.get("BREEZE_COMFYUI_STEPS") or 25)
# 低于这个空闲显存就别开跑：模型 1.4GB + 激活，剩太少会一路换页，跑 30 分钟还不一定出得来
_MIN_FREE_VRAM_MB = 3500
# 语音合成服务（7860）的「卸下模型」接口：8GB 卡上 TTS 常驻约 5.9GB，
# 出片前先让它卸卡，出完片用户的下一句合成会自动重载。
_TTS_UNLOAD_URL = (os.environ.get("BREEZE_TTS_UNLOAD_URL")
                   or "http://127.0.0.1:7860/v1/model/unload")

# Wan2.1 官方推荐的负面提示词（中文），用于压制常见畸形/水印/静止画面
_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，"
    "最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
    "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
    "杂乱的背景，三条腿，背景人很多，倒着走"
)


def _videos_dir() -> Path:
    """落地目录：data/videos/（与 /api/videos/ 静态路由一一对应）。"""
    root = Path(os.environ.get("BREEZE_DATA") or (Path(__file__).resolve().parent.parent.parent / "data"))
    d = root / "videos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gpu_free_mb() -> int | None:
    """整卡空闲显存（MB）。

    不能用 ComfyUI 的 /system_stats：那个是它自己 CUDA 上下文的上限，
    别的进程（语音合成）已经吃掉的显存它看不见，会报「7GB 空闲」骗人。
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        line = (out.stdout or "").strip().splitlines()[0]
        return int(line.strip())
    except Exception:
        return None


def _free_tts_vram() -> str:
    """请语音合成服务卸下模型，把显存让出来。

    返回一句人话说明（用于拼进错误文案），成功与否都返回，不抛异常：
    语音服务没启动本来就不该拦住出片。
    """
    try:
        r = requests.post(_TTS_UNLOAD_URL, timeout=10)
    except requests.RequestException as e:
        return f"语音服务未响应（{e.__class__.__name__}），无法自动让出显存"
    if r.status_code == 409:
        return "语音服务正在合成，暂时无法让出显存"
    if r.status_code != 200:
        return f"语音服务拒绝卸下模型（HTTP {r.status_code}）"
    # empty_cache 之后驱动释放有延迟，轮询到够用为止
    for _ in range(20):
        time.sleep(0.5)
        free = _gpu_free_mb()
        if free is None or free >= _MIN_FREE_VRAM_MB:
            return "已自动暂停语音模型并让出显存"
    return "已请求语音模型卸载，但显存仍未释放"


class ComfyUIProvider(BaseProvider):
    name = "comfyui"
    label = "本地 ComfyUI（免费 / Wan2.1）"
    max_concurrency = 1          # 单卡串行，避免显存互抢

    def __init__(self, api_key: str = "", base_url: str = "", **_kw):
        # 本地服务不需要 Key，绕过 BaseProvider 的空 Key 校验
        self.api_key = api_key or ""
        self.base_url = (base_url or os.environ.get("BREEZE_COMFYUI_URL") or _DEFAULT_URL).rstrip("/")

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def generate(self, req: GenRequest) -> GenResult:
        if not req.prompt.strip():
            raise ProviderError("提示词不能为空")
        self._require_online()

        wf = self._build_workflow(req)
        try:
            resp = requests.post(f"{self.base_url}/prompt",
                                 json={"prompt": wf, "client_id": uuid.uuid4().hex},
                                 timeout=30)
        except requests.RequestException as e:
            raise ProviderError(f"连不上本地 ComfyUI（{self.base_url}）：{e}") from e
        if resp.status_code != 200:
            # ComfyUI 校验失败会把具体哪个节点哪一项不对写在 body 里，原样透出去
            raise ProviderError(f"ComfyUI 拒绝了工作流（{resp.status_code}）：{resp.text[:400]}")
        data = resp.json()
        pid = data.get("prompt_id")
        if not pid:
            raise ProviderError(f"ComfyUI 没返回 prompt_id：{data}")
        return GenResult(job_id=str(pid), status="running")

    def poll(self, result, job_id: str) -> GenResult:
        try:
            resp = requests.get(f"{self.base_url}/history/{job_id}", timeout=20)
        except requests.RequestException as e:
            raise ProviderError(f"查询 ComfyUI 失败：{e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"查询 ComfyUI 返回 {resp.status_code}")
        data = resp.json() or {}
        entry = data.get(job_id)
        if not entry:
            return GenResult(job_id=job_id, status="running")

        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            msgs = status.get("messages") or []
            detail = ""
            for m in msgs:
                if isinstance(m, (list, tuple)) and len(m) > 1 and m[0] == "execution_error":
                    detail = str((m[1] or {}).get("exception_message") or "")[:300]
            return GenResult(job_id=job_id, status="failed", error=detail or "ComfyUI 执行出错")

        outputs = entry.get("outputs") or {}
        info = self._pick_video(outputs)
        if not info:
            # 完成了但没产物：把 outputs 结构带出来，便于定位
            return GenResult(job_id=job_id, status="failed",
                             error=f"任务完成但没有视频产物：{str(outputs)[:200]}")

        local_url = self._download(job_id, info)
        return GenResult(job_id=job_id, status="succeeded", video_url=local_url, actual_cost=0.0)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _require_online(self) -> None:
        try:
            r = requests.get(f"{self.base_url}/system_stats", timeout=5)
            if r.status_code != 200:
                raise ProviderError(f"本地 ComfyUI 状态异常（{r.status_code}）")
        except requests.RequestException as e:
            raise ProviderError(
                f"本地 ComfyUI 没启动（{self.base_url}）。请先运行 ComfyUI，"
                f"或改用云端 provider。原始错误：{e}"
            ) from e
        free = _gpu_free_mb()
        if free is not None and free < _MIN_FREE_VRAM_MB:
            # 8GB 卡上语音常驻 ~5.9GB，视频模型放不下：先请语音卸卡再验一次
            note = _free_tts_vram()
            free = _gpu_free_mb()
            if free is not None and free < _MIN_FREE_VRAM_MB:
                raise ProviderError(
                    f"显存不足：显卡当前只剩 {free} MB 空闲，本地出片至少需要 "
                    f"{_MIN_FREE_VRAM_MB} MB（{note}）。请关掉其它占显卡的程序"
                    "（如游戏、剪辑软件），或改用云端 provider。"
                )

    @staticmethod
    def _size_for(resolution: str) -> tuple[int, int]:
        """分辨率档位 -> (width, height)。

        8GB 卡（RTX 5060）实测：832x480 单条 5 秒约 15 分钟，640x368 约 6 分钟，
        耗时随像素量线性涨。本地档默认降一档，宁可慢一点也不能让人等半小时。
        """
        r = (resolution or "").lower()
        if r == "720p":
            return 832, 480
        # 368 = 23*16，Wan 要求 16 的倍数，且最接近 16:9
        return 640, 368

    @staticmethod
    def _length_for(duration: float) -> int:
        """秒 -> 帧数。Wan 要求 4n+1，16fps。"""
        frames = int(round(float(duration or 5.0) * 16))
        frames = max(17, min(frames, 121))
        return ((frames - 1) // 4) * 4 + 1

    def _build_workflow(self, req: GenRequest) -> dict:
        w, h = self._size_for(req.resolution)
        length = self._length_for(req.duration)
        seed = int(req.seed if req.seed is not None else time.time()) % (2 ** 31)
        return {
            "1": {"class_type": "UNETLoader",
                  # fp8 权重：fp16 在 8GB 卡上跑 81 帧会被迫来回换页，
                  # 实测 fp16 45~88s/步，fp8 降到 20~30s/步
                  "inputs": {"unet_name": _UNET, "weight_dtype": "fp8_e4m3fn"}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": _TEXT_ENCODER, "type": "wan", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": _VAE}},
            "4": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": req.prompt, "clip": ["2", 0]}},
            "5": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": _NEGATIVE, "clip": ["2", 0]}},
            "6": {"class_type": "ModelSamplingSD3",
                  "inputs": {"model": ["1", 0], "shift": 8.0}},
            "7": {"class_type": "EmptyHunyuanLatentVideo",
                  "inputs": {"width": w, "height": h, "length": length, "batch_size": 1}},
            "8": {"class_type": "KSampler",
                  "inputs": {"model": ["6", 0], "seed": seed, "steps": _STEPS, "cfg": 6.0,
                             "sampler_name": "uni_pc", "scheduler": "simple",
                             "positive": ["4", 0], "negative": ["5", 0],
                             "latent_image": ["7", 0], "denoise": 1.0}},
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
            "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": 16.0}},
            "11": {"class_type": "SaveVideo",
                   "inputs": {"video": ["10", 0], "filename_prefix": "bee-studio",
                              "format": "mp4", "codec": "h264"}},
        }

    @staticmethod
    def _pick_video(outputs: dict) -> dict | None:
        """在 outputs 里找 SaveVideo 产出的那个文件（history 里挂在 images 键下）。"""
        for node_out in outputs.values():
            if not isinstance(node_out, dict):
                continue
            for item in node_out.get("images") or []:
                if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(".mp4"):
                    return item
        # 兜底：有些版本把产物挂在 "gifs"/"video" 键
        for node_out in outputs.values():
            if not isinstance(node_out, dict):
                continue
            for key in ("gifs", "video"):
                for item in node_out.get(key) or []:
                    if isinstance(item, dict) and item.get("filename"):
                        return item
        return None

    def _download(self, job_id: str, info: dict) -> str:
        """把 ComfyUI 的产物拉回本地 data/videos/，返回本项目可用的 URL。"""
        params = {
            "filename": info.get("filename", ""),
            "subfolder": info.get("subfolder", ""),
            "type": info.get("type", "output"),
        }
        try:
            with requests.get(f"{self.base_url}/view", params=params, stream=True, timeout=120) as r:
                if r.status_code != 200:
                    raise ProviderError(f"下载产物失败（{r.status_code}）：{params}")
                name = f"{job_id}.mp4"
                target = _videos_dir() / name
                with open(target, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if chunk:
                            f.write(chunk)
        except requests.RequestException as e:
            raise ProviderError(f"下载产物网络错误：{e}") from e
        if target.stat().st_size < 1024:
            raise ProviderError("产物文件异常（小于 1KB），可能是空视频")
        return f"/api/videos/{name}"
