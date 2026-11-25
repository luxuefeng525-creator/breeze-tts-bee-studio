"""火山引擎 即梦 Seedance 2.5 —— 四家里最便宜的中文优化模型。

真实 endpoint 必须去 https://www.volcengine.com/docs/85621/1544715 查。
占位常量: VOLCENDPOINT_T2V / VOLCENDPOINT_I2V / VOLCENDPOINT_QUERY / VOLCENDPOINT_ASSET
"""
from __future__ import annotations
import json, time
from dataclasses import asdict
import requests
from . import BaseProvider, GenRequest, GenResult, ProviderError

# ⚠️ 去火山引擎控制台拿，别瞎填：
VOLC_ACCESSKEY = "YOUR_VOLC_ACCESSKEY"
VOLC_SECRETKEY = "YOUR_VOLC_SECRETKEY"
# Seedance 2.5 model endpoint（火山引擎方舟/百炼）
VOLC_ENDPOINT_T2V = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
VOLC_ENDPOINT_QUERY = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{task_id}"


class JimengProvider(BaseProvider):
    name = "jimeng"
    label = "即梦 Jimeng Seedance 2.5"
    max_concurrency = 2        # 火山即梦单账号并发上限 2（官方案例："并发请求不要超过 2 个"）

    # 官方定价（¥/s）—— 720p 最低档限时 ¥0.28/s
    _PRICING = {
        "720p": 0.28,           # Seedance 2.5 720P Turbo（限时折扣后）
        "1080p": 0.45,          # Seedance 2.5 1080P
    }

    def __init__(self, api_key: str, **kwargs):
        super().__init__(api_key)

    # --------------------------------------------------------------
    def estimate_cost(self, duration: float, resolution: str) -> float:
        """即梦按【输出秒】计费。"""
        r = resolution.lower().replace(" ", "")
        return float(self._PRICING.get(r, self._PRICING["720p"])) * duration

    # --------------------------------------------------------------
    def generate(self, req: GenRequest) -> GenResult:
        payload = {
            "model": "seedance-2.5-t2v-720p" if req.resolution == "720p" else "seedance-2.5-t2v-1080p",
            "content": [{"type": "text", "text": req.prompt}],
            "duration": int(req.duration) if req.duration <= 5 else 5,  # Seedance 2.5 默认 5s，支持 5s/10s
            "ratio": "16:9",
            "resolution": req.resolution,
        }
        if req.image_url:
            payload["content"].insert(0, {"type": "image_url", "image_url": {"url": req.image_url}})
            payload["model"] = "seedance-2.5-i2v-720p" if req.resolution == "720p" else "seedance-2.5-i2v-1080p"
        try:
            resp = requests.post(
                VOLC_ENDPOINT_T2V,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=15,
            )
            if resp.status_code == 429:
                raise ProviderError("即梦并发超限（最多 2 个），请等其他任务结束")
            if not resp.ok:
                raise ProviderError(f"即梦 {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            task_id = data.get("task_id") or data.get("id")
            if not task_id:
                raise ProviderError(f"即梦响应没有 task_id: {data}")
            return GenResult(job_id=str(task_id), status="queued")
        except requests.RequestException as e:
            raise ProviderError(f"即梦网络错误: {e}") from e

    def poll(self, result: GenRequest | GenResult, job_id: str) -> GenResult:
        try:
            resp = requests.get(
                VOLC_ENDPOINT_QUERY.format(task_id=job_id),
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10,
            )
            if not resp.ok:
                raise ProviderError(f"即梦查询 {resp.status_code}")
            data = resp.json()
            status_map = {"queued": "queued", "running": "running", "succeeded": "succeeded", "failed": "failed"}
            st = data.get("status", "queued")
            r = GenResult(job_id=job_id, status=status_map.get(st, st))
            if st == "succeeded":
                items = (data.get("content") or [])
                for it in items:
                    if it.get("type") == "video_url":
                        r.video_url = it.get("video_url", {}).get("url", "")
                        break
                # 火山方舟计费通常在响应里有 cost 字段
                r.actual_cost = float(data.get("usage", {}).get("input_tokens", 0)
                                      or data.get("cost", 0))
            elif st == "failed":
                r.error = data.get("error", {}).get("message", "未知错误")[:200]
            return r
        except requests.RequestException as e:
            raise ProviderError(f"即梦查询网络错误: {e}") from e
