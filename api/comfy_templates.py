"""ComfyUI 官方工作流模板库 —— 索引缓存到本地，按需把单个模板拉进本机 ComfyUI。

数据来源（2026-11-26 实测可用）：
  ghfast.top 代理 raw.githubusercontent.com / Comfy-Org/workflow_templates
  templates/index.json        781 条模板元数据（约 898KB）
  templates/<name>.json       单个模板本体（约 11KB）

为什么要代理：直连 raw.githubusercontent.com 在本机 SSL 握手失败，
GitHub API 403 限流；ghfast.top 实测可稳定取 raw 文件。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

_INDEX_URLS = (
    "https://ghfast.top/https://raw.githubusercontent.com/"
    "Comfy-Org/workflow_templates/main/templates/index.json",
    "https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/index.json",
)
_FILE_URLS = (
    "https://ghfast.top/https://raw.githubusercontent.com/"
    "Comfy-Org/workflow_templates/main/templates/{name}.json",
    "https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/{name}.json",
)
_MAX_AGE = 7 * 24 * 3600
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")
# 官方索引把视频模板的 mediaType 也标成 image（预览图是 webp），
# 只能靠 tags 里的 "Video" 或文件名里的 i2v/t2v/flf2v 认出来。
_VIDEO_HINT = re.compile(r"(^video[_-]|_i2v\b|_t2v\b|flf2v|_v2v\b)", re.I)


def _kind(t: dict) -> str:
    """把模板归成 image / video / audio，供界面筛选。"""
    media = str(t.get("media_type") or "").lower()
    tags = {str(x).lower() for x in (t.get("tags") or [])}
    if media == "audio" or "audio" in tags:
        return "audio"
    if media == "video" or "video" in tags or _VIDEO_HINT.search(str(t.get("name") or "")):
        return "video"
    return "image"


class TemplateError(RuntimeError):
    """可直接展示给用户的中文错误。"""


def _data_dir() -> Path:
    return Path(os.environ.get("BREEZE_DATA")
                or (Path(__file__).resolve().parent.parent / "data")).resolve()


def _cache_path() -> Path:
    d = _data_dir() / "comfy_templates"
    d.mkdir(parents=True, exist_ok=True)
    return d / "index.json"


def workflows_dir() -> Path:
    """本机 ComfyUI 读取工作流的目录（可用 BREEZE_COMFY_WORKFLOWS 覆写）。"""
    raw = os.environ.get("BREEZE_COMFY_WORKFLOWS") or r"F:\ComfyUI-core\user\default\workflows\_official"
    return Path(raw)


def _fetch(urls: tuple[str, ...], timeout: int = 45) -> bytes:
    last = ""
    for url in urls:
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r.content
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)[:120]
    raise TemplateError(f"拉取模板失败（{last}）。请检查网络后重试。")


def _normalize(raw: bytes) -> list[dict]:
    """把官方分组结构拍平成一条条模板，只留前端要用的字段。"""
    try:
        groups = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise TemplateError(f"模板索引不是合法 JSON：{e}") from e
    out: list[dict] = []
    for g in groups or []:
        cat = str(g.get("category") or "").strip()
        for t in g.get("templates") or []:
            name = str(t.get("name") or "").strip()
            if not name or not _NAME_RE.match(name):
                continue
            size = int(t.get("size") or 0)
            out.append({
                "name": name,
                "title": str(t.get("title") or name),
                "category": cat,
                "description": str(t.get("description") or "")[:400],
                "tags": [str(x) for x in (t.get("tags") or [])][:6],
                "models": [str(x) for x in (t.get("models") or [])][:6],
                "media_type": str(t.get("mediaType") or ""),
                "size_gb": round(size / 1024 ** 3, 2) if size else 0,
                "usage": int(t.get("usage") or 0),
                "custom_nodes": len(t.get("requiresCustomNodes") or []),
                "date": str(t.get("date") or ""),
            })
    return out


def load_index(force: bool = False) -> list[dict]:
    """读本地缓存；过期或不存在就重新拉。拉不到时退回旧缓存（有总比没有好）。"""
    path = _cache_path()
    fresh = path.is_file() and (time.time() - path.stat().st_mtime) < _MAX_AGE
    if fresh and not force:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        data = _normalize(_fetch(_INDEX_URLS))
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    except TemplateError:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        raise


def _fit_label(size_gb: float, custom_nodes: int) -> str:
    """按 8GB 卡给一句实话，避免用户下完才发现跑不动。"""
    if custom_nodes > 0:
        return "需先装自定义节点"
    if not size_gb:
        return "未标注模型大小"
    if size_gb <= 4.5:
        return "8GB 可跑"
    if size_gb <= 8.5:
        return "8GB 需换页"
    return "需更大显存"


def search(query: str = "", limit: int = 60, media: str = "") -> list[dict]:
    items = load_index()
    q = (query or "").strip().lower()
    media = (media or "").strip().lower()
    if media:
        items = [t for t in items if _kind(t) == media]
    if q:
        def hit(t: dict) -> bool:
            blob = " ".join([t.get("name", ""), t.get("title", ""),
                             t.get("category", ""), " ".join(t.get("tags") or []),
                             " ".join(t.get("models") or [])]).lower()
            return q in blob
        items = [t for t in items if hit(t)]
    items = sorted(items, key=lambda t: (-int(t.get("usage") or 0), t.get("name", "")))
    local_names = set(_local_names())
    out = []
    for t in items[: max(1, min(200, limit))]:
        t = dict(t)
        t["kind"] = _kind(t)
        t["fit"] = _fit_label(float(t.get("size_gb") or 0), int(t.get("custom_nodes") or 0))
        t["downloaded"] = t["name"] in local_names
        out.append(t)
    return out


def _local_names() -> list[str]:
    d = workflows_dir()
    if not d.is_dir():
        return []
    try:
        return [p.stem for p in d.glob("*.json")]
    except OSError:
        return []


def local_list() -> list[dict]:
    d = workflows_dir()
    items = []
    for name in _local_names():
        p = d / f"{name}.json"
        try:
            items.append({"name": name, "bytes": p.stat().st_size,
                          "title": _local_title(p, name)})
        except OSError:
            continue
    items.sort(key=lambda x: x["name"])
    return items


def _local_title(path: Path, fallback: str) -> str:
    """模板文件里如果有 title 就用它，读不到就退回文件名。"""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    for key in ("title", "name"):
        v = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip()[:80]
    # 新版模板把名字放在 extra.ds 里
    try:
        ds = obj.get("extra", {}).get("ds", {})
        v = ds.get("name") if isinstance(ds, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip()[:80]
    except AttributeError:
        pass
    return fallback


def download(name: str) -> dict:
    """把单个模板拉到本机 ComfyUI 的 workflows 目录。名字必须在官方索引里。"""
    name = str(name or "").strip()
    if not _NAME_RE.match(name):
        raise TemplateError("模板名不合法")
    known = {t["name"] for t in load_index()}
    if name not in known:
        raise TemplateError("模板不在官方索引里，拒绝下载")
    raw = _fetch(tuple(u.format(name=name) for u in _FILE_URLS))
    try:
        json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise TemplateError(f"下载到的模板不是合法 JSON：{e}") from e
    d = workflows_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise TemplateError(f"无法创建 ComfyUI 工作流目录 {d}：{e}") from e
    target = d / f"{name}.json"
    try:
        target.write_bytes(raw)
    except OSError as e:
        raise TemplateError(f"写入 {target} 失败：{e}") from e
    return {"name": name, "path": str(target), "bytes": len(raw)}
