"""环境声 / BGM 素材与混音（纯工程，不涉及任何模型）。

这一层只做四件事，全部是可单测的纯函数：

1. **解码**：把用户上传的 wav/flac/ogg/mp3 统一成 24kHz 单声道 s16le PCM。
   素材多是 44.1kHz stereo，直接按 24k 解释会拉慢约 1.84 倍且降调，
   必须先下混再重采样。
2. **生成**：程序合成噪声底（白噪 / 棕噪 / 风 / 低频嗡鸣 / 雨 / 火 / 街道），
   **不内置任何第三方音乐**，避免版权问题。
3. **铺底**：按场次时间范围把素材循环铺满，首尾做淡入淡出。
4. **混音**：干声 + 铺底 → 按时间轴 ducking → 峰值限幅 → 写 WAV。

两条硬规矩：
- 混音**不再做响度归一化**。每行干声落盘时已单独归一化，
  再对整体归一化会把铺底一起拉响、破坏 ducking 的比例关系。
- ducking 只能按**时间轴**做。每行已单独归一化，行间动态被抹平，
  瞬时能量侧链不可靠，位置法才可预期。
"""
from __future__ import annotations

import io
import math
import struct

SAMPLE_RATE = 24000
BYTES_PER_SAMPLE = 2

# 程序生成的噪声底种类：键是接口里的 kind，值是可读的说明
NOISE_KINDS: dict[str, str] = {
    "white": "白噪声（干净底噪，适合压住环境杂音）",
    "brown": "棕噪声（偏低沉，适合室内、街道底色）",
    "wind": "风声（缓慢起伏，适合户外、空旷场景）",
    "hum": "低频嗡鸣（空调/电器底噪，适合室内场景）",
    "rain": "雨声（密集高频+雨滴点击，适合雨夜、离别等潮湿场景）",
    "fire": "火声（低频火苗底+不规则噼啪，适合篝火、室内壁炉场景）",
    "street": "街道（车流嗡鸣+缓慢起伏+远处呼啸，适合城市外景、过场）",
}

# 新增的场景底音：峰值定在 0.70 满量程（约 -3.1 dBFS），留 1~2 dB 余量
_AMBIENCE_PEAK = 0.70
# 这些 kind 的随机源偏移写死，避免依赖 str.hash 的进程随机化
_AMBIENCE_OFFSETS = {"rain": 1103, "fire": 2207, "street": 3313}


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------
def _decode_with_soundfile(data: bytes) -> tuple[bytes, int]:
    """有 soundfile 就用它（支持 wav/flac/ogg/mp3），返回 (mono pcm, 采样率)。"""
    import numpy as np
    import soundfile as sf

    with sf.SoundFile(io.BytesIO(data)) as f:
        rate = f.samplerate
        frames = f.read(dtype="int16", always_2d=True)
    if frames.size == 0:
        return b"", rate
    mono = frames.mean(axis=1) if frames.shape[1] > 1 else frames[:, 0]
    return mono.astype("<i2").tobytes(), rate


def _decode_wav_stdlib(data: bytes) -> tuple[bytes, int]:
    """stdlib 兜底：只支持 PCM WAV（8/16/24/32 位），够用且不引依赖。"""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是可识别的 WAV 文件")
    pos = 12
    fmt = None
    pcm = b""
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)
    if not fmt:
        raise ValueError("WAV 缺少 fmt 块")
    audio_fmt, channels, rate, _, _, bits = fmt
    if audio_fmt not in (1, 0xFFFE):
        raise ValueError(f"只支持 PCM 的 WAV（格式码 {audio_fmt}）")
    if bits == 16:
        usable = len(pcm) - (len(pcm) % 2)
        out = pcm[:usable]
    elif bits == 8:
        out = bytes((b - 128) & 0xFF for b in pcm)
    elif bits == 24:
        usable = len(pcm) - (len(pcm) % 3)
        out = b"".join(
            struct.pack("<h", int.from_bytes(pcm[i:i + 3], "little", signed=True) >> 8)
            for i in range(0, usable, 3)
        )
    elif bits == 32:
        usable = len(pcm) - (len(pcm) % 4)
        out = b"".join(
            struct.pack("<h", int.from_bytes(pcm[i:i + 4], "little", signed=True) >> 16)
            for i in range(0, usable, 4)
        )
    else:
        raise ValueError(f"不支持 {bits} 位 WAV")
    if channels > 1:
        usable = len(out) - (len(out) % (2 * channels))
        step = 2 * channels
        out = b"".join(
            out[i:i + 2]
            for i in range(0, usable, step)
        )
    return out, rate


