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
# 4 步蒸馏 LoRA（SMD，Wan2.1 T2V 1.3B 专用）。25 步 × 8.2s/步 = 205s 纯采样，
# 8GB 卡上永远进不了 1 分钟；4 步 + cfg=1（ComfyUI 在 cfg=1 时跳过反向分支，
# 每步省一半算力）把采样压到 ~16s。
_LORA = os.environ.get("BREEZE_COMFYUI_LORA", "smd_wan21_1.3b_4step_comfy.safetensors")
_TURBO = (os.environ.get("BREEZE_COMFYUI_TURBO", "1") != "0")
# 采样步数：蒸馏档 4 步；关掉 turbo 回到 25 步原生采样
_STEPS = int(os.environ.get("BREEZE_COMFYUI_STEPS") or (4 if _TURBO else 25))
_CFG = float(os.environ.get("BREEZE_COMFYUI_CFG") or (1.0 if _TURBO else 6.0))
_SAMPLER = os.environ.get("BREEZE_COMFYUI_SAMPLER") or ("euler" if _TURBO else "uni_pc")
_SHIFT = float(os.environ.get("BREEZE_COMFYUI_SHIFT") or (10.0 if _TURBO else 8.0))

# ---- Wan2.2 TI2V-5B：带参考图时走这条，T2V+I2V 混合 ----
# 官方模板参数（Comfy-Org/workflow_templates video_wan2_2_5B_ti2v.json）：
# steps=20 / cfg=5 / uni_pc / simple / ModelSamplingSD3 shift=8 / 24fps / 121 帧 = 5 秒。
_UNET_22 = os.environ.get("BREEZE_COMFYUI_UNET_22", "wan2.2_ti2v_5B_fp16.safetensors")
_VAE_22 = os.environ.get("BREEZE_COMFYUI_VAE_22", "wan2.2_vae.safetensors")
_STEPS_22 = int(os.environ.get("BREEZE_COMFYUI_STEPS_22") or 20)
_CFG_22 = float(os.environ.get("BREEZE_COMFYUI_CFG_22") or 5.0)
_SHIFT_22 = float(os.environ.get("BREEZE_COMFYUI_SHIFT_22") or 8.0)
_SAMPLER_22 = os.environ.get("BREEZE_COMFYUI_SAMPLER_22") or "uni_pc"
_FPS_22 = 24.0
# TI2V-5B fp16 权重 10GB，8GB 卡只能按 fp8 载入
_MAX_FRAMES_22 = 121
# T2V 1.3B fp8：实测（2026-11-26）空闲仅 808MB 时 25 帧 / 4 步仍在 158.8 秒跑完，
# 显存充裕时约 35 秒。门槛只拦「真的没空间」，不按「装得下」卡。
_MIN_FREE_VRAM_MB = 700
# 5B 带图出片：fp8 UNET 4768MB + Wan2.2 VAE 1344MB + 文本编码器 6419MB = 12531MB，
# 永远装不进 8151MB，ComfyUI 靠 DynamicVRAM 换页跑。
# 实测（2026-11-26）：
#   空闲 5742MB → 稳定 2.03 秒/步；
#   空闲 1154MB → 8 秒/步，端到端 307 秒（可用）；
#   空闲  742MB → 33.6 秒/步，30 分钟都跑不完 VAE 解码（实测失败）。
# 所以门槛不能按「装得下」来卡（那 8GB 卡永远点不动），但也绝不能低到 742MB 以下
# 那种必然换页卡死的区间。2500MB 在「已知可用值 1154MB」之上留足余量。
_MIN_FREE_VRAM_MB_I2V = 2500

# ---- SVD XT 1.1（快出档，第二个动作方案）----
# 单文件 4.56GB（UNet 1428 / VAE 372 / CLIP-Vision 646 个张量，全 F16），
# 8GB 卡能整份驻留，不用像 5B 那样全程换页。官方 video_svd 模板参数：
# 25 帧（模型训练上限）/ motion_bucket_id=127 / steps=20 / cfg=2.5 /
# euler+karras / VideoLinearCFGGuidance min_cfg=1.0。
_SVD_CKPT = os.environ.get("BREEZE_COMFYUI_SVD", "svd_xt_1_1.safetensors")
_SVD_FRAMES = 25
_SVD_STEPS = int(os.environ.get("BREEZE_COMFYUI_SVD_STEPS") or 20)
_SVD_CFG = float(os.environ.get("BREEZE_COMFYUI_SVD_CFG") or 2.5)
_SVD_MOTION = int(os.environ.get("BREEZE_COMFYUI_SVD_MOTION") or 127)
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


