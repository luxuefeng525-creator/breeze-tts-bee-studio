"""Bee Studio 视频/成本/合成路由。panel.py 在 script_routes.handle 之后调它。

路由表：
  GET  /api/providers                       —— 四家 provider 列表（含定价元数据）
  GET  /api/cost/estimate                   —— 单家估算（query: provider, resolution, duration, prompt_len, is_image）
  GET  /api/cost/compare                    —— 四家对比（query: resolution, duration, prompt_len, is_image）
  POST /api/video/generate                  —— 发起视频生成（body: {provider, resolution, duration, prompt, image_url}）
  GET  /api/video/poll/{job_id}             —— 轮询一次
  POST /api/video/providers/{provider}/keys —— 暂不实现 Key 存储（前端暂时硬塞，后续加密落盘）
  POST /api/export/{project_id}             —— ffmpeg 合成 + ZIP 打包（切片 D，占位）
"""
from __future__ import annotations
import json, os, sys, time, threading, datetime, base64, sqlite3, contextlib
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

# 把 breeze-tts/ 放进 sys.path（api 子模块的 __file__ 在 breeze-tts/api/）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ------------------------------------------------------------------
# SQLite 任务持久化（A1：视频生成 + ffmpeg 导出）
# ------------------------------------------------------------------
_DB_PATH = _ROOT / "data" / "bee_tasks.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_db_lock = threading.Lock()
_db_ready = False