def decode_to_pcm24k(data: bytes) -> bytes:
    """任意常见音频 → 24kHz 单声道 s16le PCM。"""
    pcm, rate = b"", SAMPLE_RATE
    try:
        pcm, rate = _decode_with_soundfile(data)
    except Exception:
        pcm, rate = _decode_wav_stdlib(data)
    if not pcm:
        return b""
    if rate == SAMPLE_RATE:
        return pcm
    import numpy as np

    src = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if src.size < 2:
        return b""
    dst_len = max(1, int(round(src.size * SAMPLE_RATE / float(rate))))
    x_old = np.arange(src.size, dtype=np.float64)
    x_new = np.linspace(0, src.size - 1, dst_len, dtype=np.float64)
    resampled = np.interp(x_new, x_old, src)
    return np.clip(resampled, -32768, 32767).astype("<i2").tobytes()


# ---------------------------------------------------------------------------
# 生成噪声底
# ---------------------------------------------------------------------------
def _band_noise(rng, n: int, hp: float | None = None,
                lp: float | None = None):
    """频域整形的高斯噪声（二阶高通/低通相加性掩模）。

    二阶斜率很缓，不会像砖墙 FIR 那样在时域留下「金属味」的前回响；
    整段 rfft 一次算完，30 秒也只要几十毫秒。
    """
    import numpy as np

    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    gain = np.ones_like(f)
    if hp:
        gain = gain * (f * f) / (f * f + hp * hp)
    if lp:
        gain = gain * (lp * lp) / (f * f + lp * lp)
    return np.fft.irfft(spec * gain, n).astype(np.float32)


def _bursts(rng, n: int, count: int, length: float, f_lo: float, f_hi: float,
            tau_lo: float, tau_hi: float, noisy: bool = False,
            clustered: bool = False):
    """在随机位置叠加 `count` 个指数衰减短事件，返回整段加性层。

    - 正弦衰减（noisy=False）：雨滴那种「叮」的点击感
    - 噪声衰减（noisy=True）：柴火那种宽频「啪」
    - clustered=True：事件间隔取指数分布，成簇出现，听感才不机械

    全部用 `np.bincount` 一次性散点累加，不做逐采样 Python 循环。
    """
    import numpy as np

    layer = np.zeros(n, dtype=np.float32)
    if count <= 0 or n <= 2:
        return layer
    span = max(2, int(length * SAMPLE_RATE))
    span = min(span, n)
    hi = n - span
    if clustered:
        mean_gap = max(1.0, hi / max(1.0, count * 1.6))
        gaps = rng.exponential(mean_gap, count)
        starts = np.clip(np.cumsum(gaps).astype(np.int64), 0, max(0, hi))
    else:
        starts = rng.integers(0, max(1, hi), count) if hi > 0 else np.zeros(
            count, dtype=np.int64)

    t = np.arange(span, dtype=np.float32) / np.float32(SAMPLE_RATE)
    tau = rng.uniform(tau_lo, tau_hi, count).astype(np.float32)
    env = np.exp(-t[None, :] / tau[:, None]).astype(np.float32)
    if noisy:
        sig = rng.standard_normal((count, span)).astype(np.float32)
        sig -= sig.mean(axis=1, keepdims=True)
    else:
        freq = rng.uniform(f_lo, f_hi, count).astype(np.float32)
        phase = rng.uniform(0.0, 2.0 * np.pi, count).astype(np.float32)
        sig = np.sin(2.0 * np.pi * freq[:, None] * t[None, :]
                     + phase[:, None]).astype(np.float32)
    sig *= env * rng.uniform(0.35, 1.0, count).astype(np.float32)[:, None]

    idx = (starts[:, None] + np.arange(span, dtype=np.int64)[None, :]).ravel()
    acc = np.bincount(idx, weights=sig.ravel(), minlength=n)[:n]
    return acc.astype(np.float32)


