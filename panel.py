#!/usr/bin/env python
"""BreezeTTS Studio — breeze-tts Web 控制面板（stdlib 单文件，零第三方依赖）。

端口台账（权威口径见 F:/TTS2/.workbuddy/memory/MEMORY.md，启用后冻结）：
  面板 7861（本文件）| 合成 API 7860（breeze_infer.api，不改不扰）

用法:
  python panel.py            # 监听 0.0.0.0:7861
"""
from __future__ import annotations

import json
import os
import re
import sys
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, unquote
from urllib.request import Request, urlopen
import http.client as _httpclient

# 直接 `python panel.py` 运行时模块名是 __main__，而 script_routes 里 `import panel`
# 会再加载一份——锁、DATA、_JOBS 全被复制成两份，并发保护形同虚设。
# 把它登记成 panel，让两种导入路径拿到同一个模块对象。
sys.modules.setdefault("panel", sys.modules[__name__])

# ---- 端口台账（锁定，禁止修改默认值）----
# 默认值就是台账口径；允许用环境变量覆写**仅为**回滚演练/并行金丝雀，
# 否则新旧版本必须抢同一个端口，回滚就只能先停后起（期间服务中断）。
PANEL_PORT = int(os.environ.get("BREEZE_PANEL_PORT") or 7861)
API_BASE = os.environ.get("BREEZE_API_BASE") or "http://127.0.0.1:7860"  # 端口已冻结

ROOT = Path(__file__).resolve().parent
# 默认跟着代码走；允许覆写是为了让并行演练实例能读到同一份数据副本，
# 否则金丝雀实例看到的是空库，回滚演练验证不到真实音色与历史。
DATA = Path(os.environ.get("BREEZE_DATA") or (ROOT / "data")).resolve()
REFS = DATA / "refs"
AUDIOS = DATA / "audio"
for d in (DATA, REFS, AUDIOS):
    d.mkdir(parents=True, exist_ok=True)

class _ValidationError(Exception):
    """读改写过程中发现数据不合法：不写入，由外层翻成 400。"""


def _resolve_audio(rel: str) -> Path | None:
    """把 /api/audio/ 后的相对路径解析成 AUDIOS 下的真实文件，越界返回 None。

    只按 "/" 切分是不够的：Windows 上反斜杠同样分隔目录，盘符更是直接
    跳出整个目录树（实测 joinpath("C:\\Windows\\win.ini") 可读）。
    因此这里既拒绝可疑字符，也对最终路径做 is_relative_to 校验。
    """
    if not rel or "\\" in rel or ":" in rel or "\x00" in rel:
        return None
    parts = [seg for seg in rel.split("/") if seg]
    if not parts or len(parts) > 3 or any(seg in (".", "..") for seg in parts):
        return None
    if any(seg != _safe_name(seg) for seg in parts):
        return None
    try:
        base = AUDIOS.resolve()
        target = base.joinpath(*parts).resolve()
    except (OSError, ValueError):
        return None
    if target != base and not target.is_relative_to(base):
        return None
    return target


def _post_upstream(api_path: str, body: bytes, content_type: str, timeout: int = 600,
                   headers_out: dict | None = None):
    """POST 到推理 API，返回 (status, body, ctype)。错误已翻译成人话。

    headers_out 非空时把上游响应头写入该字典。Qwen 适配层用
    X-Adapter-Note 说明静默降级（例如克隆音色忽略指令），必须让 UI 看见，
    而不是像早期版本那样只取 body 就把头丢掉。
    """
    req = Request(API_BASE + api_path, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    try:
        with urlopen(req, timeout=timeout) as resp:
            if headers_out is not None:
                headers_out.update({k: resp.headers.get(k) for k in resp.headers.keys()})
            return resp.status, resp.read(), resp.headers.get("Content-Type",
                                                              "application/octet-stream")
    except HTTPError as e:
        detail = e.read()
        try:
            detail = json.loads(detail).get("detail", detail[:200])
        except Exception:
            detail = (detail.decode("utf-8", "replace")[:200]
                      if isinstance(detail, bytes) else str(detail)[:200])
        return e.code, json.dumps({"detail": _friendly_error(detail)},
                                  ensure_ascii=False).encode(), "application/json"
    except URLError as e:
        return 502, json.dumps({"detail": f"API 7860 不可达: {e.reason}"}).encode(),             "application/json"


def _post_with_retry(api_path: str, body: bytes, content_type: str,
                     timeout: int = 600,
                     headers_out: dict | None = None) -> tuple[int, bytes]:
    """排队 + 409 自动重试。剧本逐行合成也要走这里，否则会跟单条请求撞车。"""
    code, raw = 502, b"{}"
    with _INFER_LOCK:
        for attempt in range(60):
            code, raw, _ct = _post_upstream(api_path, body, content_type,
                                            timeout, headers_out)
            if code != 409:
                return code, raw
            if attempt < 59:
                time.sleep(3.0)
    return code, raw


def _engine_note(headers: dict) -> str:
    """还原上游 percent-encode 过的降级说明（HTTP 头不支持非 latin-1 字符）。"""
    return unquote(headers.get("X-Adapter-Note", "") or "")


def _safe_int(value, default: int) -> int:
    """把表单里的字符串转 int，非法值回落到默认。

    上游已经出完音频之后才在构造响应时抛 ValueError，会直接断连：
    GPU 白跑一次，用户什么也看不到。
    """
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


SAMPLE_RATE = 24000
_LOCK = threading.RLock()  # 可重入：读改写块可嵌套 _save
_INFER_LOCK = threading.Lock()  # 上游单并发：面板内部串行排队，避免 409 already running


def _friendly_error(msg: str) -> str:
    """把上游英文报错翻译成可执行的人话。"""
    low = str(msg).lower()
    if "already running" in low:
        return "另一条合成正在进行中，请等它完成后重试（面板每次只跑一条，多任务会自动排队）"
    if "does not define a negative prompt" in low:
        return "该音色不支持指令强度（CFG）：请保持 cfg=1，或改用「导向音色」来控制语气"
    if "out of memory" in low or ("cuda" in low and "memory" in low):
        return "GPU 显存不足：请缩短文本、减少批量条数，或关闭其他占用 GPU 的程序"
    if "ref_audio and ref_text" in low or "ref_audio_path and ref_text" in low:
        return "参考音频和文字稿必须成对提供（缺一不可）"
    return str(msg)[:300]


# ---------- W2-3 质量管线 ----------
def _dict_path() -> Path:
    """按调用时求值：测试把 DATA 指向临时目录后，词典也要跟着走。

    原先在 import 期绑定，测试只会重定向 DATA/REFS/AUDIOS，词典仍写生产
    data/dictionary.json——跑一次测试就往用户词典里塞条目。
    """
    return DATA / "dictionary.json"


def _load_dict():
    """读取发音词典（缺失或损坏都返回空词典，不阻塞服务）。"""
    try:
        from tts_pipeline.pron_dict import PronunciationDict
        return PronunciationDict.load(_dict_path())
    except Exception:
        class _Empty:
            entries: list = []

            def apply(self, text, scope="global"):
                return text, []

            # 调用方还用了这两个：tts_pipeline 不可用时不能抛 AttributeError
            # （那会把连接直接断掉，用户看不到任何提示）。
            def to_list(self):
                return []

            def remove(self, word, scope="global"):
                return False
        return _Empty()


def _save_dict(d) -> None:
    d.save(_dict_path())


def _apply_text_pipeline(text: str, norm_flag: str, scope: str = "global"):
    """文本规范化 + 发音词典。

    norm_flag:
      "" / "1" → 不改写文本（"1" 仅表示注入普通话指令，由合成端处理）
      "2"      → 走真实规范化管线
    返回 (新文本, 变更列表[{before, after, kind}])
    """
    if norm_flag != "2" or not text:
        return text, []
    changes: list[dict[str, str]] = []
    out = text
    try:
        from tts_pipeline.text_norm import normalize_text
        # 产品是中文优先（PRD A1）：只要出现汉字、或完全没有拉丁字母
        # （例如裸金额 "¥1,299.50"），就按中文读，不要误判成英文。
        import re as _re
        has_cjk = bool(_re.search(r"[一-鿿]", out))
        has_latin_word = bool(_re.search(r"[A-Za-z]{2,}", out))
        lang = "zh" if (has_cjk or not has_latin_word) else "en"
        result = normalize_text(out, lang=lang)
        out = result.text
        for c in result.changes:
            changes.append({"before": c.before, "after": c.after, "kind": c.kind})
    except Exception as exc:
        # 不能静默吞掉：规范化抛异常时文本会原样下发，用户只会觉得"读了怪"，
        # 而开发和排查都看不到任何信号。把失败写进变更清单让它可见。
        changes.append({"before": "", "after": "",
                        "kind": "normalize-failed", "error": str(exc)[:120]})
    try:
        d = _load_dict()
        if d.entries:
            out, dict_hits = d.apply(out, scope)
            for h in dict_hits:
                changes.append({"before": h.get("original", ""),
                                "after": h.get("replacement", ""),
                                "kind": "dict-" + h.get("kind", "replace")})
    except Exception:
        pass
    return out, changes


# ---------- 持久化（W2-4: 原子写 + 损坏恢复） ----------
def _load(name: str) -> list:
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # R1: 损坏时保留备份，不静默丢弃
            import shutil
            backup = p.with_suffix(".json.corrupt")
            try:
                shutil.copy2(p, backup)
            except Exception:
                pass
            return []
    return []


def _save(name: str, items: list) -> None:
    with _LOCK:
        target = DATA / name
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 原子替换：Windows 上 os.replace 是原子操作
        import os
        os.replace(str(tmp), str(target))


class _file_lock:
    """跨进程文件锁（lockfile + O_EXCL 自旋）。

    面板进程与任何其他进程（例如测试进程、外部脚本）同时写同一份 JSON 时，
    没有它就会出现「后写覆盖先写、丢条目」。用 O_CREAT|O_EXCL 原子创建锁文件。
    """

    def __init__(self, name: str, timeout: float = 10.0):
        self.path = DATA / (name + ".lock")
        self.timeout = timeout
        self._fd = None
        self._degraded = False

    STALE_AFTER = 60.0   # 写 JSON 只需毫秒级；60 秒还没释放说明持有者已经没了
    RELEASED = b"released"

    def _is_stale(self) -> bool:
        """锁只可能来自死进程时返回 True。

        用锁龄判断而不是探测 PID：Windows 上 os.kill(pid, 0) 会直接
        TerminateProcess 杀掉对方，拿来做存活检查是事故。
        """
        try:
            return (time.time() - self.path.stat().st_mtime) > self.STALE_AFTER
        except OSError:
            return False

    def _try_clear(self) -> str:
        """尽力清掉锁文件，返回 "unlinked" / "marked" / "failed"。

        某些环境（批量删除保护、只读目录、杀软占用）会让 unlink 抛非 OSError，
        之前这里只捕获 OSError，异常一路冒到请求处理里把连接打断，还会把锁
        永久留在盘上，之后所有写入都会等超时。

        返回值必须区分「真的删掉了」和「只写了已释放标记」：后者文件仍在，
        下一轮还会撞 FileExistsError，若当成成功就会在这里空转不 sleep。
        """
        try:
            self.path.unlink()
            return "unlinked"
        except Exception:
            pass
        try:
            self.path.write_bytes(self.RELEASED)
            return "marked"
        except Exception:
            return "failed"

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                # 已释放的标记文件等于没有持有者，可以接管
                try:
                    if self.path.read_bytes().strip() == self.RELEASED:
                        result = self._try_clear()
                        if result == "unlinked":
                            continue
                        if result == "marked":
                            # 删不掉但能写：退化为非独占获取，宁可互斥弱一点，
                            # 也不要让请求在这里永久空转。
                            sys.stderr.write(
                                f"[panel] 锁无法删除，退化获取: {self.path.name}\u000a")
                            self._degraded = True
                            self._fd = os.open(str(self.path),
                                               os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
                            os.write(self._fd, str(os.getpid()).encode())
                            return self
                except Exception:
                    pass
                if time.time() > deadline:
                    if self._is_stale() and self._try_clear() == "unlinked":
                        continue
                    raise TimeoutError(
                        f"等待 {self.path.name} 超时，可能有其他进程正在写入")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        import os
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        # 释放失败不能让请求崩掉：这里兜底成写「已释放」标记，
        # 下个持锁者看到标记会直接接管。
        if self._try_clear() == "failed":
            try:
                sys.stderr.write("[panel] 释放锁失败: " + self.path.name + "\n")
            except Exception:
                pass
        return False


def _update_json(name: str, mutator) -> list:
    """读-改-写原子操作（R2 修复）：进程内锁 + 跨进程文件锁，整个 RMW 不被打断。

    所有写入都必须走这里。裸 `_load` → 改 → `_save` 在 ThreadingHTTPServer 下
    会让两个并发请求各自读一份再整体覆盖回去，丢掉对方的改动。
    """
    with _LOCK:
        with _file_lock(name):
            items = _load(name)
            result = mutator(items)
            _save(name, items)  # _LOCK 是 RLock，这里重入安全；写盘只在 _save 一处
            return result if result is not None else items


# ---------- 工具 ----------
def pcm_to_wav(pcm: bytes, normalize: bool = True) -> bytes:
    """PCM(s16le mono 24k) 封装为 WAV；默认响度归一化（只增益不衰减，峰值限幅 97%）。"""
    if normalize and len(pcm) >= 4:
        import array

        usable = len(pcm) - (len(pcm) % 2)
        a = array.array("h")
        a.frombytes(pcm[:usable])
        if len(a) > 32:
            rms = (sum(x * x for x in a) / len(a)) ** 0.5
            peak = max(abs(x) for x in a)
            target_rms = 0.2 * 32767  # ≈ -14 dBFS，正常对白响度
            if rms > 32:
                gain = target_rms / rms
                # 允许峰值到 120% 后硬限幅：换响度，语音瞬态轻微软削可接受
                if peak > 0 and (32767 * 1.2) / peak < gain:
                    gain = (32767 * 1.2) / peak
                if gain > 1.0:
                    a = array.array(
                        "h",
                        (max(-32767, min(32767, int(x * gain))) for x in a),
                    )
                    pcm = a.tobytes() + pcm[usable:]
    n = len(pcm)
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + n, b"WAVE",
            b"fmt ", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
            b"data", n,
        )
        + pcm
    )