def _free_tts_vram(need_mb: int = _MIN_FREE_VRAM_MB) -> str:
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
        if free is None or free >= need_mb:
            return "已自动暂停语音模型并让出显存"
    return "已请求语音模型卸载，但显存仍未释放"


class ComfyUIProvider(BaseProvider):
    name = "comfyui"
    label = "本地 ComfyUI（免费 / Wan2.1 + Wan2.2）"
    max_concurrency = 1          # 单卡串行，避免显存互抢

    def __init__(self, api_key: str = "", base_url: str = "", **_kw):
        # 本地服务不需要 Key，绕过 BaseProvider 的空 Key 校验
        self.api_key = api_key or ""
        self.base_url = (base_url or os.environ.get("BREEZE_COMFYUI_URL") or _DEFAULT_URL).rstrip("/")

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def generate(self, req: GenRequest) -> GenResult:
        raw_img = str(getattr(req, "image_url", "") or "").strip()
        is_i2v = raw_img.startswith("file:")
        engine = str(getattr(req, "engine", "") or "").strip().lower()
        use_svd = is_i2v and engine == "svd"
        # SVD 没有文本条件：空提示词是合法的，别拿 5B 的规则拦它
        if not req.prompt.strip() and not use_svd:
            raise ProviderError("提示词不能为空")
        if is_i2v:
            # 图生视频要独占大半张卡：先清掉 ComfyUI 自己缓存的上一单权重（去掉
            # --disable-smart-memory 后它会一直把模型留在显存里）。不清的话
            # 实测提交时只剩 742MB，5B 全程换页、30 分钟都出不来。
            self._flush_comfy_cache()
        # 带图走 5B/SVD，显存门槛比 1.3B 高得多
        self._require_online(_MIN_FREE_VRAM_MB_I2V if is_i2v else _MIN_FREE_VRAM_MB)

        if is_i2v:
            # 带参考图：默认 Wan2.2 TI2V-5B（慢但脸稳定），
            # 或 engine=svd 走 SVD XT 1.1（快出档，4.56GB 整份驻留）。
            src = Path(raw_img[5:])
            if not src.is_file():
                raise ProviderError(f"参考图文件不存在：{src}")
            w, h = (1024, 576) if use_svd else self._size_for_22(req.resolution)
            prepared = self._prepare_image(src, w, h)
            try:
                image_name = self._upload_image(prepared)
            finally:
                prepared.unlink(missing_ok=True)   # 临时裁剪图用完即删
            wf = (self._build_workflow_svd(req, image_name) if use_svd
                  else self._build_workflow_22(req, image_name))
        elif raw_img:
            raise ProviderError(
                "本地出片只接受本机参考图（file: 路径）；外链图片请先下载并上传到角色库")
        else:
            # 无参考图：继续用 1.3B 4 步蒸馏档，35 秒出片
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
        result = GenResult(job_id=str(pid), status="running")
        if is_i2v:
            # 记下塞进 ComfyUI input 的参考图副本名，出片结束后删掉（零残留）
            result._bee_staged_image = image_name
        return result

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
            # ComfyUI 的任务队列在内存里：服务一重启（守护自愈、手动重启、崩溃），
            # 我们提交的 prompt 就彻底没了，/history 永远为空。
            # 不识别这种情况的话，前端会傻等满 30 分钟才报超时——用户以为卡死。
            if self._lost(result, job_id):
                self._drop_staged_image(result)
                self._release_if_idle()
                return GenResult(job_id=job_id, status="failed",
                                 error="本地出片服务中途重启过，这次任务已经丢失（不会自动续跑）。"
                                       "请重新点生成。")
            return GenResult(job_id=job_id, status="running")

        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            msgs = status.get("messages") or []
            detail = ""
            for m in msgs:
                if isinstance(m, (list, tuple)) and len(m) > 1 and m[0] == "execution_error":
                    detail = str((m[1] or {}).get("exception_message") or "")[:300]
            self._drop_staged_image(result)
            self._release_if_idle()
            return GenResult(job_id=job_id, status="failed", error=detail or "ComfyUI 执行出错")

        outputs = entry.get("outputs") or {}
        info = self._pick_video(outputs)
        if not info:
            # 完成了但没产物：把 outputs 结构带出来，便于定位
            self._drop_staged_image(result)
            self._release_if_idle()
            return GenResult(job_id=job_id, status="failed",
                             error=f"任务完成但没有视频产物：{str(outputs)[:200]}")

        local_url = self._download(job_id, info)
        self._drop_staged_image(result)
        self._release_if_idle()
        return GenResult(job_id=job_id, status="succeeded", video_url=local_url, actual_cost=0.0)

    def drop_staged(self, name: str) -> None:
        """按文件名删掉上传进 ComfyUI input 的参考图副本（面板侧超时收尾也要用）。"""
        name = str(name or "")
        if not name:
            return
        root = Path(os.environ.get("BREEZE_COMFYUI_INPUT") or "F:/ComfyUI-core/input")
        try:
            (root / name).unlink(missing_ok=True)
        except OSError:
            pass

    def _drop_staged_image(self, result) -> None:
        """删掉上传进 ComfyUI input 目录的参考图副本（出片终态后调用，零残留）。"""
        name = getattr(result, "_bee_staged_image", "")
        if not name:
            return
        result._bee_staged_image = ""
        self.drop_staged(name)

    def release_if_idle(self) -> None:
        """供面板侧在轮询超时等收尾场景调用：队列空了就请 ComfyUI 卸模型。"""
        self._release_if_idle()

    def _flush_comfy_cache(self) -> None:
        """清空 ComfyUI 自己缓存/占用的显存（卸模型 + 释放显存池）。

        去掉 --disable-smart-memory 之后，ComfyUI 会把上一单（如 SD1.5 图片链）
        的权重一直留在显存里；5B 带图出片前不清，提交时只剩几百 MB，
        每步从 2 秒恶化到 33 秒。
        """
        try:
            requests.post(f"{self.base_url}/free",
                          json={"unload_models": True, "free_memory": True}, timeout=60)
        except requests.RequestException:
            pass

    def _release_if_idle(self) -> None:
        """出片完就把 ComfyUI 的模型请出显存。

        实测（2026-11-26）：5B 出片后 ComfyUI 常驻 4.7GB 显存，语音合成被迫
        换页，`test_panel.py` 从 111 秒涨到 500 秒。队列里还有任务时不释放
        （否则下一单要重新加载，批量反而更慢）。
        """
        try:
            q = requests.get(f"{self.base_url}/queue", timeout=10).json() or {}
        except (requests.RequestException, ValueError):
            return
        if (q.get("queue_running") or []) or (q.get("queue_pending") or []):
            return
        self._flush_comfy_cache()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _lost(self, result, job_id: str) -> bool:
        """任务是不是已经丢了（ComfyUI 重启导致）。

        判定：给了足够宽限（刚提交时队列可能还没来得及排进去）之后，
        /history 没有它 **且** /queue 里也没有它，才算丢。
        /queue 查不到（服务正在重启/网络抖动）时不下结论，继续等。
        """
        t0 = getattr(result, "_bee_first_poll", None)
        if t0 is None:
            t0 = time.time()
            try:
                result._bee_first_poll = t0
            except AttributeError:
                pass
        if time.time() - t0 < 90:
            return False
        in_queue = self._queue_has(job_id)
        return in_queue is False

    def _queue_has(self, job_id: str) -> bool | None:
        """ComfyUI 队列里是否有这个 prompt。查不到返回 None（不参与判定）。"""
        try:
            r = requests.get(f"{self.base_url}/queue", timeout=10)
            if r.status_code != 200:
                return None
            data = r.json() or {}
        except (requests.RequestException, ValueError):
            return None
        for key in ("queue_running", "queue_pending"):
            for item in data.get(key) or []:
                # 队列项格式：[number, prompt_id, prompt, extra_data, outputs_to_execute]
                if isinstance(item, (list, tuple)) and len(item) > 1 and str(item[1]) == job_id:
                    return True
        return False

    def _require_online(self, need_mb: int = _MIN_FREE_VRAM_MB) -> None:
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
        if free is not None and free < need_mb:
            # 8GB 卡上语音常驻 ~5.9GB，视频模型放不下：先请语音卸卡再验一次
            note = _free_tts_vram(need_mb)
            free = _gpu_free_mb()
            if free is not None and free < need_mb:
                raise ProviderError(
                    f"显存不足：显卡当前只剩 {free} MB 空闲，这次出片至少需要 "
                    f"{need_mb} MB（{note}）。请关掉占显卡的程序（如游戏、剪辑软件），"
                    "或改用纯文本出片（不带参考图只需 0.7GB，更省显存）。"
                )

    @staticmethod
    def _size_for(resolution: str) -> tuple[int, int]:
        """分辨率档位 -> (width, height)。

        8GB 卡（RTX 5060）实测（2026-11-26）：T2V 640×368 / 25 帧 / 4 步，
        显存充裕约 35 秒，桌面程序占满显存时 158.8 秒。像素越多越慢，
        本地档默认降一档，宁可慢一点也不让人等半小时。
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
        wf = {
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
            "7": {"class_type": "EmptyHunyuanLatentVideo",
                  "inputs": {"width": w, "height": h, "length": length, "batch_size": 1}},
            "8": {"class_type": "KSampler",
                  "inputs": {"model": ["6", 0], "seed": seed, "steps": _STEPS, "cfg": _CFG,
                             "sampler_name": _SAMPLER, "scheduler": "simple",
                             "positive": ["4", 0], "negative": ["5", 0],
                             "latent_image": ["7", 0], "denoise": 1.0}},
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
            "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": 16.0}},
            "11": {"class_type": "SaveVideo",
                   "inputs": {"video": ["10", 0], "filename_prefix": "bee-studio",
                              "format": "mp4", "codec": "h264"}},
        }
        # 蒸馏档：在 UNET 后面挂 4 步 LoRA，再交给 ModelSamplingSD3
        model_src: list = ["1", 0]
        if _TURBO and _LORA:
            wf["12"] = {"class_type": "LoraLoaderModelOnly",
                        "inputs": {"model": ["1", 0], "lora_name": _LORA,
                                   "strength_model": 1.0}}
            model_src = ["12", 0]
        wf["6"] = {"class_type": "ModelSamplingSD3",
                   "inputs": {"model": model_src, "shift": _SHIFT}}
        return wf

    # ------------------------------------------------------------------
    # Wan2.2 TI2V-5B（带参考图时走这条）
    # ------------------------------------------------------------------
    @staticmethod
    def _size_for_22(resolution: str) -> tuple[int, int]:
        """5B 官方档，宽高都要是 32 的倍数：480p=832x480，720p=1280x704。"""
        return (1280, 704) if str(resolution).lower().startswith("720") else (832, 480)

    @staticmethod
    def _length_for_22(duration: float) -> int:
        """秒 -> 帧数。Wan2.2 是 24fps、4n+1，5 秒上限 121 帧。

        向上取整（不是向下）：5 秒要正好落到官方模板的 121 帧，
        向下会得到 117 帧，比用户要的 5 秒少 0.17 秒。
        """
        frames = int(round(float(duration or 5.0) * _FPS_22))
        frames = max(9, min(frames, _MAX_FRAMES_22))
        n = -((1 - frames) // 4)          # ceil((frames-1)/4)
        return min(n * 4 + 1, _MAX_FRAMES_22)

    @staticmethod
    def _prepare_image(src: Path, w: int, h: int) -> Path:
        """中心裁剪到目标比例并缩放到目标尺寸再交给 ComfyUI。

        Wan22ImageToVideoLatent 内部只把 start_image bilinear 拉伸到
        width×height，不裁比例 —— 竖图会被压扁。这里先裁成目标比例；
        再显式缩放到 w×h，避免 Wan 内部用低质量缩放放大参考图。
        """
        import tempfile
        from PIL import Image

        with Image.open(src) as raw:
            im = raw.convert("RGB")
        target = w / h
        iw, ih = im.size
        if iw / ih > target:
            nw = max(1, int(round(ih * target)))
            left = (iw - nw) // 2
            im = im.crop((left, 0, left + nw, ih))
        elif iw / ih < target:
            nh = max(1, int(round(iw / target)))
            top = (ih - nh) // 2
            im = im.crop((0, top, iw, top + nh))
        if im.size != (w, h):
            im = im.resize((w, h), Image.LANCZOS)
        out = Path(tempfile.gettempdir()) / f"bee-ref-{uuid.uuid4().hex[:10]}.png"
        im.save(out, "PNG")
        return out

    def _upload_image(self, path: Path) -> str:
        """把参考图塞进 ComfyUI 的 input 目录，返回 LoadImage 能用的名字。"""
        try:
            with open(path, "rb") as fh:
                resp = requests.post(
                    f"{self.base_url}/upload/image",
                    files={"image": (path.name, fh, "image/png")},
                    data={"overwrite": "true"},
                    timeout=60,
                )
        except requests.RequestException as e:
            raise ProviderError(f"参考图上传失败：{e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"参考图上传失败（{resp.status_code}）：{resp.text[:200]}")
        try:
            info = resp.json()
        except Exception:
            raise ProviderError(f"参考图上传返回异常：{resp.text[:200]}")
        name = str(info.get("name") or "")
        sub = str(info.get("subfolder") or "")
        if not name:
            raise ProviderError(f"参考图上传返回没有 name：{info}")
        return f"{sub}/{name}" if sub else name

    def _build_workflow_22(self, req: GenRequest, image_name: str) -> dict:
        """按 ComfyUI 官方 5B 模板拼工作流。

        参数来自官方模板 video_wan2_2_5B_ti2v.json：
        steps=20 / cfg=5 / uni_pc / simple / shift=8 / 24fps。
        """
        w, h = self._size_for_22(req.resolution)
        length = self._length_for_22(req.duration)
        seed = int(req.seed if req.seed is not None else time.time()) % (2 ** 31)
        return {
            "1": {"class_type": "UNETLoader",
                  # fp16 权重 10GB，8GB 卡必须按 fp8 载入
                  "inputs": {"unet_name": _UNET_22, "weight_dtype": "fp8_e4m3fn"}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": _TEXT_ENCODER, "type": "wan", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": _VAE_22}},
            "4": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": req.prompt, "clip": ["2", 0]}},
            "5": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": _NEGATIVE, "clip": ["2", 0]}},
            "12": {"class_type": "LoadImage", "inputs": {"image": image_name}},
            "7": {"class_type": "Wan22ImageToVideoLatent",
                  "inputs": {"vae": ["3", 0], "width": w, "height": h,
                             "length": length, "batch_size": 1,
                             "start_image": ["12", 0]}},
            "6": {"class_type": "ModelSamplingSD3",
                  "inputs": {"model": ["1", 0], "shift": _SHIFT_22}},
            "8": {"class_type": "KSampler",
                  "inputs": {"model": ["6", 0], "seed": seed, "steps": _STEPS_22,
                             "cfg": _CFG_22, "sampler_name": _SAMPLER_22,
                             "scheduler": "simple",
                             "positive": ["4", 0], "negative": ["5", 0],
                             "latent_image": ["7", 0], "denoise": 1.0}},
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
            "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": _FPS_22}},
            "11": {"class_type": "SaveVideo",
                   "inputs": {"video": ["10", 0], "filename_prefix": "bee-studio-5b",
                              "format": "mp4", "codec": "h264"}},
        }

    def _build_workflow_svd(self, req: GenRequest, image_name: str) -> dict:
        """SVD XT 1.1 快出档（官方 video_svd 模板）。

        25 帧是 SVD 的训练上限，播放帧率按请求时长反推（5 秒 → 5fps），
        避免「要 5 秒只给 4.17 秒」的货不对板。SVD 没有文本条件，
        prompt 只用于界面展示，不进工作流。
        """
        # 上限与路由器的 5 秒红线保持一致：真放进来超 5 秒的请求，也不许静默改短
        dur = max(1.0, min(float(req.duration or 5.0), 5.0))
        # 25 帧 / 时长 = 播放帧率；上限 24fps，否则 1 秒的请求会被拉成 3 秒
        fps = max(1, min(24, int(round(_SVD_FRAMES / dur))))
        seed = int(req.seed if req.seed is not None else time.time()) % (2 ** 31)
        return {
            "1": {"class_type": "ImageOnlyCheckpointLoader",
                  "inputs": {"ckpt_name": _SVD_CKPT}},
            "2": {"class_type": "LoadImage", "inputs": {"image": image_name}},
            # clip_vision / vae 都从同一个 checkpoint 拆出来（槽位 1 / 2）
            "3": {"class_type": "SVD_img2vid_Conditioning",
                  "inputs": {"clip_vision": ["1", 1], "init_image": ["2", 0],
                             "vae": ["1", 2], "width": 1024, "height": 576,
                             "video_frames": _SVD_FRAMES,
                             "motion_bucket_id": _SVD_MOTION,
                             "fps": fps, "augmentation_level": 0.0}},
            "4": {"class_type": "VideoLinearCFGGuidance",
                  "inputs": {"model": ["1", 0], "min_cfg": 1.0}},
            "5": {"class_type": "KSampler",
                  "inputs": {"model": ["4", 0], "seed": seed, "steps": _SVD_STEPS,
                             "cfg": _SVD_CFG, "sampler_name": "euler",
                             "scheduler": "karras",
                             "positive": ["3", 0], "negative": ["3", 1],
                             "latent_image": ["3", 2], "denoise": 1.0}},
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            "7": {"class_type": "CreateVideo",
                  "inputs": {"images": ["6", 0], "fps": float(fps)}},
            "8": {"class_type": "SaveVideo",
                  "inputs": {"video": ["7", 0], "filename_prefix": "bee-studio-svd",
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
