"""LLM client — 统一调用 OpenAI 兼容 / Anthropic 兼容端点。

LongCat 同时暴露两种格式：
- OpenAI:    https://api.longcat.chat/openai   (model=LongCat-2.5)
- Anthropic: https://api.longcat.chat/anthropic  (model=LongCat-2.5)

用法：
    from api.llm_client import call_chat
    text = call_chat(provider="longcat", api_key="xxx",
                     system="...", user="...", json_mode=True)
"""
from __future__ import annotations
import json, os, time
from pathlib import Path

import requests


# ------------------------------------------------------------------
# LLM 提供商注册表（端点 + 默认模型 + 协议）
# ------------------------------------------------------------------
LLM_PROVIDERS = {
    "longcat": {
        "label": "LongCat 2.5",
        "endpoints": {
            "openai":    "https://api.longcat.chat/openai",
            "anthropic": "https://api.longcat.chat/anthropic",
        },
        "default_model": "LongCat-2.5",
        "default_protocol": "openai",
        "default_timeout": 60,
    },
    "openai": {
        "label": "OpenAI GPT",
        "endpoints": {
            "openai": "https://api.openai.com/v1",
        },
        "default_model": "gpt-4o-mini",
        "default_protocol": "openai",
        "default_timeout": 60,
    },
}


def call_chat(*, provider: str, api_key: str,
              system: str = "", user: str,
              model: str = "", json_mode: bool = True,
              max_retries: int = 2, timeout: int = 0,
              base_url: str = "") -> str:
    """调一次 chat completion，返回纯文本。json_mode=True 时返回 JSON 文本。

    provider 为内置（longcat/openai）时按注册表取端点；其余视为自定义模型，
    必须由调用方传 base_url（OpenAI 兼容），model 缺省用 provider 本身。
    """
    cfg = LLM_PROVIDERS.get(provider)
    if cfg:
        protocol = cfg["default_protocol"]
        base_url = (base_url or cfg["endpoints"][protocol]).rstrip("/")
        model_name = model or cfg["default_model"]
        to = timeout or cfg["default_timeout"]
    else:
        # 自定义模型：只支持 OpenAI 兼容协议，端点来自 data/model_registry.json
        if not base_url:
            raise ValueError(f"未知 LLM provider: {provider}（自定义模型需提供 base_url）")
        if not base_url.lower().startswith(("http://", "https://")):
            raise ValueError(f"base_url 非法（需 http/https）：{base_url}")
        protocol = "openai"
        base_url = base_url.rstrip("/")
        model_name = model or provider
        to = timeout or 60
    if not api_key:
        raise ValueError("api_key 为空")

    if protocol == "openai":
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model_name,
            "messages": [],
            "temperature": 0.7,
        }
        if system:
            payload["messages"].append({"role": "system", "content": system})
        payload["messages"].append({"role": "user", "content": user})
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    elif protocol == "anthropic":
        url = base_url.rstrip("/") + "/v1/messages"
        payload = {
            "model": model_name,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0.7,
        }
        if system:
            payload["system"] = system
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
    else:
        raise ValueError(f"未知协议: {protocol}")

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=to)
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                break
            data = r.json()
            if protocol == "openai":
                return data["choices"][0]["message"]["content"]
            else:
                # anthropic content blocks
                blocks = data.get("content", [])
                return "".join(b.get("text", "") for b in blocks)
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
    raise RuntimeError(f"LLM 调用失败（重试 {max_retries} 次）: {last_err}")
