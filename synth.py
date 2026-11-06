#!/usr/bin/env python
"""breeze-tts 一键合成：调用本地 API（端口 7860 已锁定）并直接输出可试听的 WAV。

用法:
  python synth.py "你好，欢迎使用语音合成。" -o outputs/hello.wav
  python synth.py "Hello there." --instruction "A warm young woman." -o outputs/hello_en.wav
  python synth.py "任意文本" --ref-audio ref.wav --ref-text "参考音频的文字稿" -o out.wav
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
import uuid
from pathlib import Path

API = "http://127.0.0.1:7860/v1/audio/speech"  # 端口已锁定 7860，禁改
SAMPLE_RATE = 24000  # mono s16le


def build_multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for name, path in files.items():
        if path is None:
            continue
        data = path.read_bytes()
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), boundary


def write_wav(pcm: bytes, out: Path) -> None:
    import struct

    n = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + n, b"WAVE",
        b"fmt ", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
        b"data", n,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(header + pcm)


def main() -> int:
    ap = argparse.ArgumentParser(description="Breeze TTS one-shot synthesis to WAV")
    ap.add_argument("text", help="要合成的文本")
    ap.add_argument("-o", "--output", default="outputs/synth.wav", help="输出 WAV 路径")
    ap.add_argument("--instruction", default=None, help="自然语言声音描述/指令")
    ap.add_argument("--ref-audio", type=Path, default=None, help="参考音频（Voice Clone/Direction）")
    ap.add_argument("--ref-text", default=None, help="参考音频的准确文字稿")
    ap.add_argument("--cfg-scale", default="4", help="指令跟随强度（默认 4）")
    ap.add_argument("--seed", default="42", help="随机种子")
    ap.add_argument("--max-new-tokens", default="600", help="最大生成 token 数")
    args = ap.parse_args()

    fields = {
        "text": args.text,
        "cfg_scale": args.cfg_scale,
        "seed": args.seed,
        "max_new_tokens": args.max_new_tokens,
    }
    if args.instruction:
        fields["instruction"] = args.instruction
    if args.ref_text:
        fields["ref_text"] = args.ref_text
    files: dict[str, Path] = {}
    if args.ref_audio:
        files["ref_audio"] = args.ref_audio

    body, boundary = build_multipart(fields, files)
    req = urllib.request.Request(API, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    with urllib.request.urlopen(req, timeout=600) as resp:
        pcm = resp.read()

    if not pcm:
        print("ERROR: empty audio response", file=sys.stderr)
        return 1

    out = Path(args.output)
    write_wav(pcm, out)
    dur = len(pcm) / (SAMPLE_RATE * 2)
    print(f"OK {out}  {dur:.2f}s  {len(pcm)} bytes PCM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