def parse_multipart(body: bytes, content_type: str) -> tuple[dict, dict]:
    """极简 multipart/form-data 解析（Python3.13 无 cgi 模块）。"""
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        return {}, {}
    boundary = b"--" + m.group(1).encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in body.split(boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, sep, payload = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        headers = head.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]+)"', headers)
        if not name_m:
            continue
        name = name_m.group(1)
        fn_m = re.search(r'filename="([^"]*)"', headers)
        if fn_m:
            files[name] = (fn_m.group(1), payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


def resample_pcm(pcm: bytes, speed: float) -> bytes:
    """线性插值变速（speed>1 加快）。1.0 原样返回。"""
    if abs(speed - 1.0) < 1e-6 or len(pcm) < 4:
        return pcm
    import array
    usable = len(pcm) - (len(pcm) % 2)
    a = array.array("h")
    a.frombytes(pcm[:usable])
    n = len(a)
    if n < 2:
        return pcm
    out_len = max(2, int(n / speed))
    out = array.array("h", bytes(2 * out_len))
    step = speed
    for i in range(out_len):
        pos = i * step
        i0 = int(pos)
        if i0 >= n - 1:
            out[i] = a[n - 1]
        else:
            frac = pos - i0
            out[i] = int(a[i0] * (1 - frac) + a[i0 + 1] * frac)
    return out.tobytes()


def wav_duration(data: bytes) -> float:
    """标准 WAV 时长（秒）；非 WAV 返回 -1。"""
    if len(data) < 44 or data[:4] != b"RIFF":
        return -1.0
    pos, rate, ch, bits, dsize = 12, 0, 0, 0, 0
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        csz = struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt ":
            _, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, pos + 8)
        elif cid == b"data":
            dsize = min(csz, len(data) - pos - 8)
            break
        pos += 8 + csz + (csz & 1)
    if not (rate and ch and bits):
        return -1.0
    return dsize / (rate * ch * bits / 8)


def trim_wav_bytes(data: bytes, max_sec: float = 15.0) -> bytes:
    """把 WAV 裁到前 max_sec 秒（重建标准头）。非 WAV 原样返回。"""
    if len(data) < 44 or data[:4] != b"RIFF":
        return data
    pos, rate, ch, bits, dstart, dsize = 12, 0, 0, 0, 0, 0
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        csz = struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt ":
            _, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, pos + 8)
        elif cid == b"data":
            dstart, dsize = pos + 8, min(csz, len(data) - pos - 8)
            break
        pos += 8 + csz + (csz & 1)
    if not (rate and ch and bits and dstart):
        return data
    frame = max(1, ch * bits // 8)
    keep = min(int(max_sec * rate * ch * bits / 8) // frame * frame, dsize)
    raw = data[dstart:dstart + keep]
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(raw), b"WAVE",
        b"fmt ", 16, 1, ch, rate, rate * ch * bits // 8, frame, bits,
        b"data", len(raw),
    ) + raw


# 占位符文稿（历史遗留：填了但其实没填）——视同缺文稿
_PLACEHOLDER_REF_TEXT = (
    "参考文字稿", "参考文案", "参考稿", "占位", "placeholder",
    "test", "测试文字稿", "待补充", "todo",
)


def _is_placeholder_ref_text(text: str) -> bool:
    """判断参考文稿是否只是占位符（不是真实转写）。"""
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(t == p or t.startswith(p) for p in _PLACEHOLDER_REF_TEXT)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)[:80]


