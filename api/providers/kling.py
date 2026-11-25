"""快手 Kling 3.0/4.0 —— 开发者首选，图像质量最高。

真实 endpoint 必须去 https://kling.ai/dev/pricing 查。
占位常量: KLING_ENDPOINT_T2V / KLING_ENDPOINT_I2V / KLING_ENDPOINT_QUERY
"""
from __future__ import annotations
import requests
from . import BaseProvider, GenRequest, GenResult, ProviderError

# ⚠️ 去快手 Kling 开放平台拿：
KLING_ENDPOINT_T2V = "https://api.klingai.com/v1/videos/text2video"
KLING_ENDPOINT_I2V = "https://api.klingai.com/v1/videos/image2video"
KLING_ENDPOINT_QUERY = "https://api.klingai.com/v1/videos/{task_id}"


class KlingProvider(BaseProvider):
    name = "kling"
    label = "Kling 3.0 Standard"
    max_concurrency = 2

    # $0.084/s ≈ ¥0.61/s（按 1 USD = ¥7.2）
    _PRICING_USD_PER_S = {"720p": 0.084, "1080p": 0.10}
    FX = 7.2

    def estimate_cost(self, duration: float, resolution: str) -> float:
        return float(self._PRICING_USD_PER_S.get(resolution.lower(), 0.084)) * self.FX * duration

    def _auth_headers(self, json_body: bool = False) -> dict:
        """官方 Kling 用 Authorization: Bearer <JWT>；第三方代理多用 apikey 头。
        按 Key 形态自动选择，避免把 JWT 塞进 apikey 头被服务器判 401。"""
        k = str(self.api_key or "")
        h = {"Content-Type": "application/json"} if json_body else {}
        if k.startswith("eyJ") and k.count(".") == 2:
            h["Authorization"] = f"Bearer {k}"
        else:
            h["apikey"] = k
        return h

    def generate(self, req: GenRequest) -> GenResult:
        endpoint = KLING_ENDPOINT_I2V if req.image_url else KLING_ENDPOINT_T2V
        payload = {
            "model_name": "kling-v1-6",
            "prompt": req.prompt,
            "image": req.image_url,
            "mode": "std",          # std 即 standard
            "duration": int(req.duration) if req.duration in (5, 10) else 5,
            "aspect_ratio": "16:9",
        }
        try:
            resp = requests.post(
                endpoint, headers=self._auth_headers(json_body=True),
                json=payload, timeout=15,
            )
            if resp.status_code == 429:
                raise ProviderError("Kling 并发超限（最多 2 个）")
            if not resp.ok:
                raise ProviderError(f"Kling {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            task_id = (data.get("data") or {}).get("task_id")
            if not task_id:
                raise ProviderError(f"Kling 无 task_id: {data}")
            return GenResult(job_id=task_id, status="queued")
        except requests.RequestException as e:
            raise ProviderError(f"Kling 网络错误: {e}") from e

    def poll(self, result, job_id: str) -> GenResult:
        try:
            resp = requests.get(
                KLING_ENDPOINT_QUERY.format(task_id=job_id),
                headers=self._auth_headers(), timeout=10,
            )
            if not resp.ok:
                raise ProviderError(f"Kling 查询 {resp.status_code}")
            data = resp.json()
            d = data.get("data", {})
            raw = str(d.get("task_status") or "").lower()
            # Kling 返回 succeed/processing/submitted；路由只认 succeeded/failed，
            # 不归一化会导致轮询永不结束、300s 后误判失败。
            _map = {"succeed": "succeeded", "success": "succeeded", "succeeded": "succeeded",
                    "failed": "failed", "error": "failed",
                    "processing": "running", "submitted": "queued", "queued": "queued"}
            r = GenResult(job_id=job_id, status=_map.get(raw, raw or "queued"))
            if r.status == "succeeded":
                videos = d.get("task_result", {}).get("videos", [])
                if videos:
                    r.video_url = videos[0].get("url", "")
            elif r.status == "failed":
                r.error = d.get("task_status_msg", "未知错误")[:200]
            return r
        except requests.RequestException as e:
            raise ProviderError(f"Kling 查询网络错误: {e}") from e