def _norm(x):
    """按峰值归一到 [-1, 1]（全零时原样返回，避免除零）。"""
    import numpy as np

    m = float(np.max(np.abs(x))) if x.size else 0.0
    return x / (m or 1.0)


def _synth_rain(n: int, rng):
    """雨：1.2k–9.5k 密集高频底 + 每秒约 55 个短促雨滴点击 + 雨势慢起伏。"""
    import numpy as np

    body = _norm(_band_noise(rng, n, hp=1200.0, lp=9500.0))
    drops = _norm(_bursts(rng, n, int(round(n / SAMPLE_RATE * 55)), 0.014,
                          2200.0, 7800.0, 0.0015, 0.006))
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    # 雨势忽大忽小：0.05Hz 与 0.13Hz 两个慢 LFO 叠加
    lfo = 0.74 + 0.16 * np.sin(2 * np.pi * 0.05 * t + 0.9) \
        + 0.10 * np.sin(2 * np.pi * 0.13 * t + 2.4)
    return (0.45 * body + 0.55 * drops) * lfo.astype(np.float32)


def _synth_fire(n: int, rng):
    """火：25–320Hz 火苗呼呼底 + 每秒约 9 个成簇的宽频噼啪（一阶差分提高频）。"""
    import numpy as np

    rumble = _norm(_band_noise(rng, n, hp=25.0, lp=320.0))
    pops = _bursts(rng, n, int(round(n / SAMPLE_RATE * 9)), 0.030,
                   0.0, 0.0, 0.0010, 0.0040, noisy=True, clustered=True)
    # 差分掉低频 → 只剩「噼」的瞬态，像干柴爆裂
    pops = _norm(np.diff(pops, prepend=np.float32(0.0)))
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    flicker = 0.58 + 0.24 * np.sin(2 * np.pi * 0.09 * t + 1.7) \
        + 0.18 * np.sin(2 * np.pi * 0.23 * t + 0.3)
    return (0.42 * rumble * flicker.astype(np.float32) + 0.58 * pops)


def _synth_street(n: int, rng):
    """街道：18–180Hz 车流嗡鸣 + 400–2500Hz 路面细声 + 慢起伏 + 远处经过的呼啸。"""
    import numpy as np

    rumble = _norm(_band_noise(rng, n, hp=18.0, lp=180.0))
    hiss = _norm(_band_noise(rng, n, hp=400.0, lp=2500.0))
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    swell = (0.66 + 0.22 * np.sin(2 * np.pi * 0.031 * t + 0.4)
             + 0.12 * np.sin(2 * np.pi * 0.077 * t + 2.1)).astype(np.float32)

    # 偶发远处车辆经过：约每 9 秒一次，高斯包络先扬后落
    whoosh = np.zeros(n, dtype=np.float32)
    events = max(1, int(round(n / SAMPLE_RATE / 9.0)))
    for _ in range(events):
        span = int(rng.uniform(2.2, 4.0) * SAMPLE_RATE)
        span = max(2, min(span, n))
        start = int(rng.integers(0, max(1, n - span + 1)))
        seg = _band_noise(rng, span, hp=250.0, lp=float(rng.uniform(900, 1800)))
        x = np.linspace(0.0, 1.0, span, dtype=np.float32)
        env = np.exp(-((x - 0.55) ** 2) / (2 * 0.16 ** 2))
        seg = _norm(seg) * env * float(rng.uniform(0.5, 1.0))
        whoosh[start:start + seg.size] += seg

    return 0.50 * rumble * swell + 0.12 * hiss + 0.50 * whoosh


