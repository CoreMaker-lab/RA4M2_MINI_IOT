# -*- coding: utf-8 -*-
"""
make_audio_rate_variants.py

用途：
- 用已有的 raw / raw16 音频文件生成多个采样率版本的 WAV。
- 用来判断“PC端像变声器”是不是采样率写错导致的。
- 不需要连接板子，不需要重新录音，直接处理 voice_sessions 里的文件。

支持输入：
1. 16bit 原始 ADC 文件：
   voice_sessions/session-2.raw16
2. 8bit 原始文件：
   voice_sessions/session-2.raw

运行示例：
    python make_audio_rate_variants.py voice_sessions/session-2.raw16
    python make_audio_rate_variants.py voice_sessions/session-4.raw

输出目录：
    voice_sessions/rate_test_session-2/
        session-2_rate4000.wav
        session-2_rate5000.wav
        session-2_rate6000.wav
        session-2_rate6400.wav
        session-2_rate7000.wav
        session-2_rate8000.wav
        session-2_rate9600.wav
        session-2_rate11025.wav
        session-2_rate12000.wav
        session-2_rate16000.wav

重点：
- 哪个文件听起来最接近 MCU 喇叭播放，哪个采样率就是 PC 端应该写入 WAV 的真实采样率。
"""

import io
import os
import sys
import wave
import struct
from pathlib import Path


TEST_RATES = [
    4000,
    5000,
    6000,
    6400,
    7000,
    7500,
    8000,
    8500,
    9000,
    9600,
    10000,
    11025,
    12000,
    16000,
]


def clamp_s16(x: float) -> int:
    x = int(round(x))
    if x > 32767:
        return 32767
    if x < -32768:
        return -32768
    return x


def pcm16_from_raw8(raw: bytes) -> bytes:
    """
    raw uint8 音频 -> signed 16bit PCM
    使用实际均值作为中心，避免中心不是128时失真。
    """
    if not raw:
        return b""

    vals = [float(b) for b in raw]
    v_min = min(vals)
    v_max = max(vals)
    v_mean = sum(vals) / len(vals)
    dynamic = v_max - v_min

    print(f"[RAW8] samples={len(vals)}, min={v_min:.1f}, max={v_max:.1f}, mean={v_mean:.2f}, dynamic={dynamic:.1f}")

    x = [v - v_mean for v in vals]

    peak = max(abs(min(x)), abs(max(x)))
    if peak < 1.0:
        peak = 1.0

    gain = 26000.0 / peak

    out = bytearray()
    for v in x:
        out += struct.pack("<h", clamp_s16(v * gain))

    return bytes(out)


def pcm16_from_raw16(raw: bytes) -> bytes:
    """
    raw uint16 little-endian ADC -> signed 16bit PCM
    使用实际均值作为中心，自动增益。
    """
    if len(raw) % 2:
        raw = raw[:-1]

    n = len(raw) // 2
    if n == 0:
        return b""

    vals = list(struct.unpack("<%dH" % n, raw))

    v_min = min(vals)
    v_max = max(vals)
    v_mean = sum(vals) / len(vals)
    dynamic = v_max - v_min

    print(f"[RAW16] samples={len(vals)}, min={v_min}, max={v_max}, mean={v_mean:.2f}, dynamic={dynamic}")

    x = [float(v) - v_mean for v in vals]

    peak = max(abs(min(x)), abs(max(x)))
    if peak < 1.0:
        peak = 1.0

    gain = 26000.0 / peak

    out = bytearray()
    for v in x:
        out += struct.pack("<h", clamp_s16(v * gain))

    return bytes(out)


def write_wav(path: Path, pcm16: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python make_audio_rate_variants.py voice_sessions/session-2.raw16")
        print("  python make_audio_rate_variants.py voice_sessions/session-4.raw")
        raise SystemExit(1)

    in_path = Path(sys.argv[1])
    if not in_path.exists():
        raise SystemExit(f"file not found: {in_path}")

    raw = in_path.read_bytes()

    # 根据后缀判断格式
    if in_path.suffix.lower() == ".raw16":
        pcm16 = pcm16_from_raw16(raw)
    elif in_path.suffix.lower() == ".raw":
        # 如果 raw 文件长度是 80000，也可能其实是16bit数据
        if len(raw) == 80000:
            print("[INFO] .raw length is 80000, treat as raw16")
            pcm16 = pcm16_from_raw16(raw)
        else:
            print("[INFO] treat as raw8")
            pcm16 = pcm16_from_raw8(raw)
    else:
        # 兜底：80000按16bit，否则按8bit
        if len(raw) % 2 == 0 and len(raw) >= 70000:
            print("[INFO] unknown suffix, length looks like raw16")
            pcm16 = pcm16_from_raw16(raw)
        else:
            print("[INFO] unknown suffix, treat as raw8")
            pcm16 = pcm16_from_raw8(raw)

    base = in_path.stem
    out_dir = in_path.parent / f"rate_test_{base}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("\nGenerate WAV variants:")
    for rate in TEST_RATES:
        out_path = out_dir / f"{base}_rate{rate}.wav"
        write_wav(out_path, pcm16, rate)
        print("  ", out_path)

    print("\n请逐个试听这些文件。")
    print("哪个最接近 MCU 喇叭播放，哪个就是 PC 端应该使用的真实采样率。")
    print("例如 rate6400 最正常，就把主程序里的 AUDIO_SAMPLE_RATE 设置为 6400。")


if __name__ == "__main__":
    main()
