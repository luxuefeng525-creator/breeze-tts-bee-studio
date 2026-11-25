"""阿里云百炼 wan3.0-video / wan3.0-video-prime —— 唯一支持 30s 长视频。

⚠️ 计费规则极其特殊：【输入秒 + 输出秒】双重计费。
  即不是只算输出视频有多少秒，是 "输入时长 × P_in + 输出时长 × P_out"。
  详见 https://help.aliyun.com/zh/model-studio/wan3-0-video —— "计费方式" 章节。

真实 endpoint 去阿里云百炼拿。
占位常量: WANX_ENDPOINT_T2V / WANX_ENDPOINT_QUERY
"""
from __future__ import annotations
import requests
from . import BaseProvider, GenRequest, GenResult, ProviderError

WANX_ENDPOINT_T2V = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2video/video-generation"
WANX_ENDPOINT_QUERY = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"


class WanxProvider(BaseProvider):
    name = "wanx"
    label = "阿里云 wan3.0-video"
    max_concurrency = 1           # 百炼单视频生成并发 1

    # 官方案例价格（¥/秒）—— 输入秒 ≈ 0.20、输出秒 ≈ 0.40（720p）
    # 1080p 约 1.5x
    _PRICING = {
        "720p":  {"input_per_s": 0.20, "output_per_s": 0.40},
        "1080p": {"input_per_s": 0.30, "output_per_s": 0.60},
    }
    # 默认把 prompt 字数折算成输入秒（粗略：20 字 = 1s，或用户显式传 input_sec）
    PROMPT_CHARS_PER_INPUT_SEC = 20

    def estimate_cost(self, duration: float, resolution: str,
                      input_sec: float | None = None,
                      prompt_len: int = 0) -> float:
        """wan3.0 特殊计费：input_sec × P_in + duration × P_out。"""
        r = resolution.lower().replace(" ", "")
        t = self._PRICING.get(r, self._PRICING["720p"])
        # 如果没有显式 input_sec，用 prompt 字数折算（兜底 2s）
        in_s = input_sec if input_sec is not None else max(2.0, prompt_len / self.PROMPT_CHARS_PER_INPUT_SEC)
        return in_s * t["input_per_s"] + duration * t["output_per_s"]

    def generate(self, req: GenRequest) -> GenResult:
        model = "wanx-v1" if req.resolution == "720p" else "wanx-v1-pro"
        payload = {
            "model": model,
            "input": {"prompt": req.prompt},
            "parameters": {
                "size": f"1280*720" if req.resolution == "720p" else "1920*1080",
                "duration": int(req.duration),
            },
        }
        if req.image_url:
            payload["input"]["image"] = req.image_url
        try:
            resp = requests.post(WANX_ENDPOINT_T2V,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                         "X-DashScope-Async": "enable"},   # 百炼异步任务必须 header
                json=payload, timeout=15)
            if resp.status_code == 429:
                raise ProviderError("wan3.0 单账号并发超限（1 个），请等其他任务结束")
            if not resp.ok:
                raise ProviderError(f"wan3.0 {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            task_id = data.get("output", {}).get("task_id") or data.get("task_id")
            if not task_id:
                raise ProviderError(f"wan3.0 无 task_id: {data}")
            return GenResult(job_id=str(task_id), status="queued")
        except requests.RequestException as e:
            raise ProviderError(f"wan3.0 网络错误: {e}") from e

    def poll(self, result, job_id: str) -> GenResult:
        try:
            resp = requests.get(WANX_ENDPOINT_QUERY.format(task_id=job_id),
                                headers={"Authorization": f"Bearer {self.api_key}"}, timeout=10)
            if not resp.ok:
                raise ProviderError(f"wan3.0 查询 {resp.status_code}")
            data = resp.json()
            o = data.get("output", {})
            st = o.get("task_status", "QUEUED")
            st_map = {"QUEUED": "queued", "RUNNING": "running", "SUCCEEDED": "succeeded", "FAILED": "failed"}
            r = GenResult(job_id=job_id, status=st_map.get(st, st))
            if st == "SUCCEEDED":
                videos = o.get("results", [])
                if videos:
                    r.video_url = videos[0].get("url", "")
                usage = data.get("usage", {}) or {}
                # 百炼计费会回写 usage.total，单位 ¥
                r.actual_cost = float(usage.get("total", 0) or usage.get("amount", 0))
            elif st == "FAILED":
                r.error = o.get("message", "未知错误")[:200]
            return r
        except requests.RequestException as e:
            raise ProviderError(f"wan3.0 查询网络错误: {e}") from e
