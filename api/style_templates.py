"""剧本视觉风格模板库：文件优先、内置兜底。

风格模板同时驱动三处：弹窗选项、LLM system prompt 的画面约束、按总时长的建议镜头数。
模板缺失或损坏时不抛错——回退到内置常量，保证新建项目流程不中断。
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_FILE = _ROOT / "data" / "style_templates.json"
DEFAULT_STYLE_ID = "cinematic"
MAX_HINT_LEN = 220
MAX_SHOTS = 30


def _builtin() -> list[dict]:
    """与 data/style_templates.json 保持同源的内置回退（文件坏时使用）。"""
    return [
        {"id": "cinematic", "name": "电影感 · Cinematic",
         "description": "冷暖对比、浅景深、缓慢运镜，适合剧情向短片。",
         "prompt_hint": "画面追求电影感：冷暖对比色调、浅景深、自然光与实用光源结合；镜头运动以缓慢推轨/横移为主，构图留白，避免塑料感与过饱和。",
         "shot_seconds": 6},
        {"id": "cyberpunk", "name": "赛博朋克 · Cyberpunk",
         "description": "霓虹雨夜、体积光、高密度城市细节。",
         "prompt_hint": "画面为赛博朋克风：霓虹招牌与雨夜湿反射、蓝紫与洋红高光、浓雾与体积光；镜头多为低机位仰拍与特写，强调全息广告与机械细节。",
         "shot_seconds": 6},
        {"id": "anime", "name": "日系动画 · Anime",
         "description": "赛璐璐上色、明朗天空、节奏明快。",
         "prompt_hint": "画面为日系动画风：干净线条、柔和赛璐璐上色、大面积天空与云、夸张光影；镜头节奏明快，多用定帧与速度线表现动作。",
         "shot_seconds": 5},
        {"id": "documentary", "name": "写实纪录片 · Doc",
         "description": "手持自然光、真实环境、采访中近景。",
         "prompt_hint": "画面为写实纪录片：手持轻微晃动、自然光、真实环境声暗示（不生成声音）、采访式中近景；避免摆拍感与戏剧化打光。",
         "shot_seconds": 6},
        {"id": "product", "name": "产品广告 · Product",
         "description": "主体突出、材质质感、色调统一。",
         "prompt_hint": "画面为产品广告：主体居中或三分构图、洁净背景、柔和轮廓光勾勒材质；镜头缓慢环绕或微距推进，突出质感与卖点，色调统一。",
         "shot_seconds": 5},
        {"id": "vertical", "name": "竖屏短视频 · Vertical",
         "description": "9:16 构图、顶部字幕安全区、开头强钩子。",
         "prompt_hint": "画面按 9:16 竖屏构图：主体位于中上三分之一、顶部留字幕安全区、背景简化；镜头运动短促有力，前 1 秒给强视觉钩子。",
         "shot_seconds": 5},
    ]


def _valid(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    tid = str(item.get("id") or "").strip()
    name = str(item.get("name") or "").strip()
    hint = str(item.get("prompt_hint") or "").strip()
    secs = item.get("shot_seconds")
    return bool(tid and name and hint
                and isinstance(secs, (int, float)) and not isinstance(secs, bool) and secs > 0)


def load_templates() -> list[dict]:
    """读取模板文件；任何异常/字段缺失都回退内置。"""
    try:
        raw = json.loads(_FILE.read_text(encoding="utf-8"))
        items = raw.get("templates") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return _builtin()
        out = []
        for it in items:
            if not _valid(it):
                continue
            out.append({
                "id": str(it["id"]).strip(),
                "name": str(it["name"]).strip(),
                "description": str(it.get("description") or "").strip(),
                "prompt_hint": str(it["prompt_hint"]).strip()[:MAX_HINT_LEN],
                "shot_seconds": float(it["shot_seconds"]),
            })
        return out or _builtin()
    except Exception:
        return _builtin()


def list_templates() -> list[dict]:
    return load_templates()


def get_template(style_id: str | None) -> dict:
    """未知/空 id 一律落到默认模板，不返回 None。"""
    tpls = load_templates()
    sid = str(style_id or "").strip() or DEFAULT_STYLE_ID
    for t in tpls:
        if t["id"] == sid:
            return t
    for t in tpls:
        if t["id"] == DEFAULT_STYLE_ID:
            return t
    return tpls[0]


def suggest_shot_count(total_seconds, shot_seconds) -> int | None:
    """按总时长与单镜时长给出建议镜头数；非法输入返回 None（由调用方保持原值）。"""
    try:
        total = float(total_seconds)
        per = float(shot_seconds)
    except (TypeError, ValueError):
        return None
    if total <= 0 or per <= 0:
        return None
    return max(1, min(MAX_SHOTS, int(total / per + 0.5)))


def style_system_suffix(template: dict | None) -> str:
    """注入 LLM system prompt 的风格约束段。"""
    if not template:
        return ""
    hint = str(template.get("prompt_hint") or "").strip()
    if not hint:
        return ""
    return f"\n6. 视觉风格约束（必须遵守）：{hint}"
