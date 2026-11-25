"""Vidu —— 离线时段半价特色。高峰 $0.055/s ≈ ¥0.40/s，离线仅 40-55%。

真实 endpoint 去 https://platform.vidu.com/docs/pricing 查。
占位常量: VIDU_ENDPOINT_T2V / VIDU_ENDPOINT_QUERY
"""
from __future__ import annotations
import time, datetime
import requests
from . import BaseProvider, GenRequest, GenResult, ProviderError

VIDU_ENDPOINT_T2V = "https://api.vidu.com/v1/video/generations"
VIDU_ENDPOINT_QUERY = "https://api.vidu.com/v1/video/generations/{task_id}"

# 高峰/离线分界（北京时间 23:00 ~ 次日 9:00 = 离线）
_OFFLINE_START = 23
_OFFLINE_END = 9


def _is_offline_hour() -> bool:
    """判断现在是不是 Vidu 离线时段（北京时间）。"""
    bj_hour = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).hour
    return bj_hour >= _OFFLINE_START or bj_hour < _OFFLINE_END


class ViduProvider(BaseProvider):
    name = "vidu"
    label = "Vidu Q3-turbo"
    max_concurrency = 4

    # 高峰价（USD/s）：$0.055 / 720p，$0.065 / 1080p
    _PRICING_USD_PER_S = {"720p": 0.055, "1080p": 0.065}
    FX = 7.2

    def estimate_cost(self, duration: float, resolution: str) -> float:
        base = float(self._PRICING_USD_PER_S.get(resolution.lower(), 0.055)) * self.FX * duration
        if _is_offline_hour():
            base *= 0.5          # 离线时段半价
        return base

    @property
    def current_price_factor(self) -> float:
        """当前价格系数（实时显示在成本估算器里）。"""
        return 0.5 if _is_offline_hour() else 1.0

    def generate(self, req: GenRequest) -> GenResult:
        payload = {
            "model": "vidu-q3-turbo",
            "prompt": req.prompt,
            "duration": int(req.duration) if req.duration in (1, 5, 10, 16) else 5,
            "aspect_ratio": "16:9",
        }
        if req.image_url:
            payload["reference_images"] = [req.image_url]
        try:
            resp = requests.post(VIDU_ENDPOINT_T2V,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload, timeout=15)
            if resp.status_code == 429:
                raise ProviderError("Vidu 并发超限（最多 4 个）")
            if not resp.ok:
                raise ProviderError(f"Vidu {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            task_id = data.get("id") or (data.get("data") or {}).get("id")
            if not task_id:
                raise ProviderError(f"Vidu 无 task_id: {data}")
            return GenResult(job_id=str(task_id), status="queued")
        except requests.RequestException as e:
            raise ProviderError(f"Vidu 网络错误: {e}") from e

    def poll(self, result, job_id: str) -> GenResult:
        try:
            resp = requests.get(VIDU_ENDPOINT_QUERY.format(task_id=job_id),
                                headers={"Authorization": f"Bearer {self.api_key}"}, timeout=10)
            if not resp.ok:
                raise ProviderError(f"Vidu 查询 {resp.status_code}")
            data = resp.json()
            d = data.get("data", data)
            st = d.get("status", "queued")
            r = GenResult(job_id=job_id, status=st)
            if st == "succeeded":
                videos = d.get("videos") or []
                if videos:
                    r.video_url = videos[0].get("url", "")
            elif st == "failed":
                r.error = d.get("error", {}).get("message", "未知错误")[:200]
            return r
        except requests.RequestException as e:
            raise ProviderError(f"Vidu 查询网络错误: {e}") from e