def _db_init() -> None:
    """建表（幂等，只调一次）。"""
    global _db_ready
    if _db_ready:
        return
    with _db_lock, contextlib.closing(sqlite3.connect(_DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS video_jobs (
          job_id TEXT PRIMARY KEY,
          provider TEXT, provider_label TEXT,
          status TEXT DEFAULT 'queued',
          video_url TEXT DEFAULT '',
          actual_cost REAL DEFAULT 0,
          error TEXT DEFAULT '',
          prompt TEXT DEFAULT '',
          resolution TEXT DEFAULT '',
          duration REAL DEFAULT 0,
          created_at TEXT,
          updated_at TEXT,
          poll_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS export_jobs (
          job_id TEXT PRIMARY KEY,
          project_id TEXT,
          status TEXT DEFAULT 'pending',
          output_path TEXT DEFAULT '',
          output_duration REAL DEFAULT 0,
          zip_path TEXT DEFAULT '',
          error TEXT DEFAULT '',
          created_at TEXT,
          updated_at TEXT,
          poll_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS scripts (
          project_id TEXT PRIMARY KEY,
          title TEXT,
          genre TEXT,
          shots_json TEXT,
          prompt TEXT,
          llm_backend TEXT,
          created_at TEXT
        );
        """)
        # 迁移：POST /api/bee/script/{pid}/shots 要写 updated_at，但早期建的表没有这一列，
        # CREATE TABLE IF NOT EXISTS 不会补列 —— 老库会一直 500「no such column: updated_at」，
        # 前端 autosave 的 catch 又把错误吞掉，表现为「镜头编辑永远存不进去」。
        try:
            conn.execute("ALTER TABLE scripts ADD COLUMN updated_at TEXT")
        except sqlite3.OperationalError:
            pass  # 已经有了
        # 迁移：导出结果要回传 ZIP 路径，早期表没有这一列
        try:
            conn.execute("ALTER TABLE export_jobs ADD COLUMN zip_path TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # 已经有了
        # 迁移：上传进 ComfyUI input 的参考图副本名。轮询线程活在内存里，
        # 面板一重启就丢了名字，那张副本会永久留在 input 目录（实测漏过一张）。
        try:
            conn.execute("ALTER TABLE video_jobs ADD COLUMN staged_image TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # 已经有了
        conn.commit()
    _db_ready = True


def _video_job_write(job: dict) -> None:
    """写入/更新视频任务。"""
    _db_init()
    job = dict(job)
    job.setdefault("created_at", datetime.datetime.now().isoformat())
    job.setdefault("staged_image", "")
    job["updated_at"] = datetime.datetime.now().isoformat()
    with _db_lock, contextlib.closing(sqlite3.connect(_DB_PATH)) as conn:
        conn.execute("""
            INSERT INTO video_jobs(job_id,provider,provider_label,status,video_url,actual_cost,error,prompt,resolution,duration,created_at,updated_at,poll_count,staged_image)
            VALUES(:job_id,:provider,:provider_label,:status,:video_url,:actual_cost,:error,:prompt,:resolution,:duration,:created_at,:updated_at,:poll_count,:staged_image)
            ON CONFLICT(job_id) DO UPDATE SET
              status=excluded.status, video_url=excluded.video_url,
              actual_cost=excluded.actual_cost, error=excluded.error,
              updated_at=excluded.updated_at, poll_count=excluded.poll_count,
              staged_image=excluded.staged_image
        """, job)
        conn.commit()


def _video_job_read(job_id: str) -> dict | None:
    """读视频任务，返回 dict 或 None。"""
    _db_init()
    with _db_lock, contextlib.closing(sqlite3.connect(_DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM video_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def _video_job_list(limit: int = 50) -> list[dict]:
    _db_init()
    with _db_lock, contextlib.closing(sqlite3.connect(_DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM video_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------
# Provider 构建 + 后台轮询（可复用于重启恢复）
# ------------------------------------------------------------------
_PROVIDER_CLASSES = {
    "jimeng": ("api.providers.jimeng", "JimengProvider"),
    "kling":  ("api.providers.kling",  "KlingProvider"),
    "vidu":   ("api.providers.vidu",   "ViduProvider"),
    "wanx":   ("api.providers.wanx",   "WanxProvider"),
    "comfyui": ("api.providers.comfyui", "ComfyUIProvider"),
}


def _make_provider(provider: str):
    """按名字构建 provider 实例。comfyui 跑在本机，不需要 Key。"""
    import importlib
    mod_name, cls_name = _PROVIDER_CLASSES[provider]
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)
    api_key = "" if provider == "comfyui" else _get_key(provider)
    return cls(api_key=api_key)


def _role_by_id(role_id: str) -> dict | None:
    """从 roles.json 读角色。

    角色库是 TTS 面板与 Bee Studio 共用的唯一主键（roles.json / role.id）：
    一个角色同时挂音色（voice_id）、原型（archetype）与视频参考图（ref_image），
    不再为视频单独建一份角色表。
    """
    data_dir = Path(os.environ.get("BREEZE_DATA") or (_ROOT / "data"))
    f = data_dir / "roles.json"
    try:
        roles = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    return next((r for r in roles if r.get("id") == role_id), None)


def _ref_image_for_cloud(role: dict) -> str:
    """把角色的 ref_image 变成云端能拉取的绝对 URL。

    四家云端 provider 都把 image_url 原样发给第三方 API，本机 127.0.0.1
    或局域网地址它拉不到。所以要么角色里存的就是公网 URL，要么配
    BEE_PUBLIC_BASE（内网穿透/图床域名）把 /api/refs/... 拼成公网地址。
    """
    from api.providers import ProviderError
    raw = str(role.get("ref_image") or "").strip()
    if not raw:
        raise ProviderError(f"角色「{role.get('name')}」还没有参考图，请先在角色库里上传")
    if raw.startswith(("http://", "https://")):
        return raw
    base = os.environ.get("BEE_PUBLIC_BASE", "").strip().rstrip("/")
    if not base:
        raise ProviderError(
            "参考图现在是本机文件，云端模型拉不到。请设置环境变量 BEE_PUBLIC_BASE"
            "（例如 https://你的域名）把 /api/refs/ 暴露到公网，"
            "或给这个角色填公网图片 URL 后再出片")
    return base + raw


def _ref_image_local(role: dict) -> str:
    """本地 provider 用：把 /api/refs/... 映射成本机绝对路径，返回 file: 引用。

    ComfyUI 在同一台机器上，读不到 URL，只能读文件；provider 会负责
    裁剪与上传到 ComfyUI 的 input 目录。
    """
    from api.providers import ProviderError
    raw = str(role.get("ref_image") or "").strip()
    if not raw:
        raise ProviderError(f"角色「{role.get('name')}」还没有参考图，请先在角色库里上传")
    data_dir = Path(os.environ.get("BREEZE_DATA") or (_ROOT / "data"))
    if raw.startswith("/api/refs/"):
        p = (data_dir / "refs" / raw[len("/api/refs/"):]).resolve()
    else:
        p = Path(raw).resolve()
    if not p.is_file():
        raise ProviderError("参考图文件不存在，请重新上传")
    return "file:" + str(p)


def _require_ref_consent(role: dict) -> None:
    """肖像授权是硬门槛：没有授权记录就不许把图送到第三方。"""
    from api.providers import ProviderError
    if not str(role.get("ref_consent_at") or "").strip():
        raise ProviderError(
            f"角色「{role.get('name')}」的参考图缺少肖像授权记录，"
            "请重新上传并在弹窗里勾选授权确认")


def _poll_video_job(prov, job_id: str, result, budget: float) -> None:
    """后台轮询直到终态或超时。抽成独立函数是为了面板重启后能重新挂载。"""
    deadline = time.time() + budget
    while time.time() < deadline:
        time.sleep(5)
        try:
            r = prov.poll(result, job_id)
            j = _video_job_read(job_id)
            if not j:
                return  # DB 里没了，不轮了
            j["status"] = r.status
            j["video_url"] = r.video_url
            j["actual_cost"] = r.actual_cost
            j["error"] = r.error
            j["poll_count"] = int(j.get("poll_count") or 0) + 1
            _video_job_write(j)
            if r.status in ("succeeded", "failed"):
                return
        except Exception as e:
            j = _video_job_read(job_id)
            if j:
                j["error"] = str(e)[:200]
                _video_job_write(j)
    # 超时
    j = _video_job_read(job_id)
    if j and j["status"] not in ("succeeded", "failed"):
        j["status"] = "failed"
        j["error"] = f"轮询超时（{int(budget)}s）"
        _video_job_write(j)
        # 超时也是终态：参考图副本要删、显存要还，否则残留 + 语音合成被迫换页
        name = str(j.get("staged_image") or "")
        if name:
            try:
                prov.drop_staged(name)
            except Exception:
                pass
            j["staged_image"] = ""
            _video_job_write(j)
        try:
            prov.release_if_idle()
        except Exception:
            pass


def resume_pending_video_jobs() -> None:
    """面板启动时，把没跑完的视频任务重新接回轮询。

    轮询线程活在进程内，面板一重启就没了；而 GET /api/video/poll/{id}
    只读数据库，于是任务会永远停在 running —— 其实 ComfyUI 早就出完片了，
    用户看到的是界面一直转圈（僵尸任务）。这里在启动时补回线程。
    """
    try:
        jobs = _video_job_list(200)
    except Exception:
        return
    for job in jobs:
        if job.get("status") not in ("queued", "running"):
            continue
        jid = job.get("job_id")
        name = job.get("provider")
        if not jid or name not in _PROVIDER_CLASSES:
            continue
        budget = 1800 if name == "comfyui" else 300
        try:
            prov = _make_provider(name)
        except Exception as e:
            j = _video_job_read(jid)
            if j:
                j["status"] = "failed"
                j["error"] = f"面板重启后无法恢复轮询：{e}"[:200]
                _video_job_write(j)
            continue
        from api.providers import GenResult
        result = GenResult(job_id=jid, status="running")
        # 把重启前上传的参考图副本名接回来，任务收尾时才能删干净
        result._bee_staged_image = str(job.get("staged_image") or "")
        threading.Thread(target=_poll_video_job,
                         args=(prov, jid, result, budget), daemon=True).start()


def _export_job_write(job: dict) -> None:
    _db_init()
    job = dict(job)
    job.setdefault("created_at", datetime.datetime.now().isoformat())
    job.setdefault("zip_path", "")
    job["updated_at"] = datetime.datetime.now().isoformat()
    with _db_lock, contextlib.closing(sqlite3.connect(_DB_PATH)) as conn:
        conn.execute("""
            INSERT INTO export_jobs(job_id,project_id,status,output_path,output_duration,zip_path,error,created_at,updated_at,poll_count)
            VALUES(:job_id,:project_id,:status,:output_path,:output_duration,:zip_path,:error,:created_at,:updated_at,:poll_count)
            ON CONFLICT(job_id) DO UPDATE SET
              status=excluded.status, output_path=excluded.output_path,
              output_duration=excluded.output_duration, zip_path=excluded.zip_path,
              error=excluded.error,
              updated_at=excluded.updated_at, poll_count=excluded.poll_count
        """, job)
        conn.commit()


def _export_job_read(ejid: str) -> dict | None:
    _db_init()
    with _db_lock, contextlib.closing(sqlite3.connect(_DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM export_jobs WHERE job_id=?", (ejid,)).fetchone()
        return dict(row) if row else None


def _script_write(project_id: str, title: str, genre: str, shots_json: str,
                  prompt: str, llm_backend: str) -> None:
    """A3：剧本历史持久化。"""
    _db_init()
    now = datetime.datetime.now().isoformat()
    with _db_lock, contextlib.closing(sqlite3.connect(_DB_PATH)) as conn:
        conn.execute("""
            INSERT INTO scripts(project_id,title,genre,shots_json,prompt,llm_backend,created_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET
              title=excluded.title, genre=excluded.genre,
              shots_json=excluded.shots_json, prompt=excluded.prompt,
              llm_backend=excluded.llm_backend
        """, (project_id, title, genre, shots_json, prompt, llm_backend, now))
        conn.commit()


def _script_read(project_id: str) -> dict | None:
    _db_init()
    with _db_lock, contextlib.closing(sqlite3.connect(_DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM scripts WHERE project_id=?", (project_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            parsed = json.loads(d.pop("shots_json"))
            # 兼容：可能存的是 {"shots": [...]} 也可能直接存 [...]
            if isinstance(parsed, dict) and "shots" in parsed:
                d["shots"] = parsed["shots"]
            elif isinstance(parsed, list):
                d["shots"] = parsed
            else:
                d["shots"] = []
        except Exception:
            d["shots"] = []
        return d


def _new_export_job_id() -> str:
    return f"ej_{int(time.time())}_{os.getpid()}"


def _new_job_id() -> str:
    return f"vj_{int(time.time())}_{os.getpid()}"


def _data_media_path(rel: str) -> Path | None:
    """把 data 目录内的相对路径（如 images/x.png）解析成真实文件，越界返回 None。"""
    s = str(rel or "").strip().replace("\\", "/")
    if not s or s.startswith("/") or ":" in s or ".." in s.split("/"):
        return None
    base = Path(os.environ.get("BREEZE_DATA") or (_ROOT / "data")).resolve()
    try:
        target = (base / s).resolve()
    except (OSError, ValueError):
        return None
    if target != base and not target.is_relative_to(base):
        return None
    return target if target.is_file() else None


def _media_local_path(raw: str) -> str:
    """把前端传来的媒体引用归一成本地真实文件路径。

    前端存的配音是 `http://host:7861/api/audio/scripts/x/full.wav` 这种 URL，
    而 ffmpeg_export 用 `os.path.isfile()` 判断配音在不在——URL 一律判否，
    成片就会整条静音（用户看到"导出成功"，实际没声音）。这里把
    `/api/audio/` `/api/refs/` 前缀映射回 data/ 下的真实文件。

    认不出来的返回空串，由上层按「黑屏占位 / 静音」处理。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    data_dir = Path(os.environ.get("BREEZE_DATA") or (_ROOT / "data"))
    if s.startswith(("http://", "https://")):
        s = urlparse(s).path or ""
    for prefix, sub in (("/api/audio/", "audio"), ("/api/refs/", "refs")):
        if s.startswith(prefix):
            cand = (data_dir / sub / s[len(prefix):]).resolve()
            return str(cand) if cand.is_file() else ""
    p = Path(s)
    if p.is_absolute():
        return str(p) if p.is_file() else ""
    cand = (_ROOT / s).resolve()
    return str(cand) if cand.is_file() else ""


# ------------------------------------------------------------------
# Key 存储（先内存，后续落盘 + AES-256）
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# A2：AI Key 加密（纯 Python PBKDF2 + XOR；BEE_MASTER_KEY 环境变量）
# ------------------------------------------------------------------
def _crypto_key() -> bytes | None:
    """从环境变量读主密钥，sha256 成 32 字节。没设就返回 None（退化成明文 + Warning）。"""
    raw = os.environ.get("BEE_MASTER_KEY", "").strip()
    if not raw:
        return None
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _encrypt(text: str, key: bytes | None) -> str:
    """纯 Python XOR 流加密（PBKDF2 派生 key + AES-CTR 风格）。无 key 时返回 base64 原串。"""
    import hashlib, os, base64
    if not key:
        # 无主密钥：存 base64 + prefix:plain: 标识，方便以后升级时识别
        return "plain:" + base64.b64encode(text.encode()).decode()
    # 16 字节 nonce（盐）
    nonce = os.urandom(16)
    # PBKDF2 派生 32 字节 AES key
    derived = hashlib.pbkdf2_hmac("sha256", key, nonce, 100000, dklen=32)
    # 用 hashlib.sha256 生成 stream（AES 不可用的 fallback）
    stream = hashlib.sha256(nonce + b"|stream|00000001" + derived).digest()
    # 循环生成足够长的 stream
    out_bytes = text.encode("utf-8")
    while len(stream) < len(out_bytes):
        stream += hashlib.sha256(nonce + b"|stream|" + str(len(stream)).encode() + derived).digest()
    cipher = bytes(a ^ b for a, b in zip(out_bytes, stream))
    tag = hashlib.sha256(b"BEE-MAC|" + nonce + cipher + derived).digest()[:16]
    return "enc:" + base64.b64encode(nonce + tag + cipher).decode()


def _decrypt(b64: str, key: bytes | None) -> str:
    import hashlib, base64
    if b64.startswith("plain:"):
        return base64.b64decode(b64[len("plain:"):]).decode("utf-8", errors="replace")
    if not b64.startswith("enc:"):
        # 老版本明文兼容
        return b64
    try:
        raw = base64.b64decode(b64[len("enc:"):])
        nonce, tag, cipher = raw[:16], raw[16:32], raw[32:]
        if not key:
            raise RuntimeError("no BEE_MASTER_KEY env var — cannot decrypt")
        derived = hashlib.pbkdf2_hmac("sha256", key, nonce, 100000, dklen=32)
        stream = hashlib.sha256(nonce + b"|stream|00000001" + derived).digest()
        while len(stream) < len(cipher):
            stream += hashlib.sha256(nonce + b"|stream|" + str(len(stream)).encode() + derived).digest()
        plain = bytes(a ^ b for a, b in zip(cipher, stream))
        expected_tag = hashlib.sha256(b"BEE-MAC|" + nonce + cipher + derived).digest()[:16]
        if tag != expected_tag:
            raise RuntimeError("tag mismatch — wrong key or corrupted")
        return plain.decode("utf-8")
    except Exception as e:
        return f"[DECRYPT_FAILED: {e}]"


def _peel_plain(value: str, max_depth: int = 3) -> str:
    """剥掉可能被重复包裹的 plain:base64。

    历史 bug：把 _get_key() 的返回值又经 _keys_save() 存回去，
    会形成 plain:<base64("plain:<base64(key)>")> 的双层套娃，这里一并兼容。
    """
    import base64
    s = str(value or "")
    for _ in range(max_depth):
        if not s.startswith("plain:"):
            break
        try:
            s = base64.b64decode(s[len("plain:"):]).decode("utf-8")
        except Exception:
            break
    return s


def _keys_load() -> dict:
    """从 data/api_keys.json 加载并解密。无 BEE_MASTER_KEY 时退化成明文读。"""
    import warnings
    path = _ROOT / "data" / "api_keys.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    master_key = _crypto_key()
    if not master_key:
        # 环境变量没设：enc: 无法解密（置空防泄漏），plain: 必须解码后再用
        for k, v in list(data.items()):
            s = str(v)
            if s.startswith("enc:"):
                import sys
                print(f"⚠️ BEE_MASTER_KEY 未设置，无法解密 {k} 的 API Key。请 export BEE_MASTER_KEY=<your-secret>", file=sys.stderr)
                data[k] = ""
            else:
                data[k] = _peel_plain(s)
        return data
    return {k: _peel_plain(_decrypt(v, master_key)) for k, v in data.items()}


def _keys_save(d: dict) -> None:
    """加密并存盘。"""
    import warnings
    path = _ROOT / "data" / "api_keys.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    master_key = _crypto_key()
    if not master_key:
        import sys
        print("⚠️ BEE_MASTER_KEY 未设置，API Key 将以 base64 形式存储（非加密）。", file=sys.stderr)
    encrypted = {k: _encrypt(str(v), master_key) for k, v in d.items()}
    path.write_text(json.dumps(encrypted, ensure_ascii=False, indent=2), encoding="utf-8")


_API_KEYS: dict[str, str] = _keys_load()  # 启动时加载（解密后在内存里是明文）


def _get_key(provider: str) -> str:
    return _API_KEYS.get(provider, "")


# 明显的占位/测试 Key 特征：这些绝不能被当成「已配置」放行，否则会给用户
# 「配好了能生成」的错觉，实际一调用就 401。
_PLACEHOLDER_MARKERS = (
    "test_d", "dummy", "placeholder", "your_", "your-", "changeme",
    "xxxx", "example_key", "fake_key", "todo",
)


def _key_state(provider: str) -> tuple[bool, str]:
    """返回 (是否可用, 状态)。状态：ok / missing / placeholder。"""
    k = _get_key(provider)
    if not k:
        return False, "missing"
    low = k.lower()
    if len(k) < 16 or any(m in low for m in _PLACEHOLDER_MARKERS):
        return False, "placeholder"
    return True, "ok"


def _all_provider_names() -> list[str]:
    """所有 provider 名称（视频 + LLM）。用于 /api/providers 列表 + 测试端点。"""
    return [
        # 视频生成 providers
        "jimeng", "kling", "vidu", "wanx",
        # LLM providers
        "longcat", "openai",
    ]


# LLM providers metadata（给 /api/providers 列表用）
_LLM_LABELS = {
    "longcat": {"label": "LongCat 2.5", "type": "llm",
                "desc": "高性能 Agentic 模型 · OpenAI/Anthropic 兼容 · https://longcat.chat"},
    "openai":  {"label": "OpenAI GPT", "type": "llm",
                "desc": "OpenAI 官方 GPT-4o-mini 等 · https://platform.openai.com"},
}


# ------------------------------------------------------------------
# 模型 Registry — 内置 + 自定义（持久化 data/model_registry.json）
# ------------------------------------------------------------------
_MODEL_REGISTRY_FILE = _ROOT / "data" / "model_registry.json"

_BUILTIN_MODELS = [
    # 视频生成模型（4 家服务商，每家 2-3 个档位）
    {"id": "jimeng-seed21",  "name": "Seed 2.1",        "type": "video", "provider": "jimeng",   "model_name": "seedance-2.1",     "builtin": True},
    {"id": "jimeng-seed25",  "name": "Seed 2.5",        "type": "video", "provider": "jimeng",   "model_name": "seedance-2.5",     "builtin": True},
    {"id": "jimeng-seedev",  "name": "Seed-Evolving",   "type": "video", "provider": "jimeng",   "model_name": "seed-evolving",    "builtin": True},
    {"id": "kling-15",       "name": "Kling 1.5",       "type": "video", "provider": "kling",    "model_name": "kling-v1.5",       "builtin": True},
    {"id": "kling-30s",      "name": "Kling 3.0",       "type": "video", "provider": "kling",    "model_name": "kling-v3.0",       "builtin": True},
    {"id": "vidu-q3t",       "name": "Vidu Q3-turbo",   "type": "video", "provider": "vidu",     "model_name": "vidu-q3-turbo",    "builtin": True},
    {"id": "wanx-wan3",      "name": "Wan 3.0-video",  "type": "video", "provider": "wanx",     "model_name": "wan3.0-video",     "builtin": True},
    # LLM 模型（剧本生成）
    {"id": "longcat-25",     "name": "LongCat 2.5",    "type": "llm",   "provider": "longcat", "model_name": "longcat-2.5",      "builtin": True},
    {"id": "gpt-4o-mini",    "name": "GPT-4o-mini",    "type": "llm",   "provider": "openai",  "model_name": "gpt-4o-mini",      "builtin": True},
    {"id": "gpt-4o",         "name": "GPT-4o",         "type": "llm",   "provider": "openai",  "model_name": "gpt-4o",           "builtin": True},
    # 自动路由
    {"id": "auto-video",     "name": "Auto（自动选择最快可用）", "type": "video", "provider": "auto", "model_name": "auto", "builtin": True, "auto": True},
    {"id": "auto-llm",       "name": "Auto（自动选择最快可用）", "type": "llm",   "provider": "auto", "model_name": "auto", "builtin": True, "auto": True},
]


def _load_custom_models() -> list:
    """从 data/model_registry.json 读自定义模型（无则返回 []）。"""
    try:
        if _MODEL_REGISTRY_FILE.exists():
            data = json.loads(_MODEL_REGISTRY_FILE.read_text(encoding="utf-8"))
            return [m for m in data if isinstance(m, dict)]
    except Exception:
        pass
    return []


def _save_custom_models(models: list) -> None:
    _MODEL_REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MODEL_REGISTRY_FILE.write_text(
        json.dumps(models, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _all_models(model_type: str | None = None) -> list:
    """内置 + 自定义，按 type 过滤（None = 全部）。"""
    all_mods = _BUILTIN_MODELS + _load_custom_models()
    if model_type:
        all_mods = [m for m in all_mods if m.get("type") == model_type]
    return all_mods


# ------------------------------------------------------------------
# 辅助：请求解析 / 响应
# ------------------------------------------------------------------
def _parse_body(h: BaseHTTPRequestHandler) -> dict:
    raw = h.rfile.read(int(h.headers.get("Content-Length", 0) or 0))
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _query(h: BaseHTTPRequestHandler) -> dict[str, list[str]]:
    return parse_qs(urlparse(h.path).query, keep_blank_values=True)


def _h_json(h: BaseHTTPRequestHandler, obj, code: int = 200):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Access-Control-Allow-Origin", "*")
    h.end_headers()
    h.wfile.write(data)


def _h_text(h: BaseHTTPRequestHandler, text: str, code: int = 200):
    data = text.encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "text/plain; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


# ------------------------------------------------------------------
# GET 路由
# ------------------------------------------------------------------
def handle_get(h: BaseHTTPRequestHandler, path: str) -> bool:
    qs = _query(h)
    parts = [p for p in path.split("/") if p]

    # ---- GET /api/providers ----
    if parts == ["api", "providers"]:
        from api.cost_calc import list_providers, _PRICING_TABLE
        out = []
        for p in list_providers():
            if p["name"] == "comfyui":
                # 本地 ComfyUI 是本机服务，没有 Key 这回事；key_state=local 让前端
                # 不要弹"请配置 API Key"，直接当可用 provider
                out.append({
                    "name": p["name"], "label": p["label"],
                    "type": "video",
                    "pricing": _PRICING_TABLE.get(p["name"], {}),
                    "api_key_configured": True,
                    "key_state": "local",
                })
                continue
            ok, state = _key_state(p["name"])
            out.append({
                "name": p["name"], "label": p["label"],
                "type": "video",
                "pricing": _PRICING_TABLE.get(p["name"], {}),
                "api_key_configured": ok,
                "key_state": state,
            })
        for name, info in _LLM_LABELS.items():
            ok, state = _key_state(name)
            out.append({
                "name": name, "label": info["label"],
                "type": info["type"],
                "desc": info["desc"],
                "pricing": {},
                "api_key_configured": ok,
                "key_state": state,
            })
        _h_json(h, out)
        return True

    # ---- GET /api/models?type=video|llm ----
    if parts == ["api", "models"]:
        mtype = qs.get("type", [None])[0]
        _h_json(h, _all_models(mtype))
        return True

    # ---- GET /api/videos/list —— 本机成片列表（超分选片用）----
    if parts == ["api", "videos", "list"]:
        from api import video_upscale
        _h_json(h, {"ok": True, "items": video_upscale.list_videos()})
        return True

    # ---- GET /api/video/upscale/{job_id} —— 超分进度 ----
    if len(parts) == 4 and parts[0:3] == ["api", "video", "upscale"]:
        from api import video_upscale
        try:
            _h_json(h, {"ok": True, **video_upscale.status(parts[3])})
        except video_upscale.UpscaleError as e:
            _h_json(h, {"ok": False, "error": str(e)}, 404)
        return True

    # ---- GET /api/image/capabilities —— ControlNet / IP-Adapter 是否真可用 ----
    if parts == ["api", "image", "capabilities"]:
        from api import image_gen
        try:
            _h_json(h, {"ok": True, **image_gen.capabilities()})
        except Exception as e:
            _h_json(h, {"ok": False, "error": f"能力探测失败：{e}"}, 502)
        return True

    # ---- GET /api/images —— SD1.5 出图相册（最新在前）----
    if parts == ["api", "images"]:
        from api import image_gen
        try:
            limit = int(qs.get("limit", ["24"])[0])
        except ValueError:
            limit = 24
        _h_json(h, {"ok": True, "items": image_gen.list_images(limit)})
        return True

    # ---- GET /api/comfy/templates/local —— 本机 ComfyUI 已有的工作流 ----
    if parts == ["api", "comfy", "templates", "local"]:
        from api import comfy_templates
        _h_json(h, {"ok": True, "items": comfy_templates.local_list(),
                    "workflows_dir": str(comfy_templates.workflows_dir())})
        return True

    # ---- GET /api/comfy/templates —— 官方模板库（本地索引缓存，可按关键词搜）----
    if parts == ["api", "comfy", "templates"]:
        from api import comfy_templates
        try:
            limit = int(qs.get("limit", ["60"])[0])
        except ValueError:
            limit = 60
        try:
            items = comfy_templates.search(qs.get("q", [""])[0], limit,
                                           qs.get("media", [""])[0])
            _h_json(h, {"ok": True, "count": len(items), "items": items,
                        "workflows_dir": str(comfy_templates.workflows_dir())})
        except comfy_templates.TemplateError as e:
            _h_json(h, {"ok": False, "error": str(e)}, 502)
        return True

    # ---- GET /api/routes (调试用) ----
    if parts == ["api", "routes"]:
        _h_json(h, {
            "GET": [
                "/api/providers",
                "/api/cost/estimate?provider=X&resolution=Y&duration=N",
                "/api/cost/compare?resolution=Y&duration=N",
                "/api/video/poll/{job_id}",
                "/api/export/poll/{job_id}",
                "/api/routes",
            ],
            "POST": [
                "/api/providers/{provider}/keys",
                "/api/providers/{provider}/test",
                "/api/video/generate",
                "/api/export/{project_id}",
            ],
        })
        return True

    # ---- GET /api/cost/estimate ----
    if parts == ["api", "cost", "estimate"]:
        from api.cost_calc import estimate
        provider = qs.get("provider", ["jimeng"])[0]
        resolution = qs.get("resolution", ["720p"])[0]
        duration = float(qs.get("duration", ["5"])[0])
        prompt = qs.get("prompt", [""])[0]
        prompt_len = int(qs.get("prompt_len", [len(prompt)] or [0])[0] or 0)
        # 带不带参考图是两条完全不同的路：本地 comfyui 带图走 Wan2.2 5B（约 5 分钟），
        # 不带图走 1.3B（约 35 秒）。不解析这个参数就会把 5 分钟的活报成 35 秒。
        is_image = str(qs.get("is_image", ["0"])[0]).lower() in ("1", "true", "yes", "on")
        try:
            q = estimate(provider, resolution, duration,
                         prompt=prompt, prompt_len=prompt_len, is_image=is_image)
            _h_json(h, {
                "provider": q.provider, "label": q.provider_label,
                "resolution": q.resolution,
                "duration_input": duration,
                "duration_real": q.duration_real,
                "base_cost": round(q.base_cost, 2),
                "adjustment": round(q.adjustment, 2),
                "adjustment_label": q.adjustment_label,
                "total_cost": q.total_cost,
                "eta_sec": q.eta_sec,
                "supports_image_input": q.supports_image_input,
                "notes": q.notes,
            })
        except Exception as e:
            _h_json(h, {"error": str(e)}, 400)
        return True

    # ---- GET /api/cost/compare ----
    if parts == ["api", "cost", "compare"]:
        from api.cost_calc import compare
        resolution = qs.get("resolution", ["720p"])[0]
        duration = float(qs.get("duration", ["5"])[0])
        prompt = qs.get("prompt", [""])[0]
        # 与 /api/cost/estimate 同一口径：带图时本地 comfyui 是 5B 档，不是 1.3B
        is_image = str(qs.get("is_image", ["0"])[0]).lower() in ("1", "true", "yes", "on")
        try:
            quotes = compare(resolution, duration, prompt=prompt, is_image=is_image)
            _h_json(h, [
                {"provider": q.provider, "label": q.provider_label,
                 "total_cost": q.total_cost, "eta_sec": q.eta_sec,
                 "adjustment_label": q.adjustment_label, "notes": q.notes}
                for q in quotes
            ])
        except Exception as e:
            _h_json(h, {"error": str(e)}, 400)
        return True

    # ---- GET /api/video/poll/{job_id} ----
    if len(parts) == 4 and parts[0:3] == ["api", "video", "poll"]:
        job_id = parts[3]
        job = _video_job_read(job_id)
        if not job:
            _h_json(h, {"error": f"job {job_id} not found"}, 404)
            return True
        # 读的时候顺便 +1 poll_count（纯只读行为，别在 SQL update 里写，避免误触发）
        job["poll_count"] = int(job.get("poll_count") or 0) + 1
        _video_job_write(job)
        _h_json(h, job)
        return True

    # ---- GET /api/bee/video/jobs — Bee Studio 专属命名空间，避免与原产品冲突 ----
    if parts == ["api", "bee", "video", "jobs"]:
        # 列出最近 50 条视频任务（A1：持久化后可以重查）
        _h_json(h, _video_job_list(50))
        return True

    # ---- GET /api/export/poll/{job_id} ----
    if len(parts) == 4 and parts[0:3] == ["api", "export", "poll"]:
        ejid = parts[3]
        job = _export_job_read(ejid)
        if job:
            job["poll_count"] = int(job.get("poll_count") or 0) + 1
            _export_job_write(job)
        resp = dict(job) if job else {}
        if resp:
            # 前端读的是 mp4_path / duration_total，库里是 output_* 列名，补个别名免得两边对不上
            resp["mp4_path"] = resp.get("output_path", "")
            resp["duration_total"] = resp.get("output_duration", 0)
        _h_json(h, resp if resp else {"error": "job not found"}, 200 if resp else 404)
        return True

    # ---- GET /api/bee/scripts —— 剧本历史列表（摘要） ----
    if parts == ["api", "bee", "scripts"]:
        _db_init()
        with _db_lock, contextlib.closing(sqlite3.connect(_DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT project_id, title, prompt, llm_backend, created_at, "
                "LENGTH(shots_json) as shots_len FROM scripts "
                "ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        scripts_list = []
        for r in rows:
            raw_shots_len = r["shots_len"] or 0
            # 粗略估 shot_count：实际需 JSON 解析，但列表只展示摘要
            shot_count = 0
            try:
                full = _script_read(r["project_id"])
                if full and full.get("shots"):
                    shot_count = len(full["shots"])
            except Exception:
                pass
            scripts_list.append({
                "project_id": r["project_id"],
                "title": r["title"] or "(未命名剧本)",
                "prompt": (r["prompt"] or "")[:80],
                "shot_count": shot_count,
                "llm_backend": r["llm_backend"],
                "created_at": r["created_at"],
            })
        _h_json(h, {"scripts": scripts_list, "total": len(scripts_list)})
        return True

    # ---- GET /api/bee/script/{project_id} —— A3 剧本历史回查 ----
    if len(parts) == 4 and parts[0:3] == ["api", "bee", "script"]:
        pid = parts[3]
        sc = _script_read(pid)
        if sc:
            _h_json(h, sc)
        else:
            _h_json(h, {"error": "script not found", "project_id": pid}, 404)
        return True

    return False


# ------------------------------------------------------------------
# POST 路由
# ------------------------------------------------------------------
def handle_post(h: BaseHTTPRequestHandler, path: str, body_bytes: bytes = b"", ctype: str = "") -> bool:
    # body 已经由 panel.py 读过并传进来了，不要重读 rfile
    body = {}
    if body_bytes:
        try:
            body = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            body = {}
    parts = [p for p in path.split("/") if p]

    # ---- POST /api/video/upscale —— 视频超分（后台任务，逐帧进度）----
    if parts == ["api", "video", "upscale"]:
        from api import video_upscale
        try:
            job = video_upscale.start(str(body.get("file") or ""),
                                      int(body.get("scale") or 2))
            _h_json(h, {"ok": True, **job})
        except video_upscale.UpscaleError as e:
            _h_json(h, {"ok": False, "error": str(e)}, 400)
        except Exception as e:
            _h_json(h, {"ok": False, "error": f"启动超分失败：{e}"}, 500)
        return True

    # ---- POST /api/image/reference —— 上传 ControlNet / IP-Adapter 参考图 ----
    if parts == ["api", "image", "reference"]:
        import panel as _panel
        from api import image_gen
        try:
            _fields, files = _panel.parse_multipart(body_bytes, ctype)
        except Exception as e:
            _h_json(h, {"ok": False, "error": f"表单解析失败：{e}"}, 400)
            return True
        item = files.get("file") or (next(iter(files.values())) if files else None)
        if not item:
            _h_json(h, {"ok": False, "error": "没有收到文件（字段名用 file）"}, 400)
            return True
        fname, data = item
        try:
            _h_json(h, {"ok": True, **image_gen.save_reference(fname, data)})
        except image_gen.ImageGenError as e:
            _h_json(h, {"ok": False, "error": str(e)}, 400)
        return True

    # ---- POST /api/image/generate —— SD1.5 文生图（同步，单张约 30~60 秒）----
    if parts == ["api", "image", "generate"]:
        from api import image_gen
        try:
            meta = image_gen.generate(
                prompt=str(body.get("prompt") or ""),
                negative=str(body.get("negative") or ""),
                width=body.get("width", 512),
                height=body.get("height", 512),
                steps=body.get("steps", 20),
                cfg=body.get("cfg", 7.0),
                seed=(body.get("seed") or None),
                sampler=str(body.get("sampler") or "euler"),
                scheduler=str(body.get("scheduler") or "normal"),
                control_path=str(body.get("control_path") or ""),
                control_strength=body.get("control_strength", 0.8),
                control_low=body.get("control_low", 0.4),
                control_high=body.get("control_high", 0.8),
                ip_path=str(body.get("ip_path") or ""),
                ip_weight=body.get("ip_weight", 0.8),
            )
            _h_json(h, {"ok": True, **meta})
        except image_gen.ImageGenError as e:
            _h_json(h, {"ok": False, "error": str(e)}, 400)
        except Exception as e:
            _h_json(h, {"ok": False, "error": f"出图失败：{e}"}, 500)
        return True

    # ---- POST /api/comfy/templates/{name}/download —— 单模板拉到本机 ----
    if len(parts) == 5 and parts[0:3] == ["api", "comfy", "templates"] \
            and parts[4] == "download":
        from api import comfy_templates
        try:
            info = comfy_templates.download(parts[3])
            _h_json(h, {"ok": True, **info})
        except comfy_templates.TemplateError as e:
            _h_json(h, {"ok": False, "error": str(e)}, 400)
        return True

    # ---- POST /api/models —— 添加自定义模型 ----
    if parts == ["api", "models"]:
        name = str(body.get("name", "")).strip()
        mtype = str(body.get("type", "")).strip()
        provider = str(body.get("provider", "")).strip()
        model_name = str(body.get("model_name", "")).strip()
        if not name or not mtype or not model_name:
            _h_json(h, {"error": "name/type/model_name 必填"}, 400)
            return True
        customs = _load_custom_models()
        mid = body.get("id") or f"custom-{int(time.time())}"
        # 防重复：id 存在 → 更新；否则追加
        existing = next((i for i, m in enumerate(customs) if m.get("id") == mid), -1)
        entry = {
            "id": mid, "name": name, "type": mtype,
            "provider": provider or "custom",
            "model_name": model_name, "builtin": False,
            "base_url": body.get("base_url", ""),
            "note": body.get("note", ""),
        }
        if existing >= 0:
            customs[existing] = entry
        else:
            customs.append(entry)
        _save_custom_models(customs)
        _h_json(h, entry)
        return True

    # ---- /api/providers/{provider}/keys ---- （暂存 Key）
    if len(parts) == 4 and parts[0:2] == ["api", "providers"] and parts[3] == "keys":
        provider = parts[2]
        key = str(body.get("api_key", "")).strip()
        if not key:
            _h_json(h, {"error": "api_key 不能空"}, 400)
            return True
        _API_KEYS[provider] = key
        # A2：加密写盘（BEE_MASTER_KEY env var，无则退化成 base64 + Warning）
        try:
            _keys_save(_API_KEYS)
        except Exception as e:
            _h_json(h, {"ok": False, "error": f"Key 写入失败: {str(e)[:200]}"}, 500)
            return True
        _h_json(h, {"ok": True, "provider": provider, "masked": key[:4] + "****" + key[-4:]})
        return True

    # ---- /api/providers/{provider}/test ---- （连通性 ping）
    if len(parts) == 4 and parts[0:2] == ["api", "providers"] and parts[3] == "test":
        provider = parts[2]
        api_key = _get_key(provider)
        latency_ms = 0
        if not api_key:
            _h_json(h, {"ok": False, "error": "未配置 API Key", "latency_ms": 0})
            return True
        # LLM provider → 打一次最小 chat completion 测 key + endpoint
        if provider in ("longcat", "openai"):
            try:
                import time as _t
                from api.llm_client import call_chat
                t0 = _t.time()
                call_chat(provider=provider, api_key=api_key,
                          user="ping", json_mode=False, max_retries=0, timeout=10)
                latency_ms = int((_t.time() - t0) * 1000)
                _h_json(h, {"ok": True, "latency_ms": latency_ms})
            except Exception as exc:
                latency_ms = int(latency_ms or 0)
                _h_json(h, {"ok": False, "error": str(exc)[:200], "latency_ms": latency_ms})
            return True
        # 视频 provider → ping endpoint 域名（DNS+TCP 可达性）
        try:
            import time as _t
            import requests as _r
            _providers_urls = {
                "jimeng": ("api.providers.jimeng", "JimengProvider", "VOLC_ENDPOINT_T2V"),
                "kling":  ("api.providers.kling",  "KlingProvider",  "KLING_ENDPOINT_T2V"),
                "vidu":   ("api.providers.vidu",   "ViduProvider",   "VIDU_ENDPOINT_T2V"),
                "wanx":   ("api.providers.wanx",   "WanxProvider",   "WANX_ENDPOINT_T2V"),
            }
            if provider in _providers_urls:
                import importlib
                mod_name, cls_name, attr = _providers_urls[provider]
                mod = importlib.import_module(mod_name)
                endpoint = getattr(mod, attr, "")
            else:
                endpoint = ""
            if not endpoint or not endpoint.startswith("http"):
                _h_json(h, {"ok": True, "latency_ms": 0, "note": "Key 已配，endpoint 占位未填，跳过网络测试"})
                return True
            from urllib.parse import urlparse as _up
            host = f"{_up(endpoint).scheme}://{_up(endpoint).netloc}"
            t0 = _t.time()
            resp = _r.get(host, timeout=5)
            latency_ms = int((_t.time() - t0) * 1000)
            _h_json(h, {"ok": True, "latency_ms": latency_ms, "http_status": resp.status_code})
        except Exception as exc:
            latency_ms = int(latency_ms or 0)
            _h_json(h, {"ok": False, "error": str(exc)[:200], "latency_ms": latency_ms})
        return True

    # ---- /api/video/generate ----
    if parts == ["api", "video", "generate"]:
        provider = body.get("provider", "jimeng")
        resolution = body.get("resolution", "720p")
        try:
            duration = float(body.get("duration", 5))
        except (TypeError, ValueError):
            _h_json(h, {"error": "duration 必须是数字（秒）"}, 400)
            return True
        prompt = str(body.get("prompt", ""))
        image_url = str(body.get("image_url", ""))
        role_id = str(body.get("role_id") or "").strip()
        image_path = str(body.get("image_path") or "").strip()
        engine = str(body.get("engine") or "").strip().lower()
        from api.providers import GenRequest, ProviderError
        # 本地免费通道的硬上限：Wan2.2 5B 121 帧 @24fps ≈ 5.04 秒，SVD 25 帧，
        # 请求更长只会被静默截断（用户以为要了 20 秒，拿到的还是 5 秒），
        # 所以在这里直接拒绝，而不是骗过去。
        if not (0 < duration < 1e6):
            _h_json(h, {"error": "duration 必须是大于 0 的有限秒数"}, 400)
            return True
        if provider != "comfyui" and engine:
            _h_json(h, {"error": f"出片方案「{engine}」只用于本地免费通道（provider=comfyui）"}, 400)
            return True
        if provider == "comfyui":
            if duration > 5.0:
                _h_json(h, {
                    "error": f"本地免费通道时长必须在 0–5 秒之间（这次请求 {duration:g} 秒）。"
                             "要更长的片子，请用云端 provider，或把这段拆成多个镜头。"
                }, 400)
                return True
            if engine not in ("", "svd"):
                _h_json(h, {"error": f"未知的出片方案「{engine}」，目前只有默认（Wan2.2 5B）和 svd"}, 400)
                return True
            # SVD 只有 25 帧、播放帧率上限 24fps，最短只能 honest 地出 25/24≈1.04 秒；
            # 更短的请求若放行会被静默拉成 1 秒以上（时长口径货不对板），所以直接拒绝。
            if engine == "svd" and duration < 1.0:
                _h_json(h, {"error": f"SVD 快出档最短 1 秒（25 帧 / 最高 24fps，这次请求 {duration:g} 秒）。"
                                     "要更短的片段请用默认 5B 档。"}, 400)
                return True
            # image_url 直传 file: 也是合法首帧来源（批量与老调用方用它），别误拒
            if engine == "svd" and not (image_path or role_id or image_url.startswith("file:")):
                _h_json(h, {"error": "SVD 快出档是图生视频，需要先选一张首帧图"}, 400)
                return True
        # 图片工坊出的图直接当首帧：本地 5B 只认本机文件，这里把 data 相对路径
        # 换成 file: 绝对路径，前端不用知道磁盘布局。
        if image_path:
            if role_id:
                _h_json(h, {"error": "role_id 与 image_path 只能二选一"}, 400)
                return True
            if provider != "comfyui":
                _h_json(h, {"error": "图片首帧目前只支持本地 ComfyUI（免费通道）"}, 400)
                return True
            src = _data_media_path(image_path)
            if not src:
                _h_json(h, {"error": "首帧图片不存在或路径非法（只接受 data 目录内文件）"}, 404)
                return True
            if src.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                _h_json(h, {"error": "首帧只接受 PNG / JPG / WEBP"}, 400)
                return True
            image_url = "file:" + str(src)
        # 角色参考图：传 role_id 即可，服务端从 roles.json 解析，
        # 前端不必自己拼 URL，也避免绕开授权校验。
        if role_id:
            role = _role_by_id(role_id)
            if not role:
                _h_json(h, {"error": "角色不存在，请刷新后重试"}, 404)
                return True
            try:
                _require_ref_consent(role)
                # 本地走 file:（ComfyUI 读本机文件），云端走公网 URL
                if provider == "comfyui":
                    image_url = _ref_image_local(role)
                else:
                    image_url = _ref_image_for_cloud(role)
            except ProviderError as e:
                _h_json(h, {"error": str(e)}, 400)
                return True

        # 1. Key 检查（comfyui 是本机服务，不需要 Key）
        api_key = "" if provider == "comfyui" else _get_key(provider)
        if provider != "comfyui" and not api_key:
            _h_json(h, {"error": f"{provider} 未配置 API Key，请先在「设置 → API Keys」里填入"}, 402)
            return True

        # 2. 加载 provider 类
        try:
            prov = _make_provider(provider)
        except Exception as e:
            _h_json(h, {"error": f"provider 加载失败: {e}"}, 500)
            return True

        # 3. 发起任务
        try:
            result = prov.generate(GenRequest(
                prompt=prompt, image_url=image_url,
                duration=duration, resolution=resolution,
                engine=engine,
            ))
        except ProviderError as e:
            _h_json(h, {"error": str(e)}, 400)
            return True
        except Exception as e:
            _h_json(h, {"error": f"生成失败: {e}"}, 500)
            return True

        # 4. 存进 SQLite + 起后台线程轮询
        job_id = result.job_id or _new_job_id()
        initial_job = {
            "job_id": job_id,
            "provider": provider,
            "provider_label": prov.label,
            "status": result.status or "queued",
            "video_url": "",
            "actual_cost": 0.0,
            "error": "",
            "created_at": datetime.datetime.now().isoformat(),
            "poll_count": 0,
            "prompt": prompt[:200],
            "resolution": resolution,
            "duration": duration,
            "staged_image": getattr(result, "_bee_staged_image", ""),
        }
        _video_job_write(initial_job)
        # 后台线程：阻塞轮询。本地 ComfyUI 单卡跑 5 秒片子要几分钟，给 30 分钟；
        # 云端通常 1~5 分钟，维持原来的 300s。
        _poll_budget = 1800 if provider == "comfyui" else 300
        threading.Thread(target=_poll_video_job,
                         args=(prov, job_id, result, _poll_budget), daemon=True).start()
        # 立刻返回
        _h_json(h, {"ok": True, "job_id": job_id, "status": initial_job["status"]})
        return True

    # ---- POST /api/export/{project_id} —— 切片 D（异步后台 ffmpeg） ----
    if len(parts) == 3 and parts[0:2] == ["api", "export"]:
        project_id = parts[2]
        shots_raw = body.get("shots", [])
        from video_pipeline.ffmpeg_export import ShotSegment, compose as _ffmpeg_compose
        shots = []
        for s in shots_raw:
            shots.append(ShotSegment(
                shot_id=str(s.get("shot_id") or s.get("id") or ""),
                video_path=_media_local_path(str(s.get("video_path") or s.get("videoUrl") or "")),
                start_sec=float(s.get("start_sec", 0.0)),
                end_sec=float(s.get("end_sec") or s.get("duration", 5.0)),
                voice_path=_media_local_path(str(s.get("voice_path") or s.get("audioUrl") or "")),
                subtitle=str(s.get("subtitle") or s.get("prompt") or ""),
                scene_no=int(s.get("scene_no") or 1),
            ))
        if not shots:
            _h_json(h, {"error": "shots 列表不能为空"}, 400)
            return True
        ejid = _new_export_job_id()
        out_dir = _ROOT / "outputs" / "bee-studio" / project_id
        now_iso = datetime.datetime.now().isoformat()
        _export_job_write({
            "job_id": ejid,
            "status": "pending",
            "project_id": project_id,
            "output_path": "",
            "output_duration": 0,
            "error": "",
            "created_at": now_iso,
            "updated_at": now_iso,
            "poll_count": 0,
        })

        def _bg_compose():
            try:
                j = _export_job_read(ejid) or {}
                j["status"] = "running"
                j["poll_count"] = 0
                _export_job_write(j)
                result = _ffmpeg_compose(shots, out_dir)
                j = _export_job_read(ejid) or {}
                if result.get("error"):
                    j["status"] = "failed"
                    j["error"] = result["error"]
                else:
                    # 表里只有 output_path / output_duration / zip_path 三列，
                    # 直接 j.update(result) 会把 mp4_path 这类键丢掉，前端拿不到产物路径。
                    j["status"] = "succeeded"
                    j["output_path"] = result.get("mp4_path", "")
                    j["output_duration"] = result.get("duration_total", 0)
                    j["zip_path"] = result.get("zip_path", "")
                _export_job_write(j)
            except Exception as exc:
                j = _export_job_read(ejid) or {}
                j["status"] = "failed"
                j["error"] = str(exc)
                _export_job_write(j)

        threading.Thread(target=_bg_compose, daemon=True).start()
        _h_json(h, {"job_id": ejid, "status": "pending", "message": "ffmpeg 合成已发起"}, 202)
        return True

    # ---- POST /api/llm/script-gen —— AI 生成剧本（LongCat 等 LLM 后端） ----
    if parts == ["api", "llm", "script-gen"]:
        topic = str(body.get("topic", "")).strip()
        shots_count = int(body.get("shots_count", 5))
        audience = str(body.get("audience", "")).strip()
        style = str(body.get("style", "")).strip()
        provider = str(body.get("provider", "longcat")).strip()
        force_mock = bool(body.get("force_mock"))   # G2-2: 跳过 LLM 直接 Mock 并写 DB
        api_key = str(body.get("api_key", "")).strip() or _get_key(provider)

        # G2-2: 提前定义，force_mock 块依赖它们（Python 闭包要求先定义后引用）
        def _norm_emotion(raw_emo) -> str:
            """把 LLM 的自由情绪词收敛到 TTS 引擎认的 11 个枚举之一。

            LLM 习惯写「悬疑、紧绷、未知」这类复合词，后端 EMOTIONS 只认单值，
            原样传下去会被静默丢弃、全部变成中性（用户看到的就是「情绪对不上」）。
            """
            from tts_pipeline.script import EMOTIONS
            s = str(raw_emo or "").strip()
            if s in EMOTIONS:
                return s
            for ch in EMOTIONS:
                if ch != "中性" and ch in s:
                    return ch
            alias = {
                "忧郁": "悲伤", "失落": "悲伤", "难过": "悲伤", "压抑": "悲伤", "沉重": "悲伤",
                "喜悦": "开心", "愉快": "开心", "欢快": "开心",
                "兴奋": "激动", "热血": "激动", "爆发": "激动",
                "惊慌": "恐惧", "惊恐": "恐惧",
                "不安": "紧张", "焦躁": "紧张", "急促": "紧张", "紧绷": "紧张",
                "悬疑": "迟疑", "疑惑": "迟疑", "犹豫": "迟疑", "沉思": "迟疑", "未知": "迟疑",
                "嘲讽": "讽刺", "幽默": "讽刺",
                "疲倦": "疲惫", "虚弱": "疲惫", "释然": "温柔", "温暖": "温柔", "亲昵": "温柔",
            }
            for k, v in alias.items():
                if k in s:
                    return v
            return "中性"

        def _normalize_shot(s: dict | None) -> dict:
            """兜底填空值，保证每个 shot 都有必需字段。"""
            s = s or {}
            return {
                "scene_no": int(s.get("scene_no") or s.get("shot_num") or 1),
                "title": str(s.get("title") or f"镜头{s.get('scene_no')}"),
                "char": str(s.get("char") or s.get("character") or ""),
                "emotion": _norm_emotion(s.get("emotion")),
                "duration_sec": int(s.get("duration_sec") or s.get("duration") or 5),
                "prompt": str(s.get("prompt") or s.get("description") or ""),
                "voice_prompt": str(s.get("voice_prompt") or s.get("voice_text") or ""),
            }

        def _build_mock_script() -> dict:
            """A5 兜底 mock（LLM 挂了也能出个可编辑剧本）。"""
            mock_shots = []
            for i in range(max(shots_count, 1)):
                mock_shots.append({
                    "scene_no": i + 1,
                    "title": f"镜头 {i + 1}",
                    "char": "主角",
                    "emotion": ["平静", "紧张", "爆发", "沉思", "释然"][i % 5],
                    "duration_sec": 5,
                    "prompt": f"（mock）{topic} 场景 {i + 1}",
                    "voice_prompt": f"（mock）旁白，第 {i + 1} 句台词",
                })
            return {"title": topic or "mock 剧本", "shots": mock_shots}

        if not topic:
            _h_json(h, {"error": "topic 必填"}, 400)
            return True
        if shots_count < 1 or shots_count > 30:
            _h_json(h, {"error": "shots_count 应在 1~30 之间"}, 400)
            return True

        if force_mock:
            # G2-2: 前端要求 Mock 模式（无 LLM Key 也要持久化剧本）
            mock = _build_mock_script()
            pid = body.get("project_id") or f"proj-{int(time.time())}"
            _script_write(pid, mock["title"], "",
                          json.dumps({"shots": mock["shots"]}, ensure_ascii=False),
                          topic, "mock")
            _h_json(h, {
                "project_id": pid, "title": mock["title"], "shots": mock["shots"],
                "generated_by": "mock", "shots_count": len(mock["shots"]),
                "used_mock": True, "mock_reason": "force_mock",
            })
            return True

        if not api_key:
            _h_json(h, {"error": f"{provider} 未配置 API Key", "need_key": True}, 402)
            return True

        # 构造 system + user prompt，要求输出结构化 JSON
        system_prompt = (
            "你是资深短视频导演兼编剧。根据用户给的主题生成一段分镜剧本（JSON 格式）。\n"
            "严格遵守以下约束：\n"
            "1. 每个镜头包含：scene_no（序号）、title（标题）、char（角色，用昵称）、emotion（情绪）、duration_sec（整数秒）、prompt（详细的视频生成提示词）、voice_prompt（配音要求）。\n"
            "2. prompt 要能直接喂给即梦/Kling/Vidu 等视频模型，详细描述画面、运动、光线、情绪。\n"
            "3. 总时长约等于所有 duration_sec 之和（每个镜头建议 5~15s）。\n"
            "4. 角色名保持一致。emotion 必须从这 11 个词里选、且只能选一个："
            "中性/温柔/开心/激动/悲伤/愤怒/紧张/迟疑/恐惧/讽刺/疲惫（这是 TTS 情绪引擎的枚举，写别的词会被丢弃）；"
            "整体情绪要有起伏（紧张→爆发→收束）。\n"
            "5. 输出必须是合法 JSON，外层结构为 {\"title\": \"剧本标题\", \"shots\": [...]}"
        )
        user_prompt = (
            f"主题：{topic}\n"
            f"镜头数：{shots_count}\n"
            f"目标受众：{audience or '通用'}\n"
            f"风格/色调：{style or '电影感，冷暖对比'}\n\n"
            "请输出完整 JSON。"
        )

        # A5：schema validation + retry （_normalize_shot / _build_mock_script 已提前到 force_mock 之前定义）

        try:
            from api.llm_client import call_chat
            raw = call_chat(provider=provider, api_key=api_key,
                            system=system_prompt, user=user_prompt,
                            json_mode=True, max_retries=1, timeout=90)
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```", 2)[1].split("```")[0].strip()
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                # A5：retry 一次，prompt 更硬
                retry_prompt = (user_prompt + "\n\n⚠️ 上一次输出 JSON 解析失败。"
                                "这一次请确保：只输出裸 JSON，不要任何文字解释，不要 Markdown 代码块，"
                                "不要在开头加「好的，这是剧本：」。直接输出 {\"title\":...,\"shots\":[...]}")
                raw2 = call_chat(provider=provider, api_key=api_key,
                                 system=system_prompt, user=retry_prompt,
                                 json_mode=True, max_retries=0, timeout=90)
                cleaned2 = raw2.strip()
                if cleaned2.startswith("```"):
                    cleaned2 = cleaned2.split("```", 2)[1].split("```")[0].strip()
                data = json.loads(cleaned2)

            # A5：schema validation —— 兜底填空值
            shots_raw = data.get("shots") or []
            if not isinstance(shots_raw, list) or not shots_raw:
                raise ValueError("LLM 返回 shots 为空或非数组")
            shots = [_normalize_shot(s) for s in shots_raw[:30]]  # 上限 30
            title = str(data.get("title") or topic or "未命名剧本")

            # A3：剧本历史持久化（存 SQLite scripts 表）
            pid = body.get("project_id") or f"proj-{int(time.time())}"
            _script_write(pid, title, str(data.get("genre") or ""),
                          json.dumps({"shots": shots}, ensure_ascii=False),
                          topic, provider)

            _h_json(h, {
                "project_id": pid, "title": title, "shots": shots,
                "generated_by": provider, "shots_count": len(shots),
                "used_mock": False,
            })
        except json.JSONDecodeError as exc:
            # A5：双重失败 → mock 降级（不是真 502，是可用 fallback）
            mock = _build_mock_script()
            pid = body.get("project_id") or f"proj-{int(time.time())}"
            _script_write(pid, mock["title"], "",
                          json.dumps({"shots": mock["shots"]}, ensure_ascii=False),
                          topic, "mock")
            _h_json(h, {
                "project_id": pid, "title": mock["title"], "shots": mock["shots"],
                "generated_by": "mock", "shots_count": len(mock["shots"]),
                "used_mock": True, "mock_reason": f"JSON 解析失败: {exc.msg}",
                "raw_preview": (raw or "")[:500],
            }, 200)  # 200 OK mock 也算可用，不报 502
        except Exception as exc:
            # LLM 调用挂了 → mock 降级
            try:
                mock = _build_mock_script()
                pid = body.get("project_id") or f"proj-{int(time.time())}"
                _script_write(pid, mock["title"], "",
                              json.dumps({"shots": mock["shots"]}, ensure_ascii=False),
                              topic, "mock")
                _h_json(h, {
                    "project_id": pid, "title": mock["title"], "shots": mock["shots"],
                    "generated_by": "mock", "shots_count": len(mock["shots"]),
                    "used_mock": True, "mock_reason": f"LLM 调用失败: {str(exc)[:200]}",
                }, 200)
            except Exception as exc2:
                _h_json(h, {"error": f"LLM 失败 + mock 也挂了: {str(exc2)[:200]}"}, 502)
        return True

    # ---- POST /api/bee/script/{project_id}/shots —— G4-P1: 保存 shots（自动保存） ----
    if len(parts) == 5 and parts[0:3] == ["api", "bee", "script"] and parts[4] == "shots":
        pid = parts[3]
        shots = body.get("shots")
        if not shots or not isinstance(shots, list):
            _h_json(h, {"error": "shots 必须是数组"}, 400); return True
        _db_init()
        try:
            conn = sqlite3.connect(_DB_PATH)
            cur = conn.execute("SELECT title FROM scripts WHERE project_id=?", (pid,))
            row = cur.fetchone()
            if not row:
                _h_json(h, {"error": "project 不存在"}, 404); conn.close(); return True
            conn.execute("UPDATE scripts SET shots_json=?, updated_at=? WHERE project_id=?",
                         (json.dumps({"shots": shots}, ensure_ascii=False),
                          datetime.datetime.now().isoformat(), pid))
            conn.commit(); conn.close()
            _h_json(h, {"ok": True, "saved_shots": len(shots)})
        except Exception as exc:
            _h_json(h, {"error": str(exc)[:120]}, 500)
        return True

    # ---- POST /api/llm/script-edit —— C1：剧本改镜头（只改指定镜头，不重生成全剧） ----
    if parts == ["api", "llm", "script-edit"]:
        project_id = str(body.get("project_id") or "").strip()
        shot_index = body.get("shot_index")
        edit_instruction = str(body.get("edit_instruction") or "").strip()
        provider = str(body.get("provider") or "longcat").strip()
        api_key = str(body.get("api_key") or "").strip() or _get_key(provider)

        if not project_id or shot_index is None or not edit_instruction:
            _h_json(h, {"error": "project_id, shot_index, edit_instruction 必填"}, 400)
            return True

        sc = _script_read(project_id)
        if not sc:
            _h_json(h, {"error": f"script {project_id} 不存在，请先 script-gen"}, 404)
            return True

        shots = sc.get("shots") or []
        if shot_index < 0 or shot_index >= len(shots):
            _h_json(h, {"error": f"shot_index {shot_index} 越界（0~{len(shots)-1}）"}, 400)
            return True

        if not api_key:
            _h_json(h, {"error": f"{provider} 未配置 API Key（改镜头需要真实 LLM）",
                         "need_key": True}, 402)
            return True

        system_prompt = (
            "你是短视频分镜编剧。只修改用户指定的那一个镜头，其它镜头不动。\n"
            "输出 JSON：{\"updated_shot\": {...}} 。镜头字段见原剧本。"
        )
        old_shot = shots[shot_index]
        user_prompt = (
            f"剧本主题：{sc.get('title', '')}\n"
            f"要修改的镜头 #{shot_index}（当前值）：\n"
            f"{json.dumps(old_shot, ensure_ascii=False, indent=2)}\n\n"
            f"修改指令：{edit_instruction}\n\n"
            "请只输出 updated_shot JSON，严格遵循原镜头的字段结构。"
        )

        try:
            from api.llm_client import call_chat
            raw = call_chat(provider=provider, api_key=api_key,
                            system=system_prompt, user=user_prompt,
                            json_mode=True, max_retries=1, timeout=60)
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```", 2)[1].split("```")[0].strip()
            data = json.loads(cleaned)
            updated = data.get("updated_shot") or data

            def _normalize_edit(s: dict) -> dict:
                return {
                    "scene_no": int(s.get("scene_no") or shot_index + 1),
                    "title": str(s.get("title") or old_shot.get("title") or ""),
                    "char": str(s.get("char") or old_shot.get("char") or ""),
                    "emotion": str(s.get("emotion") or old_shot.get("emotion") or ""),
                    "duration_sec": int(s.get("duration_sec") or old_shot.get("duration_sec") or 5),
                    "prompt": str(s.get("prompt") or old_shot.get("prompt") or ""),
                    "voice_prompt": str(s.get("voice_prompt") or old_shot.get("voice_prompt") or ""),
                }

            shots[shot_index] = _normalize_edit(updated)
            _script_write(project_id, sc.get("title", ""), str(sc.get("genre") or ""),
                          json.dumps({"shots": shots}, ensure_ascii=False),
                          sc.get("prompt", ""), provider)
            _h_json(h, {
                "project_id": project_id, "updated_shot_index": shot_index,
                "updated_shot": shots[shot_index],
                "shots": shots, "edited_by": provider,
            })
        except Exception as exc:
            _h_json(h, {"error": f"改镜头 LLM 失败: {str(exc)[:200]}",
                         "raw_preview": (raw or "")[:500] if 'raw' in dir() else ""}, 502)
        return True

    # ---- POST /api/video/generate-batch —— A4：多镜头并行视频生成 ----
    if parts == ["api", "video", "generate-batch"]:
        provider = str(body.get("provider", "jimeng"))
        shot_list = body.get("shots", [])
        max_workers = int(body.get("max_workers", 3))
        if not isinstance(shot_list, list) or not shot_list:
            _h_json(h, {"error": "shots 列表不能为空"}, 400)
            return True
        if provider != "comfyui" and not _get_key(provider):
            _h_json(h, {"error": f"{provider} 未配置 API Key", "need_key": True}, 402)
            return True
        # 批量端点也必须守同一条时长红线，否则 10 秒镜头会被 5B 静默截成 5 秒
        if provider == "comfyui":
            for i, shot in enumerate(shot_list):
                # 必须显式取键：`x or 默认` 会把 0 / "" 当没传，静默改成 5 秒，
                # 这正是单端点明令禁止的货不对板（单端点 0 会 400）。
                raw = (shot or {}).get("duration")
                if raw is None:
                    raw = (shot or {}).get("duration_sec")
                if raw is None:
                    raw = 5
                try:
                    d = float(raw)
                except (TypeError, ValueError):
                    _h_json(h, {"error": f"第 {i + 1} 个镜头的 duration 不是数字"}, 400)
                    return True
                if not (0 < d <= 5.0):
                    _h_json(h, {"error": f"本地免费通道单个镜头时长必须在 0–5 秒之间"
                                        f"（第 {i + 1} 个镜头要了 {d:g} 秒）"}, 400)
                    return True

        from concurrent.futures import ThreadPoolExecutor, as_completed
        from api.providers import GenRequest, ProviderError

        # 原先这里 import 了不存在的 load_provider，批量出片一调就 ImportError。
        # 统一走 _make_provider，comfyui 无 Key 也能建。
        prov = _make_provider(provider)
        batch_results = []  # [{shot_index, job_id, status, error}]

        def _one(idx: int, shot: dict) -> dict:
            try:
                img = str(shot.get("image_url") or "")
                rid = str(shot.get("role_id") or "").strip()
                if rid:
                    role = _role_by_id(rid)
                    if not role:
                        return {"shot_index": idx, "status": "failed", "error": "角色不存在"}
                    _require_ref_consent(role)
                    img = (_ref_image_local(role) if provider == "comfyui"
                           else _ref_image_for_cloud(role))
                raw_d = shot.get("duration")
                if raw_d is None:
                    raw_d = shot.get("duration_sec")
                if raw_d is None:
                    raw_d = 5
                req = GenRequest(
                    prompt=str(shot.get("prompt") or ""),
                    image_url=img,
                    duration=float(raw_d),
                    resolution=str(shot.get("resolution") or "720p"),
                )
                result = prov.generate(req)
                jid = result.job_id or f"vb_{int(time.time())}_{idx}"
                initial = {
                    "job_id": jid, "provider": provider, "provider_label": prov.label,
                    "status": result.status or "queued",
                    "video_url": "", "actual_cost": 0.0, "error": "",
                    "created_at": datetime.datetime.now().isoformat(),
                    "poll_count": 0, "prompt": req.prompt[:200],
                    "resolution": req.resolution, "duration": req.duration,
                    "staged_image": getattr(result, "_bee_staged_image", ""),
                    "shot_index": idx,
                }
                _video_job_write(initial)
                # 复用单镜头轮询：本地给 30 分钟，云端 300 秒
                budget = 1800 if provider == "comfyui" else 300
                threading.Thread(target=_poll_video_job,
                                 args=(prov, jid, result, budget), daemon=True).start()
                return {"shot_index": idx, "job_id": jid, "status": "queued"}
            except ProviderError as e:
                return {"shot_index": idx, "status": "failed", "error": str(e)}
            except Exception as e:
                return {"shot_index": idx, "status": "failed", "error": str(e)[:200]}

        with ThreadPoolExecutor(max_workers=min(max_workers, len(shot_list))) as pool:
            futures = {pool.submit(_one, i, s): i for i, s in enumerate(shot_list)}
            for fut in as_completed(futures):
                batch_results.append(fut.result())

        # 按 shot_index 排序
        batch_results.sort(key=lambda x: x["shot_index"])
        _h_json(h, {
            "ok": True, "provider": provider,
            "batch_size": len(shot_list), "max_workers": min(max_workers, len(shot_list)),
            "results": batch_results,
        }, 202)
        return True

    return False


# ------------------------------------------------------------------
# 让 panel.py 只需调一个入口
# ------------------------------------------------------------------
def handle(h: BaseHTTPRequestHandler, method: str, path: str,
           body: bytes = b"", ctype: str = "") -> bool:
    if method == "GET":
        return handle_get(h, path)
    if method == "POST":
        return handle_post(h, path, body_bytes=body, ctype=ctype)
    if method == "DELETE":
        return handle_delete(h, path)
    return False


# ------------------------------------------------------------------
# DELETE 路由
# ------------------------------------------------------------------
def handle_delete(h: BaseHTTPRequestHandler, path: str) -> bool:
    parts = [p for p in path.split("/") if p]

    # ---- DELETE /api/images/{name} —— 删掉一张出图（含侧车元数据）----
    if len(parts) == 3 and parts[0:2] == ["api", "images"]:
        from api import image_gen
        if not image_gen.delete_image(parts[2]):
            _h_json(h, {"ok": False, "error": "图片不存在"}, 404)
            return True
        _h_json(h, {"ok": True})
        return True

    # ---- DELETE /api/models/{id} ----
    if len(parts) == 3 and parts[0:2] == ["api", "models"]:
        mid = parts[2]
        customs = _load_custom_models()
        customs = [m for m in customs if m.get("id") != mid]
        _save_custom_models(customs)
        _h_json(h, {"deleted": mid})
        return True

    return False
