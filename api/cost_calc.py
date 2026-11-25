"""视频 API 统一成本估算。providers/{jimeng,kling,vidu,wanx} 各自也有 estimate_cost，
但这里集中了一份定价表 + 路由逻辑，panel.py 路由层和前端的"实时成本估算器"都走这里。

定价表以各家官网 2026-11 数据为准，定期手动刷新。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

Provider = Literal["jimeng", "kling", "vidu", "wanx", "comfyui"]
Resolution = Literal["720p", "1080p"]


# -------- 定价表（单位：¥/秒，按【输出秒】计，wanx 特殊） --------
# wanx 的 estimate_cost 单独在 wanx.py 里算（输入+输出双秒），这里只存输出秒基准
_PRICING_TABLE: dict[Provider, dict[Resolution, dict]] = {
    # ---- 火山即梦 Seedance 2.5（¥/输出秒） ----
    "jimeng": {
        "720p": {"output_per_s": 0.28, "mode": "t2v/i2v", "ratio": ["16:9"], "durations": [5, 10, 15, 20]},
        "1080p": {"output_per_s": 0.45, "mode": "t2v/i2v", "ratio": ["16:9"], "durations": [5, 10, 15, 20]},
    },
    # ---- Kling 3.0 Standard（$0.084/s ≈ ¥0.61/s） ----
    # Kling 最长 20s（1080p）或 20s（720p）
    "kling": {
        "720p": {"output_per_s": 0.61, "mode": "t2v/i2v", "ratio": ["16:9"], "durations": [5, 10, 15, 20]},
        "1080p": {"output_per_s": 0.72, "mode": "t2v/i2v", "ratio": ["16:9"], "durations": [5, 10, 15, 20]},
    },
    # ---- Vidu Q3-turbo（高峰 ¥0.40/s，离线半价） ----
    "vidu": {
        "720p": {"output_per_s": 0.40, "mode": "t2v/i2v", "ratio": ["16:9"], "durations": [1, 5, 10, 16], "offline_discount": 0.5},
        "1080p": {"output_per_s": 0.47, "mode": "t2v/i2v", "ratio": ["16:9"], "durations": [1, 5, 10, 16], "offline_discount": 0.5},
    },
    # ---- 阿里云 wan3.0-video（输入+输出双秒） ----
    "wanx": {
        "720p": {"output_per_s": 0.40, "input_per_s": 0.20, "mode": "t2v/i2v", "ratio": ["16:9"], "durations": list(range(5, 31, 5))},
        "1080p": {"output_per_s": 0.60, "input_per_s": 0.30, "mode": "t2v/i2v", "ratio": ["16:9"], "durations": list(range(5, 31, 5))},
    },
    # ---- 本地 ComfyUI（Wan2.1 T2V 1.3B）：零单价，只花自己的电和显卡时间 ----
    "comfyui": {
        "480p": {"output_per_s": 0.0, "mode": "t2v", "ratio": ["16:9"], "durations": [5, 10, 15, 20]},
    },
}

DEFAULT_FX = 7.2
# prompt 折算成输入秒（wanx 用）
PROMPT_CHARS_PER_INPUT_SEC = 20


@dataclass
class CostQuote:
    """一次估算的完整输出（panel.py 路由层 / 前端用这个）。"""
    provider: Provider
    provider_label: str
    resolution: Resolution
    duration: float
    duration_real: float                    # 模型接受的 closest 时长（很多家只支持离散值）
    base_cost: float                        # 基础 ¥
    adjustment: float = 0.0                 # Vidu 离线折扣 / 促销等
    adjustment_label: str = ""              # "Vidu 离线时段 50% off"
    total_cost: float = field(init=False)
    eta_sec: float = 60.0                   # 预估生成耗时（含排队）
    supports_image_input: bool = True
    notes: str = ""

    def __post_init__(self):
        self.total_cost = round((self.base_cost + self.adjustment), 2)


# -------- 查询用 --------
_LABELS: dict[Provider, str] = {
    "jimeng": "即梦 Jimeng Seedance 2.5",
    "kling": "Kling 3.0 Standard",
    "vidu": "Vidu Q3-turbo",
    "wanx": "阿里云 wan3.0-video",
    "comfyui": "本地 ComfyUI（免费 / Wan2.1）",
}


def list_providers() -> list[dict]:
    return [{"name": p, "label": _LABELS[p]} for p in _PRICING_TABLE]


def estimate(provider: Provider, resolution: Resolution, duration: float,
             prompt_len: int = 0, prompt: str = "",
             is_image: bool = False) -> CostQuote:
    """统一估算入口。

    Args:
        provider: 哪家
        resolution: "720p" / "1080p"
        duration: 输出秒数（float 也行，内部会 round）
        prompt_len: prompt 字符数（wanx 折算输入秒用）
        prompt: 完整 prompt（wanx 也用）
        is_image: I2V 还是 T2V（暂不影响计费，但影响可用性）

    Returns:
        CostQuote
    """
    # import here 避免循环依赖
    if provider == "jimeng":
        from .providers.jimeng import JimengProvider
        p = JimengProvider(api_key="FAKE")
        real_dur = _round_to_ceil(duration, _PRICING_TABLE["jimeng"][resolution]["durations"])
        base = p.estimate_cost(real_dur, resolution)
        return CostQuote("jimeng", _LABELS["jimeng"], resolution, duration, real_dur,
                         base, 0.0, "", eta_sec=90.0)

    if provider == "kling":
        from .providers.kling import KlingProvider
        p = KlingProvider(api_key="FAKE")
        real_dur = _round_to_ceil(duration, _PRICING_TABLE["kling"][resolution]["durations"])
        base = p.estimate_cost(real_dur, resolution)
        return CostQuote("kling", _LABELS["kling"], resolution, duration, real_dur,
                         base, 0.0, "", eta_sec=120.0)

    if provider == "vidu":
        from .providers.vidu import ViduProvider
        p = ViduProvider(api_key="FAKE")
        real_dur = _round_to_ceil(duration, _PRICING_TABLE["vidu"][resolution]["durations"])
        base = p.estimate_cost(real_dur, resolution)
        factor = p.current_price_factor
        adj = base * (factor - 1)
        adj_label = f"Vidu 离线时段 50% off" if factor < 1.0 else ""
        return CostQuote("vidu", _LABELS["vidu"], resolution, duration, real_dur,
                         base, adj, adj_label, eta_sec=80.0)

    if provider == "wanx":
        from .providers.wanx import WanxProvider
        p = WanxProvider(api_key="FAKE")
        real_dur = _round_to_ceil(duration, _PRICING_TABLE["wanx"][resolution]["durations"])
        base = p.estimate_cost(real_dur, resolution,
                               prompt_len=prompt_len or len(prompt or ""))
        return CostQuote("wanx", _LABELS["wanx"], resolution, duration, real_dur,
                         base, 0.0, "", eta_sec=150.0,
                         notes="输入秒 + 输出秒双重计费")

    if provider == "comfyui":
        # 本地推理不产生 API 费用；只提示平均耗时（单卡跑 5 秒约 3~8 分钟）
        return CostQuote("comfyui", _LABELS["comfyui"], "480p", duration, duration,
                         0.0, 0.0, "", eta_sec=max(180.0, duration * 60),
                         supports_image_input=False,
                         notes="本地显卡推理，零单价；出片前自动让语音模型卸卡，之后首次合成需重新加载（约半分钟）")

    raise ValueError(f"未知 provider: {provider}")


def compare(resolution: Resolution, duration: float,
            prompt: str = "", is_image: bool = False) -> list[CostQuote]:
    """对比四家，按 total_cost 排序（给前端"帮我选最便宜的"按钮用）。"""
    quotes = []
    for p in _PRICING_TABLE:
        try:
            quotes.append(estimate(p, resolution, duration,
                                   prompt=prompt, is_image=is_image))
        except Exception as e:
            quotes.append(CostQuote(p, _LABELS[p], resolution, duration, duration,
                                    0, 0, "", notes=str(e)[:80]))
    quotes.sort(key=lambda q: q.total_cost)
    return quotes


# -------- utils --------
def _round_to_ceil(target: float, candidates: list[float]) -> float:
    """很多模型只支持离散时长，把用户想要的秒数 UP 到最近的候选值。
    用户说 28s，Kling 只给 30s（不是 10s）。
    target 比所有候选都大 → 返回最大候选；target 比所有候选都小 → 返回最小候选。
    """
    if not candidates:
        return round(target)
    # 找第一个 >= target 的候选（向上取整）
    for c in sorted(candidates):
        if c >= target:
            return c
    # 所有候选都比 target 小 → 返回最大的那个
    return max(candidates)