def generate_noise(kind: str, seconds: float, seed: int = 20261107,
                   amplitude: float = 0.28) -> bytes:
    """程序合成噪声底（确定性：同样的 kind+seed 出来一样的波形）。"""
    if kind not in NOISE_KINDS:
        raise ValueError(f"未知的噪声类型：{kind}")
    n = max(1, int(round(float(seconds) * SAMPLE_RATE)))
    import numpy as np

    if kind in _AMBIENCE_OFFSETS:
        rng = np.random.default_rng(seed + _AMBIENCE_OFFSETS[kind])
    else:
        rng = np.random.default_rng(seed + abs(hash(kind)) % 100000)
    peak = max(1.0, amplitude * 32767)

    if kind == "white":
        wave = rng.uniform(-1.0, 1.0, n).astype(np.float32)
    elif kind == "brown":
        # 泄漏积分：白噪过一级低通，能量偏低频，听感更「厚」
        step = rng.uniform(-1.0, 1.0, n).astype(np.float32)
        wave = np.empty(n, dtype=np.float32)
        acc = 0.0
        for i in range(n):          # 一次成型，n 不大（≤几分钟）
            acc = (acc + step[i] * 0.02) * 0.995
            wave[i] = acc
        m = float(np.max(np.abs(wave))) or 1.0
        wave = wave / m
    elif kind == "wind":
        noise = rng.uniform(-1.0, 1.0, n).astype(np.float32)
        # 一阶低通 + 缓慢起伏：像窗外的风
        wave = np.empty(n, dtype=np.float32)
        acc = 0.0
        for i in range(n):
            acc += (noise[i] - acc) * 0.02
            wave[i] = acc
        t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
        lfo = 0.55 + 0.45 * np.sin(2 * math.pi * 0.07 * t)
        wave = wave * lfo
    elif kind == "hum":
        t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
        wave = (0.8 * np.sin(2 * math.pi * 50 * t)
                + 0.25 * np.sin(2 * math.pi * 100 * t)
                + 0.06 * rng.uniform(-1.0, 1.0, n).astype(np.float32)).astype(np.float32)
    elif kind == "rain":
        wave = _synth_rain(n, rng)
        peak = max(peak, _AMBIENCE_PEAK * 32767)
    elif kind == "fire":
        wave = _synth_fire(n, rng)
        peak = max(peak, _AMBIENCE_PEAK * 32767)
    else:  # street
        wave = _synth_street(n, rng)
        peak = max(peak, _AMBIENCE_PEAK * 32767)

    wave = wave / (float(np.max(np.abs(wave))) or 1.0) * peak
    return np.clip(wave, -32768, 32767).astype("<i2").tobytes()


# ---------------------------------------------------------------------------
# PCM 工具
# ---------------------------------------------------------------------------
def pcm_duration(pcm: bytes) -> float:
    """PCM(s16le mono 24k) 时长（秒）。"""
    return len(pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE)


def loop_pcm(pcm: bytes, seconds: float) -> bytes:
    """把素材循环铺满指定时长；素材为空返回空。"""
    if not pcm:
        return b""
    want = max(0, int(round(float(seconds) * SAMPLE_RATE))) * BYTES_PER_SAMPLE
    if want == 0:
        return b""
    if len(pcm) >= want:
        return pcm[:want]
    reps = want // len(pcm) + 1
    return (pcm * reps)[:want]


def fade_pcm(pcm: bytes, fade_in: float = 0.0, fade_out: float = 0.0) -> bytes:
    """线性淡入淡出（秒）。"""
    if not pcm:
        return b""
    import numpy as np

    wave = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = wave.size
    fin = min(n, int(round(max(0.0, fade_in) * SAMPLE_RATE)))
    fout = min(n, int(round(max(0.0, fade_out) * SAMPLE_RATE)))
    if fin > 0:
        ramp = np.linspace(0.0, 1.0, fin, dtype=np.float32)
        wave[:fin] *= ramp
    if fout > 0:
        ramp = np.linspace(1.0, 0.0, fout, dtype=np.float32)
        wave[n - fout:] *= ramp
    return np.clip(wave, -32768, 32767).astype("<i2").tobytes()


