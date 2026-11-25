"""视频 API Provider 抽象基类。所有四家（Jimeng/Kling/Vidu/Wan3.0）都继承它。

真实 endpoint 必须去各家官网查，占位常量是 `XXX_ENDPOINT`。
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class GenRequest:
    """生成请求（所有字段可选，provider 按需取用）。"""
    prompt: str = ""
    image_url: str = ""          # I2V 参考图；空则走 T2V
    duration: float = 5.0        # 输出秒数
    resolution: str = "720p"     # 720p / 1080p
    seed: Optional[int] = None


@dataclass
class GenResult:
    """生成结果。"""
    job_id: str
    status: str = "queued"       # queued / running / succeeded / failed
    video_url: str = ""          # succeeded 时填
    actual_cost: float = 0.0     # 实际结算（¥），能拿到就填
    billing_currency: str = "CNY"
    error: str = ""
    started_at: float = field(default_factory=time.time)


class ProviderError(Exception):
    """provider 层所有异常统一包装。msg 给 UI 显示。"""
    pass


class BaseProvider(ABC):
    """视频 API 统一接口。每家 provider 实现两个方法。"""

    name: str = ""                # "jimeng" / "kling" / "vidu" / "wanx"
    label: str = ""               # 显示给用户："即梦 Jimeng Seedance 2.5"
    max_concurrency: int = 2      # 各家并发上限（即梦 2，阿里云 RPM 300 但单视频 1）

    def __init__(self, api_key: str, **kwargs):
        if not api_key:
            raise ProviderError(f"{self.label or self.name}: 未配置 API Key")
        self.api_key = api_key

    # ------------------------------------------------------------------

    @abstractmethod
    def generate(self, req: GenRequest) -> GenResult:
        """发起生成任务，立刻返回 GenResult（status=queued/running）。
        实现必须在 POST 的同时拿到 job_id（或 task_id）填进去。
        """

    @abstractmethod
    def poll(self, result: GenRequest | GenResult, job_id: str) -> GenResult:
        """轮询一次，返回最新状态。succeeded 时 video_url 必须填。"""

    # ------------------------------------------------------------------
    # 统一辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _poll_loop(do_poll, job_id: str, max_wait: float = 300.0,
                   interval: float = 5.0) -> GenResult:
        """通用阻塞式轮询（给 CLI/单元测试用）。
        do_poll(job_id) -> GenResult。超时或终态（succeeded/failed）返回。
        """
        deadline = time.time() + max_wait
        r = GenResult(job_id=job_id, status="queued")
        while time.time() < deadline:
            r = do_poll(job_id)
            if r.status in ("succeeded", "failed"):
                return r
            time.sleep(interval)
        r.status = "failed"
        r.error = f"超时（>{max_wait:.0f}s）"
        return r