# ---------- 面板 HTML ----------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BreezeTTS Studio</title>
<style>
  :root{
    --bg:#0A0E27; --bg2:#0D1226; --panel:#141A2E; --panel2:#0E1326;
    --line:#262E4A; --line2:#2A3050;
    --text:#F8FAFC; --muted:#9CA8C4; --dim:#64748B;
    --primary:#60A5FA; --primary-d:#3B82F6; --primary-soft:rgba(96,165,250,.14);
    --cta:#F97316; --cta-d:#EA580C; --cta-glow:rgba(249,115,22,.35);
    --ok:#34D399; --ok-soft:rgba(52,211,153,.12);
    --danger:#F87171; --danger-soft:rgba(248,113,113,.12);
    --warn:#FBBF24;
    --radius:14px; --radius-s:10px;
    --shadow:0 10px 30px rgba(2,6,23,.45);
    --font:"Fira Sans","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
    --mono:"Fira Code",Consolas,"Courier New",monospace;
  }
  *{box-sizing:border-box; margin:0; padding:0}
  html{font-size:16px}
  body{
    background:
      radial-gradient(1100px 500px at 85% -10%, rgba(59,130,246,.10), transparent 60%),
      radial-gradient(900px 420px at -10% 110%, rgba(249,115,22,.07), transparent 55%),
      var(--bg);
    color:var(--text); min-height:100vh; font-family:var(--font); line-height:1.6;
    -webkit-font-smoothing:antialiased;
    display:flex; flex-direction:column; height:100vh; overflow:hidden;
  }
  code,.mono{font-family:var(--mono)}
  ::selection{background:rgba(96,165,250,.35)}
  ::-webkit-scrollbar{width:9px; height:9px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:#2A3050; border-radius:8px; border:2px solid var(--panel)}
  ::-webkit-scrollbar-thumb:hover{background:#3B82F6}

  button{font:inherit; cursor:pointer; border:none; border-radius:var(--radius-s);
    transition:background-color .2s, border-color .2s, color .2s, box-shadow .2s, opacity .2s}
  button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible{
    outline:2px solid var(--primary); outline-offset:2px}
  input, select, textarea{
    font:inherit; color:var(--text); background:var(--panel2);
    border:1px solid var(--line); border-radius:var(--radius-s);
    padding:11px 13px; width:100%; transition:border-color .2s, box-shadow .2s}
  input::placeholder, textarea::placeholder{color:var(--dim)}
  input:hover, select:hover, textarea:hover{border-color:#34406B}
  input:focus, select:focus, textarea:focus{
    border-color:var(--primary); box-shadow:0 0 0 3px rgba(96,165,250,.16); outline:none}
  textarea{resize:vertical; min-height:132px; line-height:1.7}
  label{display:block; font-size:.78rem; font-weight:600; letter-spacing:.06em;
    text-transform:uppercase; color:var(--muted); margin:12px 0 6px}

  /* ---------- header ---------- */
  header{
    display:flex; align-items:center; gap:14px; padding:13px 24px;
    flex-shrink:0; position:sticky; top:0; z-index:30;
    background:rgba(10,14,39,.86); backdrop-filter:blur(14px);
    border-bottom:1px solid var(--line);
  }
  .brand{display:flex; align-items:center; gap:11px}
  .brand-mark{
    width:38px; height:38px; border-radius:11px; display:flex; align-items:center; justify-content:center;
    background:linear-gradient(135deg,#3B82F6,#7C3AED); box-shadow:0 6px 18px rgba(59,130,246,.4)}
  .brand-mark svg{width:20px; height:20px}
  .brand h1{font-size:1.12rem; font-weight:700; letter-spacing:.01em}
  .brand h1 .grad{
    background:linear-gradient(90deg,#60A5FA,#A78BFA);
    -webkit-background-clip:text; background-clip:text; color:transparent}
  .pill{
    display:inline-flex; align-items:center; gap:8px; padding:7px 14px;
    border-radius:999px; border:1px solid var(--line); background:rgba(14,19,38,.8);
    font-size:.8rem; color:var(--muted)}
  .pill.mono{font-family:var(--mono); font-size:.74rem}
  .dot{width:8px; height:8px; border-radius:50%; background:var(--dim); flex-shrink:0}
  .dot.on{background:var(--ok); box-shadow:0 0 8px rgba(52,211,153,.7)}
  .dot.off{background:var(--danger); box-shadow:0 0 8px rgba(248,113,113,.6)}
  .spacer{flex:1}
  .mode-switch{
    display:flex; border:1px solid var(--line); border-radius:12px; overflow:hidden;
    background:var(--panel2)}
  .mode-switch button{
    min-height:40px; padding:8px 18px; background:transparent; color:var(--muted); font-size:.88rem;
    border-radius:0}
  .mode-switch button+button{border-left:1px solid var(--line)}
  .mode-switch button:hover{color:var(--text)}
  .mode-switch button.active{background:var(--primary-d); color:#fff; font-weight:600}

  /* ---------- layout ---------- */
  main{
    display:grid; grid-template-columns:312px minmax(0,1fr) 336px; gap:18px;
    padding:20px 24px 24px; max-width:1620px; margin:0 auto;
    flex:1; min-height:0; overflow:hidden}
  .col{display:flex; flex-direction:column; gap:18px; min-width:0; min-height:0; overflow:hidden}
  .col.center{overflow-y:auto}
  .card{
    background:linear-gradient(165deg, var(--panel), #11152A 70%);
    border:1px solid var(--line); border-radius:var(--radius);
    padding:18px; box-shadow:var(--shadow);
    display:flex; flex-direction:column; min-height:0}
  .col>.card{flex:1 1 0}
  .card h2{
    font-size:.92rem; font-weight:700; letter-spacing:.02em; margin-bottom:14px;
    display:flex; align-items:center; gap:9px; justify-content:space-between}
  .card h2 .sub{font-size:.74rem; color:var(--dim); font-weight:500; letter-spacing:.03em}
  .card h2 .h2-left{display:flex; align-items:center; gap:9px}
  .card h2 svg{width:17px; height:17px; color:var(--primary)}

  /* ---------- buttons ---------- */
  .btn{
    min-height:44px; padding:10px 16px; background:var(--panel2); color:var(--text);
    border:1px solid var(--line2); font-size:.9rem; font-weight:500;
    display:inline-flex; align-items:center; justify-content:center; gap:8px}
  .btn:hover{border-color:var(--primary); color:var(--primary); background:var(--primary-soft)}
  .btn-cta{
    background:linear-gradient(135deg, #F97316, #EA580C); color:#fff; font-weight:700;
    min-width:158px; min-height:48px; font-size:.98rem;
    box-shadow:0 6px 20px var(--cta-glow)}
  .btn-cta:hover{background:linear-gradient(135deg,#FB8B3C,#F0630C); color:#fff;
    box-shadow:0 8px 26px rgba(249,115,22,.5); transform:translateY(-1px)}
  .btn-cta[disabled]{opacity:.55; cursor:not-allowed; transform:none; box-shadow:none}
  .btn-danger{color:var(--danger); border-color:rgba(248,113,113,.35)}
  .btn-danger:hover{border-color:var(--danger); background:var(--danger-soft); color:var(--danger)}
  .icon{width:18px; height:18px; flex-shrink:0}
  .iconbtn{
    width:34px; height:34px; display:inline-flex; align-items:center; justify-content:center;
    background:transparent; color:var(--dim); border:1px solid transparent; border-radius:9px; flex-shrink:0}
  .iconbtn:hover{color:var(--primary); border-color:var(--line2); background:var(--primary-soft)}
  .iconbtn.del:hover{color:var(--danger); border-color:rgba(248,113,113,.4); background:var(--danger-soft)}

  /* ---------- lists ---------- */
  .list{display:flex; flex-direction:column; gap:8px; flex:1 1 auto; min-height:0; overflow-y:auto; padding-right:2px}
  .item{
    display:flex; align-items:center; gap:9px; padding:11px 12px; border-radius:12px;
    border:1px solid var(--line); background:var(--panel2); cursor:pointer;
    transition:border-color .2s, background-color .2s}
  .item:hover{border-color:var(--primary); background:rgba(96,165,250,.07)}
  .item.active{border-color:var(--primary); background:var(--primary-soft);
    box-shadow:inset 0 0 0 1px rgba(96,165,250,.35)}
  .item .name{font-size:.88rem; font-weight:600}
  .item .meta{font-size:.71rem; color:var(--dim); margin-top:1px}
  .item .grow{flex:1; min-width:0}
  .item .name,.item .meta{white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .tag{
    font-size:.66rem; font-weight:700; letter-spacing:.05em; padding:3px 9px;
    border-radius:999px; border:1px solid var(--line); color:var(--muted); flex-shrink:0}
  .tag.design{color:#A78BFA; border-color:rgba(167,139,250,.4); background:rgba(167,139,250,.1)}
  .tag.clone{color:var(--ok); border-color:rgba(52,211,153,.4); background:var(--ok-soft)}
  .tag.direction{color:var(--cta); border-color:rgba(249,115,22,.4); background:rgba(249,115,22,.1)}
  .empty{
    color:var(--dim); font-size:.84rem; padding:18px 10px; text-align:center;
    border:1px dashed var(--line); border-radius:12px}

  /* ---------- chips ---------- */
  .chips{display:flex; gap:8px; flex-wrap:wrap; margin:2px 0 4px; min-height:40px; align-items:center}
  .chip{
    padding:8px 14px; border-radius:999px; border:1px solid var(--line2);
    background:var(--panel2); font-size:.82rem; cursor:pointer; min-height:38px;
    display:inline-flex; align-items:center; gap:6px; transition:border-color .2s, color .2s, background-color .2s}
  .chip:hover{border-color:var(--primary); color:var(--primary)}
  .chip.active{background:var(--primary-soft); border-color:var(--primary); color:var(--primary); font-weight:600}
  .chip.flat{cursor:default; opacity:.75}

  /* ---------- status ---------- */
  .status{
    font-size:.86rem; min-height:1.5em; margin-top:12px;
    padding:10px 14px; border-radius:var(--radius-s); border:1px solid transparent;
    display:flex; align-items:center; gap:9px}
  .status:empty{display:none}
  .status.ok{color:var(--ok); background:var(--ok-soft); border-color:rgba(52,211,153,.25)}
  .status.err{color:var(--danger); background:var(--danger-soft); border-color:rgba(248,113,113,.3)}
  .status.busy{color:var(--primary); background:var(--primary-soft); border-color:rgba(96,165,250,.28)}
  .spinner{
    width:15px; height:15px; border-radius:50%; flex-shrink:0;
    border:2px solid rgba(96,165,250,.25); border-top-color:var(--primary);
    animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  @media (prefers-reduced-motion:reduce){
    *{transition:none !important; animation:none !important}
    .spinner{border-top-color:var(--primary); border-right-color:var(--primary)}
  }
  .hint{font-size:.76rem; color:var(--dim); margin-top:6px; line-height:1.55}
  .hint code{color:var(--muted); background:var(--panel2); padding:1px 6px; border-radius:6px; font-size:.72rem}

  /* ---------- player ---------- */
  .player{
    display:flex; align-items:center; gap:12px; margin-top:14px; flex-wrap:wrap}
  audio{width:100%; min-height:44px; filter:saturate(.92)}
  .player-box{
    margin-top:14px; padding:12px; border-radius:12px;
    background:var(--panel2); border:1px solid var(--line)}
  .progress-track{
    height:4px; border-radius:4px; background:var(--line); overflow:hidden; margin-top:12px}
  .progress-track .bar{
    height:100%; width:0; border-radius:4px;
    background:linear-gradient(90deg, var(--primary), #A78BFA);
    transition:width .5s ease}

  /* ---------- history ---------- */
  .hist{display:flex; flex-direction:column; gap:8px; flex:1 1 auto; min-height:0; overflow-y:auto; padding-right:2px}
  .hist .item{cursor:default}
  .hist .item.fresh{border-color:var(--ok); background:var(--ok-soft)}
  .hist .play{color:var(--primary)}

  /* ---------- dialogs ---------- */
  dialog{
    border:1px solid var(--line2); border-radius:16px; background:var(--panel);
    color:var(--text); width:min(540px, 92vw); padding:22px; box-shadow:0 24px 80px rgba(2,6,23,.7)}
  dialog::backdrop{background:rgba(4,6,16,.72); backdrop-filter:blur(3px)}
  dialog h3{margin-bottom:4px; font-size:1.08rem; font-weight:700}
  .dlg-actions{display:flex; gap:10px; justify-content:flex-end; margin-top:18px}
  .filelabel{
    display:flex; align-items:center; gap:10px; padding:13px;
    border:1px dashed var(--line2); border-radius:var(--radius-s);
    cursor:pointer; color:var(--muted); font-size:.85rem; min-height:46px;
    transition:border-color .2s, color .2s}
  .filelabel:hover{border-color:var(--primary); color:var(--primary)}
  .filelabel input{display:none}
  .row{display:flex; gap:12px; flex-wrap:wrap}
  .row>*{flex:1; min-width:130px}

  @media (max-width:1220px){
    main{grid-template-columns:1fr; height:auto; overflow:visible}
    .col{overflow:visible}
    .list,.hist{max-height:320px}
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round">
        <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/>
      </svg>
    </div>
    <h1>Breeze<span class="grad">TTS</span> Studio</h1>
  </div>
  <div class="pill"><span class="dot" id="apiDot"></span><span id="apiText">连接中…</span></div>
  <div class="pill mono" id="modelInfo" title="model_digest">—</div>
  <div class="spacer"></div>
  <div class="mode-switch" role="tablist" aria-label="合成模式">
    <button id="modeSingle" class="active" role="tab" aria-selected="true">单条合成</button>
    <button id="modeBatch" role="tab" aria-selected="false">批量合成</button>
  </div>
</header>

<main>
  <!-- 左：音色库 + 角色库 -->
  <div class="col">
    <section class="card">
      <h2>
        <span class="h2-left">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>
          音色库
        </span>
        <span class="sub" id="voiceCount">0 个</span>
      </h2>
      <div class="row" style="margin-bottom:12px">
        <button class="btn" onclick="openVoiceDlg('design')" title="用自然语言描述创造全新音色">+ 设计</button>
        <button class="btn" onclick="openVoiceDlg('clone')" title="上传自己的声音直接克隆">+ 克隆</button>
        <button class="btn" onclick="openVoiceDlg('direction')" title="克隆声音并控制语气">+ 导向</button>
      </div>
      <div class="list" id="voiceList"><div class="empty">暂无音色<br>点上方按钮创建第一个</div></div>
    </section>
    <section class="card">
      <h2>
        <span class="h2-left">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
          角色库
        </span>
        <span class="sub" id="roleCount">0 个</span>
      </h2>
      <button class="btn" style="width:100%;margin-bottom:12px" onclick="openRoleDlg(null)">+ 新建角色</button>
      <div class="list" id="roleList"><div class="empty">暂无角色<br>角色 = 名字 + 音色 + 默认参数</div></div>
    </section>
  </div>

  <!-- 中：工作台 -->
  <div class="col center">
    <section class="card">
      <h2>
        <span class="h2-left">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          合成工作台
        </span>
      </h2>
      <div class="chips" id="activeChips"></div>
      <div class="player-box" id="instrPreviewWrap" style="margin:0 0 6px;padding:10px 13px">
        <div class="hint" style="margin:0 0 4px">本次将下发的指令（合成前可核对，杜绝「莫名带方言」）：</div>
        <div class="mono" id="instrPreview" style="font-size:.78rem;line-height:1.7;color:var(--muted)">（无指令：默认/纯克隆音色）</div>
      </div>

      <label for="emotionSelect">情感 / 语气（可选，让声音带情绪与风格）</label>
      <select id="emotionSelect" onchange="onEmotionChange()">
        <option value="" data-instr="">平静自然（默认）</option>
        <optgroup label="情感">
          <option value="em_gentle" data-instr="语气温柔亲切，情感细腻">温柔亲切</option>
          <option value="em_happy" data-instr="语气开心愉悦，充满活力">开心愉悦</option>
          <option value="em_excited" data-instr="语气激动热情，感染力强">激动热情</option>
          <option value="em_sad" data-instr="语气悲伤低沉，略带伤感">悲伤低沉</option>
          <option value="em_angry" data-instr="语气愤怒有力，掷地有声">愤怒有力</option>
        </optgroup>
        <optgroup label="风格">
          <option value="st_story" data-instr="像讲故事一样娓娓道来，节奏自然，有画面感">讲故事</option>
          <option value="st_news" data-instr="标准新闻播音腔，字正腔圆，客观庄重">新闻播音</option>
          <option value="st_recite" data-instr="字正腔圆，清晰咬字，抑扬顿挫，节奏从容">朗诵</option>
          <option value="st_classic" data-instr="朗诵古文，抑扬顿挫，节奏从容，字字清晰，古韵悠然">古文朗诵</option>
        </optgroup>
      </select>
      <div class="hint" id="emotionHint">情感与风格由自然语言指令驱动；克隆音色选择后自动解锁指令强度</div>

      <label for="accentSelect">口音 / 方言（可选，随文本语言生效）</label>
      <select id="accentSelect" onchange="onAccentChange()">
        <option value="" data-instr="">无口音（默认）</option>
        <optgroup label="中国方言">
          <option value="cn_guizhou" data-instr="用浓重的贵州方言说话，明显的贵州方言语调、咬字和用词，腔调要重">贵州话</option>
          <option value="cn_dongbei" data-instr="用浓重的东北方言说话，豪爽直率，明显的东北语调起伏和用词">东北话</option>
          <option value="cn_sichuan" data-instr="用浓重的四川方言说话，明显的川渝语调和韵味，腔调要重">四川话</option>
          <option value="cn_yue" data-instr="用浓重的粤语说话，地道广东话，明显的粤语声调和用词">粤语（广东话）</option>
          <option value="cn_henan" data-instr="用浓重的河南方言说话，明显的中原官话韵味和语调">河南话</option>
          <option value="cn_shaanxi" data-instr="用浓重的陕西方言说话，明显的关中话韵味和豪迈语调">陕西话</option>
          <option value="cn_shanghai" data-instr="用浓重的上海话（吴语）说话，明显的吴侬软语语调">上海话</option>
          <option value="cn_beijing" data-instr="用浓重的北京话说话，明显的儿化音和松弛京腔">北京话</option>
          <option value="cn_taiwan" data-instr="用明显的台湾腔说话，软糯亲切，句尾带轻柔语气词">台湾腔</option>
          <option value="cn_hunan" data-instr="用浓重的湖南方言说话，明显的湖南口音语调和用词">湖南话</option>
          <option value="cn_shandong" data-instr="用浓重的山东方言说话，明显的憨厚敦实语调">山东话</option>
          <option value="cn_minnan" data-instr="用浓重的闽南语说话，明显的闽台方言声调和用词">闽南话</option>
        </optgroup>
        <optgroup label="全球口音">
          <option value="en_us" data-instr="Speak with a General American accent">美式英语</option>
          <option value="en_gb" data-instr="Speak with a British Received Pronunciation accent">英式英语</option>
          <option value="en_southern" data-instr="Speak with a warm American Southern drawl">美国南方口音</option>
          <option value="en_scottish" data-instr="Speak with a Scottish accent">苏格兰口音</option>
          <option value="en_indian" data-instr="Speak with an Indian English accent">印度英语</option>
          <option value="en_australian" data-instr="Speak with an Australian accent">澳洲英语</option>
          <option value="en_newyork" data-instr="Speak with a New York accent">纽约口音</option>
          <option value="en_french" data-instr="Speak English with a French accent">法式口音英语</option>
          <option value="en_japanese" data-instr="Speak English with a Japanese accent">日式口音英语</option>
          <option value="en_spanish" data-instr="Speak English with a Spanish accent">西式口音英语</option>
        </optgroup>
      </select>
      <div class="hint" id="accentHint">口音由自然语言指令驱动；「克隆音色 + 口音」自动变为导向合成（可用指令强度）。支持的表演标记：<code>[笑]</code> <code>[咳嗽]</code> <code>[清嗓子]</code> <code>[叹气]</code></div>

      <label for="synthText">合成文本</label>
      <textarea id="synthText" placeholder="输入要合成的文本。表演标记：[笑] [咳嗽] [清嗓子] [叹气]；停顿标记：[停顿0.5] 插入半秒静音"></textarea>
      <textarea id="batchText" placeholder="每行一条文本，逐行批量合成（建议先用 2-3 行试跑，GPU 显存有限）" style="display:none"></textarea>
      <div class="hint" id="lenHint"></div>

      <div class="row" style="margin-top:4px">
        <div><label for="cfgScale">指令强度 cfg_scale</label>
          <input id="cfgScale" type="number" min="1" max="8" step="0.5" value="4">
          <div class="hint" id="cfgHint"></div></div>
        <div><label for="seed">随机种子 seed</label>
          <input id="seed" type="number" value="42"></div>
        <div><label for="speedSel">语速</label>
          <select id="speedSel">
            <option value="0.75">0.75× 慢速</option>
            <option value="0.85">0.85×</option>
            <option value="1" selected>1.0× 正常</option>
            <option value="1.15">1.15×</option>
            <option value="1.3">1.3×</option>
            <option value="1.5">1.5× 快速</option>
          </select></div>
        <div id="maxTokensWrap" style="display:none"><label for="maxTokens">最大生成 token</label>
          <input id="maxTokens" type="number" min="50" max="1500" step="50" value="600"></div>
      </div>

      <div class="row" style="margin-top:4px">
        <div><label for="langSel">文本语种</label>
          <select id="langSel">
            <option value="">Auto 自动识别（推荐）</option>
            <option value="Chinese">中文</option>
            <option value="English">English</option>
            <option value="Japanese">日本語</option>
            <option value="Korean">한국어</option>
            <option value="German">Deutsch</option>
            <option value="French">Français</option>
            <option value="Russian">Русский</option>
            <option value="Portuguese">Português</option>
            <option value="Spanish">Español</option>
            <option value="Italian">Italiano</option>
          </select></div>
        <div><label for="instructPolicySel">克隆音色遇到情绪 / 方言指令时</label>
          <select id="instructPolicySel">
            <option value="voice">保留克隆音色（指令不生效）</option>
            <option value="instruct">让指令生效（改用原生预设音色）</option>
          </select>
          <div class="hint">Qwen3-TTS 的克隆模型不接受指令；选「让指令生效」会换用 CustomVoice 预设音色承载方言与情绪。</div></div>
      </div>

      <div class="player" id="playerBox">
        <button class="btn btn-cta" id="synthBtn" onclick="synthesize()">
          <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          开始合成
        </button>
        <button class="btn" id="downloadBtn" onclick="downloadCurrent()" style="display:none">
          <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          下载 WAV
        </button>
      </div>

      <div class="status" id="synthStatus"></div>
      <div class="progress-track" id="progTrack" style="display:none"><div class="bar" id="progBar" style="animation:none;width:0%"></div></div>
      <div class="hint" id="segProgress"></div>
      <div class="hint" id="lastInstrWrap" style="display:none">本次指令：<span class="mono" id="lastInstr"></span></div>
      <div class="hint" id="engineNoteWrap" style="display:none;color:var(--warn,#b8860b)">引擎提示：<span id="engineNote"></span></div>

      <div class="player-box" id="playerBoxWrap" style="display:none">
        <audio id="player" controls></audio>
      </div>
    </section>
  </div>

  <!-- 右：历史 -->
  <div class="col">
    <section class="card">
      <h2>
        <span class="h2-left">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>
          合成历史
        </span>
        <span class="sub" id="histCount">0 条</span>
      </h2>
      <div class="hist" id="histList"><div class="empty">还没有合成记录<br>合成结果会自动保存在这里</div></div>
    </section>
  </div>
</main>

<!-- 音色编辑弹窗 -->
<dialog id="voiceDlg">
  <h3 id="voiceDlgTitle">新建音色</h3>
  <div class="hint" id="voiceDlgHint"></div>
  <input type="hidden" id="voiceId">
  <input type="hidden" id="voiceType">
  <label for="voiceName">音色名称</label>
  <input id="voiceName" placeholder="如：温柔小雅">
  <div id="voiceInstrWrap">
    <label for="voiceInstr">声音描述 / 方向指令（自然语言）</label>
    <textarea id="voiceInstr" placeholder="一位温柔自信的年轻女性，声音清晰，语气亲切，表达轻快而富有感染力。"></textarea>
  </div>
  <div id="voiceRefWrap" style="display:none">
    <label>参考音频（自己的声音，干净无噪音，几秒即可）</label>
    <label class="filelabel">
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
      <span id="voiceRefName">选择 wav 文件…</span>
      <input type="file" id="voiceRef" accept=".wav,.mp3,.flac,audio/*">
    </label>
    <label for="voiceRefText">参考音频的准确文字稿（必填，一字不差）</label>
    <textarea id="voiceRefText" placeholder="参考音频里说的完整内容，包括重复的部分"></textarea>
    <div class="hint">克隆音色不需要指令；「导向音色」在克隆基础上再加语气指令。</div>
    <div class="hint" id="voiceRefDur"></div>
    <button class="btn" id="voiceTrimBtn" style="display:none;margin-top:8px" onclick="trimRef()">裁剪参考音频到前 15 秒（提速）</button>
  </div>
  <div class="row">
    <div><label for="voiceCfg">默认 cfg_scale</label>
      <input id="voiceCfg" type="number" min="1" max="8" step="0.5" value="4"></div>
  </div>
  <div class="status err" id="voiceDlgErr" style="display:none"></div>
  <div class="dlg-actions">
    <button class="btn" onclick="voiceDlg.close()">取消</button>
    <button class="btn btn-cta" onclick="saveVoice()">保存音色</button>
  </div>
</dialog>

<!-- 角色编辑弹窗 -->
<dialog id="roleDlg">
  <h3 id="roleDlgTitle">新建角色</h3>
  <input type="hidden" id="roleId">
  <label for="roleName">角色名称</label>
  <input id="roleName" placeholder="如：故事旁白-小雅">
  <label for="roleDesc">角色简介</label>
  <input id="roleDesc" placeholder="这个人设负责什么场景">
  <label for="roleVoice">绑定音色</label>
  <select id="roleVoice"></select>
  <div class="row">
    <div><label for="roleCfg">默认 cfg_scale</label>
      <input id="roleCfg" type="number" min="1" max="8" step="0.5" value="4"></div>
    <div><label for="roleSeed">默认 seed</label>
      <input id="roleSeed" type="number" value="42"></div>
  </div>
  <div class="status err" id="roleDlgErr" style="display:none"></div>
  <div class="dlg-actions">
    <button class="btn" onclick="roleDlg.close()">取消</button>
    <button class="btn btn-cta" onclick="saveRole()">保存角色</button>
  </div>
</dialog>

<script>
const $ = (id) => document.getElementById(id);
const state = {
  voices: [], roles: [], history: [],
  voiceId: null, roleId: null, mode: "single",
  lastWavUrl: null, lastWavName: null, refFile: null,
  accent: "", accentLabel: "", histOffset: 0,
  emotion: "", emotionLabel: "", segPlaying: 0,
  segDefs: [], segFiles: [], failedIdx: [], engineNote: "",
};

const TYPE_LABEL = { design:"设计", clone:"克隆", direction:"导向" };

function api(path, opts) {
  return fetch(path, opts).then(async (r) => {
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    return r.json();
  });
}

function friendlyErr(msg) {
  const s = String(msg || "");
  if (/already running/i.test(s)) return "另一条合成正在进行，请等它完成后重试（多任务会自动排队）";
  if (/negative prompt/i.test(s)) return "该音色不支持指令强度（CFG），保持 cfg=1 或改用「导向音色」";
  if (/out of memory|cuda/i.test(s)) return "GPU 显存不足：请缩短文本或关闭占用 GPU 的程序";
  if (/不可达|refused|Failed to fetch/i.test(s)) return "合成服务（7860）未响应，请确认 API 已启动";
  return s;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;",
  }[c]));
}

function setStatus(msg, kind) {
  const el = $("synthStatus");
  if (!msg) { el.textContent = ""; el.className = "status"; return; }
  const spin = kind === "busy" ? '<span class="spinner"></span>' : "";
  el.innerHTML = spin + "<span>" + escapeHtml(msg) + "</span>";
  el.className = "status " + (kind || "ok");
}

/* ---------- 状态栏 ---------- */
async function refreshModel() {
  try {
    const m = await api("/api/model");
    $("apiDot").className = "dot on";
    $("apiText").textContent = "API 在线";
    $("modelInfo").textContent = (m.model_digest||"").slice(0,8) + " · " + m.frame_rate + " fps · ≤" + m.max_new_tokens + " tok";
    $("modelInfo").title = "model_digest: " + (m.model_digest||"");
  } catch (e) {
    $("apiDot").className = "dot off";
    $("apiText").textContent = "API 离线（7860）";
    $("modelInfo").textContent = "—";
  }
}

/* ---------- 音色 ---------- */
async function refreshVoices() {
  state.voices = await api("/api/voices");
  $("voiceCount").textContent = state.voices.length + " 个";
  const box = $("voiceList");
  if (!state.voices.length) {
    box.innerHTML = '<div class="empty">暂无音色<br>点上方按钮创建第一个</div>';
    return;
  }
  const vShown = state.voices.slice(0, 10);
  const vMore = state.voices.length > 10
    ? '<div class="hint" style="text-align:center">仅显示前 10 个（共 ' + state.voices.length + '）</div>' : "";
  box.innerHTML = vShown.map((v) => `
    <div class="item ${state.voiceId===v.id?"active":""}" onclick="pickVoice('${v.id}')" role="button" tabindex="0">
      <div class="grow"><div class="name">${escapeHtml(v.name)}</div>
      <div class="meta">${escapeHtml((v.instruction||v.ref_text||"").slice(0,26))}</div></div>
      <span class="tag ${v.type}">${TYPE_LABEL[v.type]||v.type}</span>
      <button class="iconbtn" title="编辑音色" onclick="event.stopPropagation();editVoice('${v.id}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>
      </button>
      <button class="iconbtn del" title="删除音色" onclick="event.stopPropagation();deleteVoice('${v.id}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
      </button>
    </div>`).join("") + vMore;
}

function pickVoice(id) {
  state.voiceId = state.voiceId === id ? null : id;
  state.roleId = null;
  syncCfgByVoice();
  renderChips(); refreshVoices(); refreshRoles();
  updateInstrPreview();
}

function syncCfgByVoice() {
  const v = state.voices.find((x) => x.id === state.voiceId);
  const el = $("cfgScale"), hint = $("cfgHint");
  const hasExtra = !!extraInstruction();
  if (v && v.type === "clone" && !hasExtra) {
    el.value = 1;
    hint.textContent = "克隆默认走标准普通话（cfg=1 已够用）；选「情感」可加强语气，选「口音」切换方言";
  } else if (v && v.type === "clone" && hasExtra) {
    if (parseFloat(el.value) === 1) el.value = 4;
    hint.textContent = "克隆 + 情感/口音 = 导向合成，可自由调节指令强度";
  } else {
    hint.textContent = "";
  }
}

function onAccentChange() {
  const sel = $("accentSelect");
  const opt = sel.options[sel.selectedIndex];
  state.accent = opt.dataset.instr || "";
  state.accentLabel = state.accent ? opt.text : "";
  $("accentHint").textContent = state.accent
    ? "口音指令（将下发给模型）：" + state.accent
    : "口音由自然语言指令驱动；「克隆音色 + 口音」自动变为导向合成（可用指令强度）。支持的表演标记：[笑] [咳嗽] [清嗓子] [叹气]";
  syncCfgByVoice();
  renderChips();
  updateInstrPreview();
}

function clearAccent() {
  $("accentSelect").value = "";
  onAccentChange();
}

function onEmotionChange() {
  const sel = $("emotionSelect");
  const opt = sel.options[sel.selectedIndex];
  state.emotion = opt.dataset.instr || "";
  state.emotionLabel = state.emotion ? opt.text : "";
  $("emotionHint").textContent = state.emotion
    ? "情感指令（将下发给模型）：" + state.emotion
    : "情感与风格由自然语言指令驱动；克隆音色选择后自动解锁指令强度";
  syncCfgByVoice();
  renderChips();
  updateInstrPreview();
}

function clearEmotion() {
  $("emotionSelect").value = "";
  onEmotionChange();
}

function extraInstruction() {
  return [state.emotion, state.accent].filter(Boolean).join("，");
}

const NORM_TEXT = "用标准普通话，发音标准，字正腔圆，吐字清晰";

function wantNorm() {
  // 克隆/默认音色默认走标准普通话；显式选方言口音则按口音来
  const v = state.voices.find((x) => x.id === state.voiceId);
  const isCloneish = !v || v.type === "clone";
  return isCloneish && !state.accent;
}

function updateInstrPreview() {
  // 只显示真正会下发的内容；角色简介仅备注不下发
  const parts = [];
  const v = state.voices.find((x) => x.id === state.voiceId);
  const vIns = v && v.instruction && v.instruction.trim() ? v.instruction.trim() : "";
  if (wantNorm() && !vIns) parts.push("【发音规范】" + NORM_TEXT);
  if (vIns) parts.push("【音色指令】" + vIns);
  if (state.emotion) parts.push("【情感】" + state.emotion);
  if (state.accent) parts.push("【口音】" + state.accent);
  const r = state.roles.find((x) => x.id === state.roleId);
  const note = r ? "　〔角色 " + r.name + " 备注：仅标识，不下发〕" : "";
  const el = $("instrPreview");
  el.textContent = (parts.length ? parts.join(" ｜ ") : "（无指令，纯克隆原始口音）") + note;
}

async function trimRef() {
  const id = $("voiceId").value;
  if (!id) return;
  try {
    const res = await api("/api/voices/" + id + "/trim", { method: "POST", body: "" });
    $("voiceRefDur").textContent = `已裁剪到 ${res.ref_duration.toFixed(1)} 秒，合成会明显提速`;
    $("voiceTrimBtn").style.display = "none";
    await refreshVoices();
  } catch (e) { dlgErr("裁剪失败：" + friendlyErr(e.message)); }
}

function editVoice(id) {
  const v = state.voices.find((x) => x.id === id);
  if (!v) return;
  state.refFile = null;
  dlgErr("");
  $("voiceId").value = v.id;
  $("voiceType").value = v.type;
  $("voiceName").value = v.name;
  $("voiceInstr").value = v.instruction || "";
  $("voiceRefText").value = v.ref_text || "";
  $("voiceRef").value = "";
  $("voiceRefName").textContent = v.ref_audio
    ? "已上传：" + v.ref_audio.split("/").pop() + "（重新选择可替换）"
    : "选择 wav 文件…";
  const rdur = v.ref_duration || 0;
  $("voiceRefDur").textContent = rdur > 0
    ? `参考音频时长 ${rdur.toFixed(1)} 秒` + (rdur > 15 ? " —— 过长会显著拖慢每次合成，建议裁到 15 秒内" : "（推荐 5-15 秒）")
    : "";
  $("voiceTrimBtn").style.display = (rdur > 15 && v.ref_audio) ? "" : "none";
  $("voiceCfg").value = v.cfg_scale ?? 4;
  const isRef = v.type !== "design";
  $("voiceRefWrap").style.display = isRef ? "" : "none";
  $("voiceInstrWrap").style.display = (v.type === "clone") ? "none" : "";
  $("voiceDlgTitle").textContent = "编辑音色：" + v.name;
  $("voiceDlgHint").textContent = "修改后点保存即生效；不重新选文件则保留原参考音频";
  $("voiceDlg").showModal();
}

function openVoiceDlg(type) {
  state.refFile = null;
  dlgErr("");
  $("voiceId").value = ""; $("voiceType").value = type;
  $("voiceName").value = ""; $("voiceInstr").value = "";
  $("voiceRefText").value = ""; $("voiceRef").value = "";
  $("voiceRefName").textContent = "选择 wav 文件…";
  $("voiceCfg").value = 4;
  const isRef = type !== "design";
  $("voiceRefWrap").style.display = isRef ? "" : "none";
  $("voiceInstrWrap").style.display = (type === "clone") ? "none" : "";
  $("voiceDlgTitle").textContent =
    type === "design" ? "设计音色（Voice Design）" :
    type === "clone" ? "克隆音色（Voice Clone）" : "导向音色（Voice Direction）";
  $("voiceDlgHint").textContent =
    type === "design" ? "用一句自然语言描述创造一个全新音色，无需参考音频。" :
    type === "clone" ? "上传自己的声音 + 准确文稿，复刻音色。" :
    "上传参考音频克隆音色，再用指令控制语气、语速与情绪。";
  $("voiceDlg").showModal();
}

$("voiceRef")?.addEventListener("change", (e) => {
  state.refFile = e.target.files[0] || null;
  $("voiceRefName").textContent = state.refFile ? state.refFile.name : "选择 wav 文件…";
});

function dlgErr(msg) {
  const el = $("voiceDlgErr");
  if (msg) { el.textContent = msg; el.style.display = ""; }
  else { el.textContent = ""; el.style.display = "none"; }
}

async function saveVoice() {
  const id = $("voiceId").value;
  const type = $("voiceType").value;
  const name = $("voiceName").value.trim();
  dlgErr("");
  if (!name) { dlgErr("请填写音色名称"); return; }
  if (type !== "design") {
    if (!id && !state.refFile) { dlgErr("请上传参考音频（自己的声音，几秒即可）"); return; }
    if (!$("voiceRefText").value.trim()) {
      dlgErr("请填写「参考音频的准确文字稿」——与音频里说的话一字不差，这是克隆的必要条件"); return;
    }
  }
  const body = {
    name, type,
    instruction: $("voiceInstr").value.trim(),
    ref_text: $("voiceRefText").value.trim(),
    cfg_scale: parseFloat($("voiceCfg").value) || 4,
  };
  try {
    let voice;
    if (id) {
      voice = await api("/api/voices/" + id, {
        method: "PUT", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
    } else {
      voice = await api("/api/voices", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
    }
    if (state.refFile && type !== "design") {
      const fd = new FormData();
      fd.append("ref_audio", state.refFile);
      await api("/api/voices/" + voice.id + "/ref", { method: "POST", body: fd });
    }
    $("voiceDlg").close();
    setStatus("音色已保存：" + name, "ok");
    await refreshVoices(); renderChips();
  } catch (e) { dlgErr("保存失败：" + friendlyErr(e.message)); }
}

async function deleteVoice(id) {
  if (!confirm("删除该音色？")) return;
  try {
    await api("/api/voices/" + id, { method: "DELETE" });
    if (state.voiceId === id) state.voiceId = null;
    await refreshVoices(); renderChips();
  } catch (e) { setStatus("删除失败：" + friendlyErr(e.message), "err"); }
}

/* ---------- 角色 ---------- */
async function refreshRoles() {
  state.roles = await api("/api/roles");
  $("roleCount").textContent = state.roles.length + " 个";
  const box = $("roleList");
  if (!state.roles.length) {
    box.innerHTML = '<div class="empty">暂无角色<br>角色 = 名字 + 音色 + 默认参数</div>'; return;
  }
  box.innerHTML = state.roles.map((r) => `
    <div class="item ${state.roleId===r.id?"active":""}" onclick="pickRole('${r.id}')" role="button" tabindex="0">
      <div class="grow"><div class="name">${escapeHtml(r.name)}</div>
      <div class="meta">${escapeHtml(r.description||voiceNameOf(r.voice_id))}</div></div>
      <button class="iconbtn" title="编辑角色" onclick="event.stopPropagation();openRoleDlg('${r.id}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>
      </button>
      <button class="iconbtn del" title="删除角色" onclick="event.stopPropagation();deleteRole('${r.id}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
      </button>
    </div>`).join("");
}

function voiceNameOf(id) {
  const v = state.voices.find((x) => x.id === id);
  return v ? "音色：" + v.name : "";
}

function pickRole(id) {
  state.roleId = state.roleId === id ? null : id;
  if (state.roleId) {
    const r = state.roles.find((x) => x.id === id);
    if (r) {
      state.voiceId = r.voice_id || null;
      if (r.defaults) {
        $("cfgScale").value = r.defaults.cfg_scale ?? 4;
        $("seed").value = r.defaults.seed ?? 42;
      }
    }
  }
  syncCfgByVoice();
  renderChips(); refreshRoles(); refreshVoices();
  updateInstrPreview();
}

function openRoleDlg(id) {
  $("roleId").value = id || "";
  const r = id ? state.roles.find((x) => x.id === id) : null;
  $("roleDlgTitle").textContent = r ? "编辑角色" : "新建角色";
  $("roleName").value = r ? r.name : "";
  $("roleDesc").value = r ? (r.description || "") : "";
  $("roleCfg").value = r?.defaults?.cfg_scale ?? 4;
  $("roleSeed").value = r?.defaults?.seed ?? 42;
  $("roleVoice").innerHTML = state.voices.map((v) =>
    `<option value="${v.id}">${escapeHtml(v.name)}（${TYPE_LABEL[v.type]||v.type}）</option>`).join("");
  if (r?.voice_id) $("roleVoice").value = r.voice_id;
  $("roleDlgErr").style.display = "none";
  $("roleDlg").showModal();
}

async function saveRole() {
  const id = $("roleId").value;
  const name = $("roleName").value.trim();
  const err = $("roleDlgErr");
  err.style.display = "none";
  if (!name) { err.textContent = "请填写角色名称"; err.style.display = ""; return; }
  const body = {
    name,
    description: $("roleDesc").value.trim(),
    voice_id: $("roleVoice").value || null,
    defaults: {
      cfg_scale: parseFloat($("roleCfg").value) || 4,
      seed: parseInt($("roleSeed").value) || 42,
    },
  };
  try {
    if (id) {
      await api("/api/roles/" + id, { method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
    } else {
      await api("/api/roles", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
    }
    $("roleDlg").close();
    setStatus("角色已保存：" + name, "ok");
    await refreshRoles(); renderChips();
  } catch (e) { err.textContent = "保存失败：" + friendlyErr(e.message); err.style.display = ""; }
}

async function deleteRole(id) {
  if (!confirm("删除该角色？")) return;
  try {
    await api("/api/roles/" + id, { method: "DELETE" });
    if (state.roleId === id) state.roleId = null;
    await refreshRoles(); renderChips();
  } catch (e) { setStatus("删除失败：" + friendlyErr(e.message), "err"); }
}

/* ---------- 工作台 ---------- */
function renderChips() {
  const box = $("activeChips");
  const v = state.voices.find((x) => x.id === state.voiceId);
  const r = state.roles.find((x) => x.id === state.roleId);
  const parts = [];
  if (r) parts.push(`<span class="chip active" onclick="pickRole('${r.id}')" title="点击取消">角色：${escapeHtml(r.name)} ✕</span>`);
  if (v) parts.push(`<span class="chip active" onclick="pickVoice('${v.id}')" title="点击取消">音色：${escapeHtml(v.name)} ✕</span>`);
  if (state.emotionLabel) parts.push(`<span class="chip active" onclick="clearEmotion()" title="点击移除">情感：${escapeHtml(state.emotionLabel)} ✕</span>`);
  if (state.accentLabel) parts.push(`<span class="chip active" onclick="clearAccent()" title="点击移除">口音：${escapeHtml(state.accentLabel)} ✕</span>`);
  if (!parts.length) parts.push('<span class="chip flat">未选择角色/音色（用默认音色合成）</span>');
  box.innerHTML = parts.join("");
}

function estimateSeconds(chars) {
  return Math.max(12, Math.round(15 + chars * 2.0));
}

function updateLenHint() {
  const el = $("synthText"), hint = $("lenHint");
  const n = (el.value || "").replace(/\s/g, "").length;
  if (state.mode === "single" && n > 120) {
    const est = estimateSeconds(n);
    hint.textContent = `长文本（${n} 字）：本机显存有限，预计需要 ${Math.ceil(est/60)} 分钟左右，请耐心等待进度提示；追求交互速度可按句号分段合成`;
  } else if (state.mode === "single" && n > 0) {
    const est = estimateSeconds(n);
    hint.textContent = est > 60 ? `预计约 ${Math.ceil(est/60)} 分钟` : `预计约 ${est} 秒`;
  } else {
    hint.textContent = "";
  }
}
$("synthText")?.addEventListener("input", updateLenHint);

function splitText(t) {
  // 返回 [{text, gap}]；支持 [停顿N] / [pause:N] 标记生成段间静音
  const out = [];
  const re = /\[停顿\s*([\d.]+)\s*\]|\[pause\s*:\s*([\d.]+)\s*\]/gi;
  const chunks = [];
  let last = 0, m;
  while ((m = re.exec(t)) !== null) {
    chunks.push({ seg: t.slice(last, m.index), gap: parseFloat(m[1] || m[2] || 0.5) });
    last = re.lastIndex;
  }
  chunks.push({ seg: t.slice(last), gap: 0.25 });
  for (const c of chunks) {
    let cur = "";
    for (const ch of c.seg) {
      cur += ch;
      if ("。！？；!?;\n".includes(ch) && cur.trim().length >= 2) {
        out.push({ text: cur.trim(), gap: 0.25 }); cur = "";
      } else if (cur.length >= 55) {
        out.push({ text: cur.trim(), gap: 0.25 }); cur = "";
      }
    }
    if (cur.trim()) out.push({ text: cur.trim(), gap: c.gap });
    else if (out.length && c.gap > 0.25) out[out.length - 1].gap = c.gap;
  }
  return out;
}

function renderSegProgress(i, n, text) {
  const pct = Math.round((i / n) * 100);
  const bar = $("progBar");
  if (bar) bar.style.width = pct + "%";
  $("segProgress").textContent = n > 1
    ? `第 ${Math.min(i + 1, n)}/${n} 段 · 正在念：「${(text || "").slice(0, 16)}${(text || "").length > 16 ? "…" : ""}」 · 总进度 ${pct}%`
    : "";
}

function markSegDone(i, n) {
  const bar = $("progBar");
  if (bar) bar.style.width = Math.round(((i + 1) / n) * 100) + "%";
}

async function runSegments(idxs, segs, segFiles, t0) {
  const n = segs.length;
  const failed = [];
  for (const i of idxs) {
    const seg = segs[i];
    renderSegProgress(i, n, seg.text);
    try {
      const fd = new FormData();
      fd.append("text", seg.text);
      const extra = extraInstruction();
      if (extra) fd.append("accent", extra);
      if (wantNorm()) fd.append("norm", "1");
      if (state.voiceId) fd.append("voice_id", state.voiceId);
      if (state.roleId) fd.append("role_id", state.roleId);
      fd.append("cfg_scale", $("cfgScale").value || 4);
      fd.append("seed", $("seed").value || 42);
      fd.append("speed", $("speedSel").value || 1);
      const _lang = $("langSel").value || "";
      if (_lang) fd.append("language", _lang);
      const _policy = $("instructPolicySel").value || "voice";
      if (_policy && _policy !== "voice") fd.append("instruct_policy", _policy);
      if (n > 1) fd.append("no_history", "1");
      const res = await api("/api/synthesize", { method: "POST", body: fd });
      if (res.engine_note) state.engineNote = res.engine_note;
      segFiles[i] = (res.wav_url || "").split("/").pop();
      const player = $("player");
      const idle = player.paused || player.ended || !player.src;
      player.src = res.wav_url;
      $("playerBoxWrap").style.display = "";
      if (idle) player.play().catch(() => {});
      state.lastWavUrl = res.wav_url;
      state.lastWavName = (res.wav_url || "").split("/").pop();
      $("downloadBtn").style.display = "";
      markSegDone(i, n);
    } catch (e) {
      failed.push(i);
    }
  }
  return failed;
}

async function synthesize() {
  const text = state.mode === "single" ? $("synthText").value.trim() : $("batchText").value.trim();
  if (!text) { setStatus("请输入合成文本", "err"); return; }
  const btn = $("synthBtn");
  btn.disabled = true;
  $("playerBoxWrap").style.display = "none";
  $("player").pause();
  $("downloadBtn").style.display = "none";
  $("segProgress").textContent = "";

  const useSeg = state.mode === "single" && (text.replace(/\s/g, "").length > 60 || /\[停顿|\[pause/i.test(text));
  const segs = useSeg ? splitText(text) : [{ text, gap: 0.25 }];
  const multi = segs.length > 1;
  state.segDefs = segs;
  state.segFiles = new Array(segs.length).fill(null);
  state.failedIdx = [];
  state.engineNote = "";

  const chars = text.replace(/\s/g, "").length;
  const est = estimateSeconds(chars);
  const t0 = Date.now();
  $("progTrack").style.display = "";
  $("progBar").style.width = "0%";
  const timer = setInterval(() => {
    const el = ((Date.now() - t0) / 1000) | 0;
    const segNote = multi ? `第 ${Math.min(state.segPlaying + 1, segs.length)}/${segs.length} 段 · ` : "";
    setStatus(`合成中…${segNote}已用 ${el}s / 预计约 ${est > 90 ? Math.ceil(est / 60) + " 分钟" : est + " 秒"}（${multi ? "分段渐进，边合成边出稿" : "结果会自动播放"}）`, "busy");
  }, 1000);

  try {
    const idxs = segs.map((_, i) => i);
    let failed = await runSegments(idxs, segs, state.segFiles, t0);
    state.failedIdx = failed;
    const done = segs.length - failed.length;
    const secs = ((Date.now() - t0) / 1000).toFixed(1);

    if (done === 0) {
      setStatus("合成失败：全部分段未成功，请检查 API 状态后重试", "err");
      return;
    }

    // 合并已完成段（失败段跳过）
    const files = [];
    const gaps = [];
    for (let i = 0; i < segs.length; i++) {
      if (state.segFiles[i]) {
        files.push(state.segFiles[i]);
        gaps.push(segs[i].gap);
      }
    }
    let finalUrl = null, finalDur = 0;
    if (files.length > 1) {
      setStatus(`正在合并 ${files.length} 段…`, "busy");
      const merged = await api("/api/concat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files, gaps, text: text.slice(0, 120),
          voice_id: state.voiceId || null, role_id: state.roleId || null,
          speed: $("speedSel").value || 1,
        }),
      });
      finalUrl = merged.wav_url;
      finalDur = merged.duration;
      state.lastWavUrl = merged.wav_url;
      state.lastWavName = (merged.wav_url || "").split("/").pop();
      $("player").src = merged.wav_url;
      $("player").play().catch(() => {});
    } else {
      finalUrl = state.lastWavUrl;
      finalDur = 0;
    }
    const cfgNote = ($("cfgScale").value || 4) == 1 ? " · cfg=1" : ` · cfg=${$("cfgScale").value}`;
    const failNote = failed.length ? ` · ⚠ ${failed.length} 段失败` : "";
    setStatus(`合成完成：${(finalDur || 0).toFixed(2)}s · ${done}/${segs.length} 段 · 耗时 ${secs}s${cfgNote}${failNote}`, failed.length ? "err" : "ok");
    $("segProgress").textContent = failed.length
      ? `失败段：${failed.map((i) => "「" + segs[i].text.slice(0, 10) + "…」").join("、")} · 点「重试失败段」补齐`
      : "";
    if (failed.length) {
      const rb = document.createElement("button");
      rb.className = "btn";
      rb.textContent = `重试失败段（${failed.length}）`;
      rb.style.marginTop = "8px";
      rb.onclick = async () => {
        rb.disabled = true;
        rb.textContent = "重试中…";
        const f2 = await runSegments(state.failedIdx, state.segDefs, state.segFiles, t0);
        state.failedIdx = f2;
        rb.textContent = f2.length ? `仍有 ${f2.length} 段失败` : "重试完成 ✓";
        await refreshHistory();
      };
      $("segProgress").appendChild(rb);
    }
    const insEl = $("lastInstr");
    insEl.textContent = extraInstruction() || "（无指令：默认/纯克隆音色）";
    $("lastInstrWrap").style.display = "";
    const noteEl = $("engineNoteWrap");
    if (state.engineNote) {
      $("engineNote").textContent = state.engineNote;
      noteEl.style.display = "";
    } else {
      noteEl.style.display = "none";
    }
    await refreshHistory();
  } catch (e) {
    setStatus("合成失败：" + friendlyErr(e.message), "err");
  } finally {
    clearInterval(timer);
    $("progTrack").style.display = "none";
    btn.disabled = false;
    updateLenHint();
  }
}

function downloadCurrent() {
  if (!state.lastWavUrl) return;
  const a = document.createElement("a");
  a.href = state.lastWavUrl;
  a.download = state.lastWavName || "synth.wav";
  a.click();
}

/* ---------- 历史 ---------- */
const HIST_PAGE = 10;

function histNav(offset) {
  state.histOffset = Math.max(0, offset);
  renderHistPage();
}

async function refreshHistory(freshId) {
  state.history = await api("/api/history");
  $("histCount").textContent = state.history.length + " 条";
  if (state.histOffset === 0 || state.histOffset >= state.history.length) state.histOffset = 0;
  renderHistPage(freshId);
}

function renderHistPage(freshId) {
  const box = $("histList");
  const total = state.history.length;
  if (!total) {
    box.innerHTML = '<div class="empty">还没有合成记录<br>合成结果会自动保存在这里</div>'; return;
  }
  const end = total - state.histOffset;
  const start = Math.max(0, end - HIST_PAGE);
  const slice = state.history.slice(start, end).reverse();
  const navTop = state.histOffset > 0
    ? '<button class="btn" style="width:100%;min-height:36px" onclick="histNav(' + (state.histOffset - HIST_PAGE) + ')">← 回到最新</button>' : "";
  const navBottom = start > 0
    ? '<button class="btn" style="width:100%;min-height:36px" onclick="histNav(' + (state.histOffset + HIST_PAGE) + ')">查看更早 ' + Math.min(HIST_PAGE, start) + ' 条（共 ' + total + '）</button>'
    : (total > HIST_PAGE ? '<div class="hint" style="text-align:center">已到最早记录（共 ' + total + ' 条）</div>' : "");
  box.innerHTML = navTop + slice.map((h) => `
    <div class="item ${h.id===freshId?"fresh":""}">
      <div class="grow">
        <div class="name">${escapeHtml((h.text||"[批量]").slice(0,24))}</div>
        <div class="meta">${(h.duration||0).toFixed(1)}s · ${escapeHtml((h.created_at||"").slice(5,16))}</div>
      </div>
      <button class="iconbtn play" title="播放" onclick="playHist('${h.wav_url}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
      </button>
      <button class="iconbtn" title="下载" onclick="dlHist('${h.wav_url}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      </button>
      <button class="iconbtn del" title="删除" onclick="delHist('${h.id}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
      </button>
    </div>`).join("") + navBottom;
}

function playHist(url) {
  $("player").src = url;
  $("playerBoxWrap").style.display = "";
  $("player").play().catch(() => {});
}
function dlHist(url) {
  const a = document.createElement("a");
  a.href = url; a.download = url.split("/").pop(); a.click();
}
async function delHist(id) {
  try {
    await api("/api/history/" + id, { method: "DELETE" });
    await refreshHistory();
  } catch (e) { setStatus("删除失败：" + friendlyErr(e.message), "err"); }
}

/* ---------- 模式切换 ---------- */
$("modeSingle").addEventListener("click", () => setMode("single"));
$("modeBatch").addEventListener("click", () => setMode("batch"));
function setMode(m) {
  state.mode = m;
  $("modeSingle").classList.toggle("active", m === "single");
  $("modeBatch").classList.toggle("active", m === "batch");
  $("synthText").style.display = m === "single" ? "" : "none";
  $("batchText").style.display = m === "batch" ? "" : "none";
  $("maxTokensWrap").style.display = m === "batch" ? "" : "none";
  updateLenHint();
}

/* ---------- 快捷键 ---------- */
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    if (!$("synthBtn").disabled) synthesize();
  }
});

/* ---------- 启动 ---------- */
(async function init() {
  setMode("single");
  await Promise.all([refreshModel(), refreshVoices().catch(()=>[]), refreshRoles().catch(()=>[]), refreshHistory().catch(()=>[])]);
  renderChips();
  updateLenHint();
  updateInstrPreview();
  setInterval(refreshModel, 15000);
})();
</script>
</body>
</html>
"""


# ---------- HTTP Handler ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "BreezeTTSPanel/1.0"

    # ---- helpers ----
    def _json(self, obj, code: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"detail": msg}, code)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _serve_file(self, path: Path, ctype: str) -> None:
        if not path.is_file():
            self._err("not found", 404)
            return
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(raw)

    def _proxy_stream(self, api_path: str, body: bytes, content_type: str, timeout: int = 600,
                      save_as: str = ""):
        """流式透传：按块转发上游 PCM，不整段缓冲（W2-2 S1 修复）。

        save_as 非空时把整段 PCM 落成 WAV（文件名由前端指定），
        供前端分段渐进合成后交给 /api/concat 合并（W2-6）。
        """
        import urllib.parse as _up
        parsed = _up.urlparse(API_BASE)
        conn = None
        resp = None
        try:
            # 上游单并发：撞车（409）时排队重试，而不是直接失败
            with _INFER_LOCK:
                for attempt in range(60):
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    conn = _httpclient.HTTPConnection(
                        parsed.hostname, parsed.port, timeout=timeout)
                    conn.request("POST", api_path, body=body,
                                 headers={"Content-Type": content_type})
                    resp = conn.getresponse()
                    if resp.status != 409:
                        break
                    if attempt < 59:
                        time.sleep(2.0)
                if resp is None:
                    raise RuntimeError("no upstream response")
            if resp.status != 200:
                detail = resp.read()
                try:
                    detail = json.loads(detail).get("detail", detail[:200])
                except Exception:
                    detail = detail.decode("utf-8", "replace")[:200] if isinstance(detail, bytes) else str(detail)[:200]
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                err = json.dumps({"detail": _friendly_error(detail)}, ensure_ascii=False).encode()
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            # 200: stream chunks to browser
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Sample-Rate", resp.getheader("X-Sample-Rate", "24000"))
            self.send_header("X-Sample-Format", resp.getheader("X-Sample-Format", "s16le"))
            _note = resp.getheader("X-Adapter-Note", "")
            if _note:
                # 降级原因必须能到前端，不能像早期版本那样静默吞掉
                self.send_header("X-Adapter-Note", _note)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            collected = bytearray() if save_as else None
            while True:
                # 必须用 read1：read(n) 在 chunked 响应上会阻塞攒满 n 字节
                # 才返回（等于又加了一层缓冲，首包被推迟、总耗时反而变长）。
                chunk = resp.read1(8192)
                if not chunk:
                    break
                if collected is not None:
                    collected += chunk
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            # W2-7: 必须先落盘再发结束标记——否则客户端收到流结束的瞬间
            # 文件还没写完，紧接着调 /api/concat 会读不到（竞态）。
            save_error = None
            if collected is not None and collected:
                try:
                    safe = _safe_name(save_as)
                    if not safe.lower().endswith(".wav"):
                        safe += ".wav"
                    (AUDIOS / safe).write_bytes(
                        pcm_to_wav(bytes(collected), normalize=False))
                except Exception as exc:
                    save_error = exc
                    sys.stderr.write(f"[panel] 分段落盘失败 {save_as!r}: {exc!r}\n")
            if save_error is not None:
                # 不能发成功终止块：客户端会立刻调 /api/concat 然后拿不到文件，
                # 整段音频静默丢失。发一个非法 chunk 让浏览器判定本次传输失败，
                # 前端按"该段失败"处理并允许重试。
                self.wfile.write(b"!!save-failed\r\n")
                self.wfile.flush()
                return
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception as exc:
            # 静默会让浏览器只看到 network error，日志毫无线索
            sys.stderr.write(f"[panel] 流式合成中断: {exc!r}\n")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _infer_with_retry(self, api_path: str, body: bytes, content_type: str,
                          timeout: int = 600, headers_out: dict | None = None):
        """上游单并发：锁内排队 + 409 撞车自动重试（跨进程/外部客户端撞车也能消化）。"""
        return _post_with_retry(api_path, body, content_type, timeout, headers_out)

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        pass

    # ---- GET ----
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        # 剧本台路由（独立模块；惰性导入避免与 panel 互相 import）
        from script_routes import handle as _script_handle
        if _script_handle(self, "GET", path, b"", ""):
            return

        if path in ("/", "/index.html"):
            _web_index = ROOT / "web" / "index.html"
            if _web_index.is_file():
                raw = _web_index.read_bytes()
            else:
                raw = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        # W2-1: serve extracted static files from web/
        if path in ("/styles.css", "/app.js", "/app.css"):
            fpath = ROOT / "web" / path.lstrip("/")
            if fpath.is_file():
                ext_ctype = {".css": "text/css", ".js": "application/javascript"}[fpath.suffix]
                data = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ext_ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
        # W2-3c: 发音词典读取
        if path == "/api/dict":
            self._json(_load_dict().to_list())
            return
        # W2-3c: 朗读预览（原文 → 规范化 → 命中项）
        if path == "/api/norm/preview":
            return
        if path == "/api/voices":
            self._json(_load("voices.json"))
            return
        if path == "/api/roles":
            self._json(_load("roles.json"))
            return
        if path == "/api/history":
            items = _load("history.json")
            for h in items:
                h.setdefault("wav_url", "/api/audio/" + Path(h.get("wav", "")).name)
            self._json(items)
            return
        if path == "/api/model":
            try:
                with urlopen(API_BASE + "/v1/model", timeout=10) as resp:
                    self._json(json.loads(resp.read()))
            except Exception as e:
                self._err(f"API 7860 不可达: {e}", 502)
            return
        if path.startswith("/api/audio/"):
            target = _resolve_audio(path[len("/api/audio/"):])   # 模块级函数，不是方法
            if target is None:
                self._err("音频路径不合法", 400)
                return
            self._serve_file(target, "audio/wav")
            return
        if path.startswith("/api/refs/"):
            self._serve_file(REFS / _safe_name(path.rsplit("/", 1)[-1]), "audio/wav")
            return
        self._err("not found", 404)

    # ---- POST ----
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        ctype = self.headers.get("Content-Type", "")
        body = self._read_body()
        # 剧本台路由（独立模块；惰性导入避免与 panel 互相 import）
        from script_routes import handle as _script_handle
        if _script_handle(self, "POST", path, body, ctype):
            return


        # ---- W2-3c: 朗读预览 ----
        if path == "/api/norm/preview":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("请求体必须是 JSON")
            if not isinstance(d, dict):
                return self._err("请求体必须是 JSON 对象")
            text = str(d.get("text") or "")
            scope = str(d.get("voice_id") or "")
            instruction = str(d.get("instruction") or "").strip()
            accent = str(d.get("accent") or "").strip()
            emotion = str(d.get("emotion") or "").strip()
            normalized, changes = _apply_text_pipeline(text, "2", scope)
            # 预览"最终下发指令"的拼装逻辑（与合成端一致）
            parts = [x for x in (instruction, emotion, accent) if x]
            final_ins = "，".join(parts)
            cfg = float(d.get("cfg_scale") or 4)
            voice = None
            if scope:
                for v in _load("voices.json"):
                    if v["id"] == scope:
                        voice = v
                        break
            if voice and voice.get("instruction") and voice["instruction"] not in parts:
                final_ins = "，".join([voice["instruction"]] + parts) if parts else voice["instruction"]
            if not final_ins:
                final_ins = "用标准普通话，发音标准，字正腔圆，吐字清晰"
                if accent or emotion:
                    final_ins = ""
            if not final_ins:
                cfg = 1.0
            self._json({
                "original": text,
                "normalized": normalized,
                "final_text": normalized,
                "changes": changes,
                "instruction": final_ins,
                "cfg": cfg,
                "dict_count": len(_load_dict().entries),
            })
            return

        # ---- W2-3c: 发音词典增删 ----
        if path == "/api/dict":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("请求体必须是 JSON")
            if not isinstance(d, dict):
                return self._err("请求体必须是 JSON 对象")
            word = str(d.get("word") or "").strip()
            reading = str(d.get("reading") or "").strip()
            scope = str(d.get("scope") or "global").strip() or "global"
            if not word:
                return self._err("请填写要纠正的词")
            if not reading:
                return self._err("请填写正确的读法")
            dictionary = _load_dict()
            try:
                entry = dictionary.add({"word": word, "reading": reading, "scope": scope})
            except ValueError as exc:
                return self._err(str(exc))
            _save_dict(dictionary)
            self._json({"ok": True, "entry": entry.to_dict()})
            return

        if path == "/api/voices":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("invalid json")
            if not d.get("name"):
                return self._err("name required")
            vtype = d.get("type", "design")
            if vtype not in ("design", "clone", "direction"):
                return self._err("type must be design|clone|direction")
            # W2-7: 创建阶段允许文稿为空（草稿态）——正确流程是
            # 上传 → 质检 → 校对文稿 → 保存；强制校验放在 PUT（定稿）阶段。
            # 原型音色（F9）：只给 archetype 时用原型描述预填指令，
            # 让「按原型建音色」一次 POST 就能落库，不必前端拼文案。
            arch = str(d.get("archetype") or "").strip()
            instruction = str(d.get("instruction") or "").strip()
            if arch:
                from tts_pipeline.script import ARCHETYPES
                if arch not in ARCHETYPES:
                    # 拼错的原型会建出一个「没有音色描述」的空壳音色，
                    # 用户以为选了少年女，实际听到的是默认发音人。
                    return self._err(f"不认识的原型「{arch}」")
                if not instruction:
                    instruction = (ARCHETYPES[arch].get("instruction") or "").strip()
            voice = {
                "id": _new_id("v"),
                "name": d["name"],
                "type": vtype,
                "instruction": instruction,
                "archetype": arch,
                "ref_audio": None,
                "ref_text": d.get("ref_text", ""),
                "cfg_scale": float(d.get("cfg_scale", 4)),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            # 原子追加：并发建音色时不能各自读一份再整体覆盖回去
            _update_json("voices.json", lambda it: it.append(voice))
            self._json(voice, 201)
            return

        if path.startswith("/api/voices/") and path.endswith("/ref"):
            vid = path.split("/")[3]
            fields, files = parse_multipart(body, ctype)
            f = files.get("ref_audio")
            if not f:
                return self._err("ref_audio file required")
            fname, data = f
            ext = Path(fname).suffix.lower() or ".wav"
            if ext not in (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".webm"):
                return self._err("unsupported audio format")
            safe = _safe_name(f"{vid}_{int(time.time())}{ext}")
            (REFS / safe).write_bytes(data)
            dur = wav_duration(data)
            def _set_ref(items, _vid=vid, _safe=safe, _dur=dur):
                for v in items:
                    if v["id"] == _vid:
                        v["ref_audio"] = f"refs/{_safe}"
                        v["ref_duration"] = _dur

            _update_json("voices.json", _set_ref)
            # W2-3c: 参考音频质检（可选，失败不阻断上传）
            quality = None
            try:
                from tts_pipeline.ref_prep import prepare_wav_bytes, RefPrepError
                prepared = prepare_wav_bytes(data, target_sr=24000, max_s=60.0)
                quality = prepared.quality.as_dict()
                quality["prepared_duration_s"] = round(prepared.duration_s, 3)
                quality["window"] = [round(prepared.window[0], 3),
                                     round(prepared.window[1], 3)]
                quality["operations"] = list(prepared.operations)
            except Exception as exc:
                code = getattr(exc, "code", "")
                quality = {"ready": True, "score": None,
                           "issues": [{"code": code or "assess_failed",
                                       "message": str(exc)[:160],
                                       "severity": "warn"}]}
            self._json({"ok": True, "ref_audio": f"refs/{safe}",
                        "ref_duration": dur, "quality": quality})
            return

        if path.startswith("/api/voices/") and path.endswith("/trim"):
            vid = path.split("/")[3]
            # 定位只读一次；音频文件与 JSON 是两份独立数据，先处理文件，
            # 再对 JSON 做原子更新，避免把耗时 IO 关进锁里。
            items_now = _load("voices.json")
            target = next((v for v in items_now if v["id"] == vid), None)
            if not target or not target.get("ref_audio"):
                return self._err("该音色没有参考音频可裁剪", 404)
            rp = DATA / target["ref_audio"]
            if not rp.is_file():
                return self._err("参考音频文件缺失，请重新上传", 404)
            trimmed = trim_wav_bytes(rp.read_bytes(), 15.0)
            rp.write_bytes(trimmed)
            dur = wav_duration(trimmed)

            def _set_dur(items, _vid=vid, _dur=dur):
                for v in items:
                    if v["id"] == _vid:
                        v["ref_duration"] = _dur

            _update_json("voices.json", _set_dur)
            self._json({"ok": True, "ref_duration": dur})
            return

        if path == "/api/roles":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("invalid json")
            if not d.get("name"):
                return self._err("name required")
            # 剧本按名字找角色：同名角色会让后建的那个静默顶掉前一个，
            # 作者以为在改"雪峰"，实际合成用的是另一个人的音色。
            wanted = str(d["name"]).strip()
            try:
                speed = float(d.get("speed") or 1.0)
            except (TypeError, ValueError):
                speed = 1.0
            role = {
                "id": _new_id("r"),
                "name": wanted,
                "description": d.get("description", ""),
                "voice_id": d.get("voice_id"),
                # 剧本台依赖这两个字段：原型决定音色描述，默认情绪决定该角色的基调
                "archetype": d.get("archetype") or "custom",
                # 空串 = 未设置：剧本台据此走「自动推断」。
                # 若在这里填「中性」，会让所有无标签行都恒为中性，
                # 标点与关键词推断永远不触发。
                "default_emotion": d.get("default_emotion") or "",
                # 角色的语速与口音：同一句话由不同角色念出来，快慢与腔调也该不同
                "speed": min(2.0, max(0.5, speed)),
                "accent": str(d.get("accent") or "").strip(),
                "defaults": d.get("defaults") or {"cfg_scale": 4, "seed": 42},
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            # 查重放进 mutator：先查后写会让两个并发请求都通过检查，
            # 最后写进两条同名角色——正是这次要根治的问题。
            def _add(items, _role=role):
                if any(str(r.get("name") or "").strip() == _role["name"] for r in items):
                    raise _ValidationError(
                        f"已经有同名角色「{_role['name']}」了，换一个名字，或直接编辑那个角色")
                items.append(_role)

            try:
                _update_json("roles.json", _add)
            except _ValidationError as exc:
                return self._err(str(exc))
            self._json(role, 201)
            return

        if path == "/api/synthesize/stream":
            # W2-2: 流式合成，按块转发 PCM
            fields, files = parse_multipart(body, ctype)
            api_fields = {k: v for k, v in fields.items()}
            voice_id = api_fields.get("voice_id", "")
            role_id = api_fields.get("role_id", "")
            voice = None
            if voice_id:
                for v in _load("voices.json"):
                    if v["id"] == voice_id:
                        voice = v
                        break
                if voice is None:
                    return self._err(f"指定的音色不存在（{voice_id}），请重新选择")
            if role_id and not voice:
                role_rec = next((r for r in _load("roles.json")
                                 if r["id"] == role_id), None)
                if role_rec is None:
                    return self._err("指定的角色已不存在，请重新选择")
                # 角色没绑音色时不能静默回落到默认发音人：用户以为在听「雪峰」，
                # 实际听到的是通用音色，表现就是"角色不能生成语音"。
                if not (role_rec.get("voice_id") or "").strip():
                    return self._err(
                        f"角色「{role_rec.get('name', '')}」还没绑定音色："
                        "点该角色的编辑按钮选一个音色，或取消角色改用默认发音人")
                for v in _load("voices.json"):
                    if v["id"] == role_rec["voice_id"]:
                        voice = v
                        break
            # W2-3c: 文本规范化 + 发音词典（norm=2）——只应调用一次
            _scope = api_fields.get("voice_id", "") or ""
            _new_text, _text_changes = _apply_text_pipeline(
                api_fields.get("text", ""), api_fields.get("norm", ""), _scope)
            if _new_text:
                api_fields["text"] = _new_text
            ins = (api_fields.get("instruction") or "").strip()
            accent = (api_fields.get("accent") or "").strip()
            emotion = (api_fields.get("emotion") or "").strip()
            extra_parts = []
            if voice and voice.get("instruction"):
                extra_parts.append(voice["instruction"])
            if emotion:
                extra_parts.append(emotion)
            if accent:
                extra_parts.append(accent)
            # merged 只能包含"额外"指令；为空时不能退化成 ins 本身，
            # 否则下面会拼成「沉稳旁白，沉稳旁白」，模型会多生成数十秒音频。
            merged = "，".join(extra_parts)
            if merged:
                api_fields["instruction"] = (ins + "，" + merged) if ins else merged
            norm_flag = api_fields.get("norm", "")
            has_accent = bool(accent)
            if norm_flag == "1" and not has_accent and not ins:
                api_fields["instruction"] = "用标准普通话，发音标准，字正腔圆，吐字清晰"
            cfg_used = api_fields.get("cfg_scale", "4")
            if not (api_fields.get("instruction") or "").strip():
                api_fields["cfg_scale"] = "1"
                cfg_used = "1"
            elif voice and voice.get("type") == "clone" and not has_accent and not emotion:
                api_fields["cfg_scale"] = "1"
                cfg_used = "1"
            if voice:
                if voice.get("ref_text") and not api_fields.get("ref_text"):
                    api_fields["ref_text"] = voice["ref_text"]
                if voice.get("ref_audio"):
                    rp = DATA / voice["ref_audio"]
                    if rp.is_file():
                        files["ref_audio"] = (rp.name, rp.read_bytes())
            out_body, out_ctype = _remultipart(api_fields, files)
            _save_as = (api_fields.get("save_as") or "").strip()
            api_fields.pop("save_as", None)
            out_body, out_ctype = _remultipart(api_fields, files)
            self._proxy_stream("/v1/audio/speech", out_body, out_ctype,
                               save_as=_save_as)
            return

        if path == "/api/synthesize":
            fields, files = parse_multipart(body, ctype)
            text = (fields.get("text") or "").strip()
            if not text:
                return self._err("text required")
            # 组装上游参数
            api_fields = {
                "text": text,
                "cfg_scale": fields.get("cfg_scale", "4"),
                "seed": fields.get("seed", "42"),
            }
            # Qwen 适配层新增字段：显式语种、原生预设音色、克隆/指令冲突策略。
            # 留空时上游分别回落到 Auto / 自动推断 / 保音色，旧前端零改动可用。
            for _k in ("language", "speaker", "instruct_policy"):
                _v = (fields.get(_k) or "").strip()
                if _v:
                    api_fields[_k] = _v
            # W2-3c: 文本规范化 + 发音词典（norm=2）——此前这条路径漏了接线，
            # 导致 norm=2 只在预览和流式端点生效。
            _scope = fields.get("voice_id", "") or ""
            _new_text, _text_changes = _apply_text_pipeline(
                text, fields.get("norm", ""), _scope)
            if _new_text:
                api_fields["text"] = _new_text
            ins = fields.get("instruction", "")
            if ins:
                api_fields["instruction"] = ins
            ref_text = fields.get("ref_text", "")
            if ref_text:
                api_fields["ref_text"] = ref_text
            # 从音色/角色取参数
            voices = {v["id"]: v for v in _load("voices.json")}
            roles = {r["id"]: r for r in _load("roles.json")}
            role = roles.get(fields.get("role_id", ""))
            # 指定了角色却查不到：可能刚被删掉，同样不能静默回落
            if fields.get("role_id") and role is None:
                return self._err("指定的角色已不存在，请重新选择")
            # 显式指定了 voice_id 就以它为准；只选了角色而角色没绑音色则明确拒绝，
            # 否则会安静地用默认发音人，用户无从判断角色到底生效没有。
            if role is not None and not fields.get("voice_id") \
                    and not (role.get("voice_id") or "").strip():
                return self._err(
                    f"角色「{role.get('name', '')}」还没绑定音色："
                    "点该角色的编辑按钮选一个音色，或取消角色改用默认发音人")
            voice = None
            vid = fields.get("voice_id") or (role or {}).get("voice_id")
            if vid:
                voice = voices.get(vid)
            if fields.get("voice_id") and voice is None:
                return self._err(
                    f"指定的音色不存在（{fields.get('voice_id')}），请重新选择")
            if voice:
                _vref = voice.get("ref_audio")
                _vtxt = (voice.get("ref_text") or "").strip()
                if bool(_vref) != bool(_vtxt):
                    missing = "文字稿" if _vref else "参考音频"
                    return self._err(f"音色「{voice.get('name', '')}」缺{missing}（参考音频与文字稿必须成对），请点该音色的编辑按钮补齐后再合成")
                if _vref and _is_placeholder_ref_text(_vtxt):
                    # W2-7b: 占位符文稿 = 没填。放行会让模型学错对齐，
                    # 结果是"克隆不像"，而系统还不报错——必须点名拦截。
                    return self._err(
                        f"音色「{voice.get('name', '')}」的参考文稿还是占位符「{_vtxt}」。"
                        "请点该音色的编辑按钮，把参考音频里说的话一字不差地写进去，"
                        "否则克隆出来的声音会对不上，相似度会明显下降。")
                if _vref and not (DATA / _vref).is_file():
                    return self._err(f"音色「{voice.get('name', '')}」的参考音频文件缺失，请点编辑重新上传")
                if voice.get("instruction") and not ins:
                    api_fields["instruction"] = voice["instruction"]
                if voice.get("ref_text") and not ref_text:
                    api_fields["ref_text"] = voice["ref_text"]
                if voice.get("cfg_scale") and "cfg_scale" not in fields:
                    api_fields["cfg_scale"] = str(voice["cfg_scale"])
                if voice.get("ref_audio"):
                    rp = DATA / voice["ref_audio"]
                    if rp.is_file():
                        files["ref_audio"] = (rp.name, rp.read_bytes())
            # 默认普通话规范：克隆/默认音色且未显式选口音时注入（norm=1 由前端决策）
            if (fields.get("norm") or "") == "1":
                _ins0 = (api_fields.get("instruction") or "").strip()
                _acc0 = (fields.get("accent") or "").strip()
                if not _ins0 and not _acc0:
                    api_fields["instruction"] = "用标准普通话，发音标准，字正腔圆，吐字清晰"
            # CFG 规则：无指令模板（tts_plain / ref_clone_tata）无 negative prompt，cfg 必须=1
            accent = (fields.get("accent") or "").strip()
            if accent:
                ins = (api_fields.get("instruction") or "").strip()
                api_fields["instruction"] = f"{ins}，{accent}" if ins else accent
            cfg_used = api_fields.get("cfg_scale", "4")
            if not (api_fields.get("instruction") or "").strip():
                api_fields["cfg_scale"] = "1"
                cfg_used = "1"
            # 重新封 multipart 发给 7860（上游单并发，加锁排队 + 409 自动重试）
            # 取回上游响应头：X-Adapter-Note 承载静默降级原因，必须回显给 UI。
            out_body, out_ctype = _remultipart(api_fields, files)
            _up_hdrs: dict = {}
            code, raw = self._infer_with_retry("/v1/audio/speech", out_body, out_ctype,
                                               headers_out=_up_hdrs)
            if code != 200:
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception:
                    pass
                return
            try:
                _sp = float(fields.get("speed") or 1.0)
            except ValueError:
                _sp = 1.0
            _sp = min(2.0, max(0.5, _sp))
            raw = resample_pcm(raw, _sp)
            wav = pcm_to_wav(raw)
            hid = _new_id("h")
            fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{hid}.wav"
            (AUDIOS / fname).write_bytes(wav)
            if (fields.get("no_history") or "") != "1":
                def _append_hist(items, _row=None):
                    items.append({
                        "id": hid,
                        "role_id": fields.get("role_id"),
                        "voice_id": vid,
                        "text": text,
                        "params": {"cfg_scale": api_fields.get("cfg_scale"),
                                   "seed": api_fields.get("seed")},
                        "wav": f"audio/{fname}",
                        "duration": len(raw) / (SAMPLE_RATE * 2),
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    return items
                _update_json("history.json", _append_hist)
            self._json({
                "id": hid,
                "wav_url": f"/api/audio/{fname}",
                "duration": len(raw) / (SAMPLE_RATE * 2),
                "cfg_used": _safe_int(cfg_used, 1),
                "instruction_used": (api_fields.get("instruction") or "").strip(),
                "text_used": api_fields.get("text", ""),
                "engine_note": _engine_note(_up_hdrs),
                "engine_route": _up_hdrs.get("X-Adapter-Route", ""),
            })
            return

        if path == "/api/synthesize/batch":
            fields, files = parse_multipart(body, ctype)
            try:
                texts = json.loads(fields.get("texts") or "[]")
            except Exception:
                return self._err("texts must be JSON array")
            if not isinstance(texts, list) or not texts:
                return self._err("texts required")
            # W2-3c: 批量逐条规范化
            _bscope = fields.get("voice_id", "") or ""
            if (fields.get("norm") or "") == "2":
                _normed = []
                for _t in texts:
                    _nt, _ = _apply_text_pipeline(_t, "2", _bscope)
                    _normed.append(_nt)
                texts = _normed
            api_fields = {
                "texts": json.dumps(texts, ensure_ascii=False),
                "cfg_scale": fields.get("cfg_scale", "4"),
                "seed": fields.get("seed", "42"),
                "max_new_tokens": fields.get("max_new_tokens", "600"),
            }
            for _k in ("language", "speaker", "instruct_policy"):
                _v = (fields.get(_k) or "").strip()
                if _v:
                    api_fields[_k] = _v
            voices = {v["id"]: v for v in _load("voices.json")}
            roles = {r["id"]: r for r in _load("roles.json")}
            role = roles.get(fields.get("role_id", ""))
            # 指定了角色却查不到：可能刚被删掉，同样不能静默回落
            if fields.get("role_id") and role is None:
                return self._err("指定的角色已不存在，请重新选择")
            # 显式指定了 voice_id 就以它为准；只选了角色而角色没绑音色则明确拒绝，
            # 否则会安静地用默认发音人，用户无从判断角色到底生效没有。
            if role is not None and not fields.get("voice_id") \
                    and not (role.get("voice_id") or "").strip():
                return self._err(
                    f"角色「{role.get('name', '')}」还没绑定音色："
                    "点该角色的编辑按钮选一个音色，或取消角色改用默认发音人")
            voice = None
            vid = fields.get("voice_id") or (role or {}).get("voice_id")
            if vid:
                voice = voices.get(vid)
            if fields.get("voice_id") and voice is None:
                return self._err(
                    f"指定的音色不存在（{fields.get('voice_id')}），请重新选择")
            if voice:
                _vref = voice.get("ref_audio")
                _vtxt = (voice.get("ref_text") or "").strip()
                if bool(_vref) != bool(_vtxt):
                    missing = "文字稿" if _vref else "参考音频"
                    return self._err(f"音色「{voice.get('name', '')}」缺{missing}（参考音频与文字稿必须成对），请点该音色的编辑按钮补齐后再合成")
                if _vref and _is_placeholder_ref_text(_vtxt):
                    # W2-7b: 占位符文稿 = 没填。放行会让模型学错对齐，
                    # 结果是"克隆不像"，而系统还不报错——必须点名拦截。
                    return self._err(
                        f"音色「{voice.get('name', '')}」的参考文稿还是占位符「{_vtxt}」。"
                        "请点该音色的编辑按钮，把参考音频里说的话一字不差地写进去，"
                        "否则克隆出来的声音会对不上，相似度会明显下降。")
                if _vref and not (DATA / _vref).is_file():
                    return self._err(f"音色「{voice.get('name', '')}」的参考音频文件缺失，请点编辑重新上传")
                if voice.get("instruction"):
                    api_fields["instruction"] = voice["instruction"]
                if voice.get("ref_text"):
                    api_fields["ref_text"] = voice["ref_text"]
                if voice.get("ref_audio"):
                    rp = DATA / voice["ref_audio"]
                    if rp.is_file():
                        files["ref_audio"] = (rp.name, rp.read_bytes())
            if (fields.get("norm") or "") == "1":
                _ins0 = (api_fields.get("instruction") or "").strip()
                _acc0 = (fields.get("accent") or "").strip()
                if not _ins0 and not _acc0:
                    api_fields["instruction"] = "用标准普通话，发音标准，字正腔圆，吐字清晰"
            accent = (fields.get("accent") or "").strip()
            if accent:
                ins = (api_fields.get("instruction") or "").strip()
                api_fields["instruction"] = f"{ins}，{accent}" if ins else accent
            cfg_used = api_fields.get("cfg_scale", "4")
            if not (api_fields.get("instruction") or "").strip():
                api_fields["cfg_scale"] = "1"
                cfg_used = "1"
            out_body, out_ctype = _remultipart(api_fields, files)
            _up_hdrs: dict = {}
            code, raw = self._infer_with_retry("/v1/audio/speech/batch", out_body, out_ctype,
                                               headers_out=_up_hdrs)
            if code != 200:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            # 分段落盘（X-Segment-Bytes 在上游响应头；这里用整段+近似均分兜底）
            wav = pcm_to_wav(raw)
            hid = _new_id("h")
            fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{hid}_batch.wav"
            (AUDIOS / fname).write_bytes(wav)
            _update_json("history.json", lambda it: it.append({
                "id": hid,
                "role_id": fields.get("role_id"),
                "voice_id": vid,
                "text": f"[批量 {len(texts)} 条] " + " / ".join(t[:12] for t in texts[:3]),
                "params": {"cfg_scale": api_fields.get("cfg_scale"), "seed": api_fields.get("seed"), "count": len(texts)},
                "wav": f"audio/{fname}",
                "duration": len(raw) / (SAMPLE_RATE * 2),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }))
            self._json({
                "id": hid,
                "wav_url": f"/api/audio/{fname}",
                "duration": len(raw) / (SAMPLE_RATE * 2),
                "segments": [len(raw)],
                "count": len(texts),
                "cfg_used": _safe_int(cfg_used, 1),
                "instruction_used": (api_fields.get("instruction") or "").strip(),
                "engine_note": _engine_note(_up_hdrs),
                "engine_route": _up_hdrs.get("X-Adapter-Route", ""),
            })
            return

        if path == "/api/concat":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("invalid json")
            fnames = d.get("files") or []
            if not fnames:
                return self._err("files required")
            gaps = d.get("gaps") or []
            pcm_parts = []
            for i, fn in enumerate(fnames):
                fp = AUDIOS / _safe_name(str(fn))
                if not fp.is_file():
                    return self._err(f"分段文件不存在: {fn}", 404)
                raw = fp.read_bytes()
                pcm_parts.append(raw[44:] if raw[:4] == b"RIFF" else raw)
                if i < len(fnames) - 1:
                    try:
                        gap = float(gaps[i]) if i < len(gaps) else 0.25
                    except (TypeError, ValueError):
                        gap = 0.25
                    gap = min(5.0, max(0.0, gap))
                    if gap > 0:
                        pcm_parts.append(b"\x00\x00" * int(SAMPLE_RATE * gap))
            pcm = b"".join(pcm_parts)
            try:
                _sp = float(d.get("speed") or 1.0)
            except (TypeError, ValueError):
                _sp = 1.0
            pcm = resample_pcm(pcm, min(2.0, max(0.5, _sp)))
            wav = pcm_to_wav(pcm, normalize=True)
            hid = _new_id("h")
            fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{hid}_merged.wav"
            (AUDIOS / fname).write_bytes(wav)
            dur = len(pcm) / (SAMPLE_RATE * 2)
            _update_json("history.json", lambda it: it.append({
                "id": hid,
                "role_id": d.get("role_id"),
                "voice_id": d.get("voice_id"),
                "text": (d.get("text") or "[分段合并]")[:200],
                "params": {"merged_segments": len(fnames)},
                "wav": f"audio/{fname}",
                "duration": dur,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }))
            self._json({"id": hid, "wav_url": f"/api/audio/{fname}", "duration": dur, "segments": len(fnames)})
            return

        self._err("not found", 404)

    # ---- PUT ----
    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        body = self._read_body()
        # 剧本台路由（独立模块；惰性导入避免与 panel 互相 import）
        from script_routes import handle as _script_handle
        if _script_handle(self, "PUT", path, body, ""):
            return

        try:
            d = json.loads(body or b"{}")
        except Exception:
            return self._err("invalid json")

        if path.startswith("/api/voices/"):
            vid = path.rsplit("/", 1)[-1]
            def _apply(items, _vid=vid):
                for v in items:
                    if v["id"] == _vid:
                        for k in ("name", "type", "instruction", "ref_text", "archetype"):
                            if k in d:
                                v[k] = d[k]
                        if "cfg_scale" in d:
                            v["cfg_scale"] = float(d["cfg_scale"])
                        if v.get("type") in ("clone", "direction") and not (v.get("ref_text") or "").strip():
                            # 抛异常 = 本次不落盘（half-written 记录不会留在文件里）
                            raise _ValidationError(
                                "还不能保存：克隆/导向音色必须先填写「参考音频的准确文字稿」，与音频内容一字不差")
                        return {"found": v}
                return {"found": None}

            try:
                res = _update_json("voices.json", _apply)
            except _ValidationError as exc:
                return self._err(str(exc))
            if not res.get("found"):
                return self._err("voice not found", 404)
            return self._json(res["found"])

        if path.startswith("/api/roles/"):
            rid = path.rsplit("/", 1)[-1]
            wanted = str(d.get("name") or "").strip() if "name" in d else None
            if wanted is not None and not wanted:
                return self._err("角色名不能为空")

            def _apply(items, _rid=rid, _name=wanted):
                for r in items:
                    if r["id"] == _rid:
                        # 改名查重也放在锁内，否则两个并发改名都能通过
                        if _name and any(x["id"] != _rid
                                         and str(x.get("name") or "").strip() == _name
                                         for x in items):
                            raise _ValidationError(f"已经有同名角色「{_name}」了")
                        for k in ("description", "voice_id", "defaults",
                                  "archetype", "default_emotion", "accent"):
                            if k in d:
                                r[k] = d[k]
                        if _name:
                            r["name"] = _name
                        if "speed" in d:
                            try:
                                r["speed"] = min(2.0, max(0.5, float(d["speed"])))
                            except (TypeError, ValueError):
                                pass
                        return {"found": r}
                return {"found": None}

            try:
                res = _update_json("roles.json", _apply)
            except _ValidationError as exc:
                return self._err(str(exc))
            if not res.get("found"):
                return self._err("role not found", 404)
            return self._json(res["found"])

        self._err("not found", 404)

    # ---- DELETE ----
    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        # 剧本台路由（独立模块；惰性导入避免与 panel 互相 import）
        from script_routes import handle as _script_handle
        if _script_handle(self, "DELETE", path, b"", ""):
            return

        # W2-3c: 删除词典条目
        if path.startswith("/api/dict/"):
            from urllib.parse import unquote
            # urlparse 不解码百分号编码，中文词条必须先 unquote
            word = _safe_name(unquote(path.rsplit("/", 1)[-1]))
            scope = "global"
            query = urlparse(self.path).query
            if query:
                from urllib.parse import parse_qs
                scope = (parse_qs(query).get("scope") or ["global"])[0]
            dictionary = _load_dict()
            if not dictionary.remove(word, scope):
                return self._err("词典里没有这一条", 404)
            _save_dict(dictionary)
            self._json({"ok": True})
            return
        if path.startswith("/api/voices/"):
            vid = path.rsplit("/", 1)[-1]
            def _drop(items, _vid=vid):
                hit = any(v["id"] == _vid for v in items)
                if hit:
                    items[:] = [v for v in items if v["id"] != _vid]
                return {"hit": hit}

            res = _update_json("voices.json", _drop)
            if not res["hit"]:
                return self._err("voice not found", 404)
            return self._json({"ok": True})
        if path.startswith("/api/roles/"):
            rid = path.rsplit("/", 1)[-1]
            def _drop(items, _rid=rid):
                hit = any(r["id"] == _rid for r in items)
                if hit:
                    items[:] = [r for r in items if r["id"] != _rid]
                return {"hit": hit}

            res = _update_json("roles.json", _drop)
            if not res["hit"]:
                return self._err("role not found", 404)
            return self._json({"ok": True})
        if path.startswith("/api/history/"):
            hid = path.rsplit("/", 1)[-1]
            items = _load("history.json")
            target = next((h for h in items if h["id"] == hid), None)
            if not target:
                return self._err("history not found", 404)
            wp = DATA / target.get("wav", "")
            if wp.is_file():
                try:
                    wp.unlink()
                except OSError:
                    pass
            def _drop_hist(items, _hid=hid):
                items[:] = [h for h in items if h["id"] != _hid]

            _update_json("history.json", _drop_hist)
            return self._json({"ok": True})
        self._err("not found", 404)


def _remultipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """把 fields+files 重新封装为 multipart/form-data 请求体（发给 7860）。"""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
        )
    for k, (fname, data) in files.items():
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)
    print(f"BreezeTTS Studio panel: http://127.0.0.1:{PANEL_PORT}  (proxy -> {API_BASE}, port locked)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