def write_wav(pcm: bytes) -> bytes:
    """PCM(s16le mono 24k) → WAV。**不做响度归一化**（见模块说明）。"""
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


# ---------------------------------------------------------------------------
# 混音
# ---------------------------------------------------------------------------
def _duck_envelope(n: int, timeline: list[dict], duck_db: float,
                   release: float = 0.25) -> list[float]:
    """按时间轴生成铺底增益包络：人声段降到 duck_db，其余为 1.0。

    人声段边界做斜坡（0.2s 压下去、0.25s 抬回来），避免硬切出现「咔哒」。
    """
    env = [1.0] * n
    if not timeline or duck_db >= 0:
        return env
    gain = 10 ** (duck_db / 20.0)
    attack = int(0.2 * SAMPLE_RATE)
    rel = int(release * SAMPLE_RATE)
    for row in timeline:
        try:
            start = int(float(row.get("start", 0)) * SAMPLE_RATE)
            end = int(float(row.get("end", 0)) * SAMPLE_RATE)
        except (TypeError, ValueError):
            continue
        start = max(0, min(n, start))
        end = max(start, min(n, end))
        if end <= start:
            continue
        for i in range(start, end):
            env[i] = gain
        # 进入人声：0.2s 内把铺底压下去；离开人声：0.25s 内抬回来。
        # 两段斜坡写在常量段之后（覆盖），避免出现「整段都是斜坡」的形状
        a0 = start
        a1 = min(end, start + attack)
        if attack > 0 and a1 > a0:
            span = a1 - a0
            for i in range(a0, a1):
                env[i] = 1.0 + (gain - 1.0) * (i - a0) / span
        r0 = max(start, end - rel)
        r1 = end
        if rel > 0 and r1 > r0:
            span = r1 - r0
            for i in range(r0, r1):
                env[i] = gain + (1.0 - gain) * (i - r0) / span
    return env


def build_mix(voice_pcm: bytes, beds: list[dict], timeline: list[dict],
              duck_db: float = -12.0) -> bytes:
    """干声 + 铺底 → ducking → 限幅，返回混音 PCM。

    beds 每项：{"pcm": bytes, "start": 秒, "gain": 0–1, "fade_in": 秒,
    "fade_out": 秒}；start 是相对整轨起点的放置位置。
    """
    import numpy as np

    n = len(voice_pcm) // BYTES_PER_SAMPLE
    if n == 0:
        return b""
    voice = np.frombuffer(voice_pcm, dtype="<i2").astype(np.float32)
    env = np.asarray(_duck_envelope(n, timeline, duck_db), dtype=np.float32)
    out = voice.copy()

    for bed in beds:
        raw = bed.get("pcm") or b""
        if not raw:
            continue
        gain = float(bed.get("gain", 1.0))
        if gain <= 0:
            continue
        wave = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        wave = wave * gain
        wave = np.frombuffer(
            fade_pcm(wave.astype("<i2").tobytes(),
                     float(bed.get("fade_in", 0.0)),
                     float(bed.get("fade_out", 0.0))),
            dtype="<i2").astype(np.float32)
        start = max(0, int(float(bed.get("start", 0.0)) * SAMPLE_RATE))
        if start >= n:
            continue
        seg = wave[: max(0, n - start)]
        if seg.size == 0:
            continue
        out[start:start + seg.size] += seg * env[start:start + seg.size]

    return np.clip(out, -32768, 32767).astype("<i2").tobytes()


def scene_ranges(timeline: list[dict]) -> list[tuple[str, float, float]]:
    """从时间轴推出每个场次的时间范围 [(场次名, 起, 止)]。

    场次名取自行的 scene 字段；同一场次连续出现才合并成一段。
    """
    out: list[tuple[str, float, float]] = []
    for row in timeline:
        name = str(row.get("scene") or "").strip()
        try:
            start = float(row.get("start", 0))
            end = float(row.get("end", 0))
        except (TypeError, ValueError):
            continue
        if out and out[-1][0] == name:
            out[-1] = (name, out[-1][1], max(out[-1][2], end))
        else:
            out.append((name, start, end))
    return out
