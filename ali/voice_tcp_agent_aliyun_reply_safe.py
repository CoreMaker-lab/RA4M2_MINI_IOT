# -*- coding: utf-8 -*-
"""
voice_tcp_agent_aliyun_reply_safe.py

RA4M2 语音闭环稳定版：
- MCU 上传录音到 PC:9000
- PC ASR / Agent / TTS
- 如果 NLS token 过期或 TTS 失败，仍然生成本地 beep 测试音并下发
- MCU 再连接 PC:9000，PC 下发 40000 bytes 的 uint8_t 音频
"""

import os
import io
import json
import math
import wave
import time
import socket
import struct
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


TCP_HOST = os.environ.get("TCP_HOST", "0.0.0.0")
TCP_PORT = int(os.environ.get("TCP_PORT", "9000"))
REPLY_TCP_HOST = os.environ.get("REPLY_TCP_HOST", "0.0.0.0")
REPLY_TCP_PORT = int(os.environ.get("REPLY_TCP_PORT", "9000"))

AUDIO_SAMPLE_RATE = int(os.environ.get("AUDIO_SAMPLE_RATE", "8000"))
AUDIO_SAMPLE_COUNT = int(os.environ.get("AUDIO_SAMPLE_COUNT", "40000"))

VOICE_DIR = Path(os.environ.get("VOICE_DIR", "voice_sessions"))
ANSWER_DIR = Path(os.environ.get("ANSWER_DIR", "answer_sessions"))

AGENT_URL = os.environ.get(
    "AGENT_URL",
    "https://1930052576475971.agentrun-data.cn-hangzhou.aliyuncs.com/"
    "agent-runtimes/RA4M2_1/endpoints/Default/invocations/"
    "openai/v1/chat/completions"
)

NLS_REGION = os.environ.get("NLS_REGION", "cn-shanghai")
NLS_GATEWAY = f"https://nls-gateway-{NLS_REGION}.aliyuncs.com"
NLS_APPKEY = os.environ.get("NLS_APPKEY", "")
NLS_TOKEN = os.environ.get("NLS_TOKEN", "")
NLS_VOICE = os.environ.get("NLS_VOICE", "xiaoyun")

MAGIC_UPLOAD = 0xA55A
MAGIC_REPLY = 0xA55B
CHUNK = 512
HEADER_FMT = "<HHHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


def make_http() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


HTTP = make_http()


def save_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def clamp_s16(x: float) -> int:
    x = int(round(x))
    if x > 32767:
        return 32767
    if x < -32768:
        return -32768
    return x


def raw_u8_to_input_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    if not raw_u8:
        raise ValueError("empty raw audio")

    vals = [float(b) for b in raw_u8]
    v_min = min(vals)
    v_max = max(vals)
    v_mean = sum(vals) / len(vals)
    dynamic = v_max - v_min

    print(
        f"[AUDIO-U8] samples={len(vals)}, min={v_min:.1f}, "
        f"max={v_max:.1f}, mean={v_mean:.2f}, dynamic={dynamic:.1f}"
    )

    x = [v - v_mean for v in vals]

    if len(x) >= 3:
        y = [0.0] * len(x)
        y[0] = x[0]
        y[-1] = x[-1]
        for i in range(1, len(x) - 1):
            y[i] = 0.2 * x[i - 1] + 0.6 * x[i] + 0.2 * x[i + 1]
    else:
        y = x

    peak = max(abs(min(y)), abs(max(y)))
    if peak < 1:
        peak = 1

    gain = 24000.0 / peak
    pcm16 = bytearray()

    for v in y:
        pcm16 += struct.pack("<h", clamp_s16(v * gain))

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(pcm16))

    return out.getvalue()


def wav_to_mcu_u8(wav_bytes: bytes, target_samples: int = 40000) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        fr = wf.getframerate()
        nframes = wf.getnframes()
        frames = wf.readframes(nframes)

    print(f"[REPLY-AUDIO] wav channels={nch}, sampwidth={sw}, rate={fr}, frames={nframes}")

    samples_u8 = bytearray()

    if sw == 1:
        if nch == 1:
            samples_u8.extend(frames)
        else:
            samples_u8.extend(frames[0::nch])
    elif sw == 2:
        total_samples = len(frames) // 2
        vals = struct.unpack("<%dh" % total_samples, frames)

        if nch > 1:
            vals = vals[0::nch]

        for s in vals:
            b = (int(s) >> 8) + 128
            if b < 0:
                b = 0
            elif b > 255:
                b = 255
            samples_u8.append(b)
    else:
        raise ValueError(f"unsupported wav sampwidth={sw}")

    if fr != AUDIO_SAMPLE_RATE and len(samples_u8) > 1:
        print(f"[WARN] TTS rate={fr}, resample to {AUDIO_SAMPLE_RATE}")
        src = samples_u8
        new_len = int(len(src) * AUDIO_SAMPLE_RATE / fr)
        if new_len <= 0:
            new_len = len(src)

        dst = bytearray()
        for i in range(new_len):
            pos = i * (len(src) - 1) / max(1, new_len - 1)
            j = int(pos)
            t = pos - j
            if j + 1 < len(src):
                v = src[j] * (1.0 - t) + src[j + 1] * t
            else:
                v = src[j]
            dst.append(int(round(v)))
        samples_u8 = dst

    if len(samples_u8) > target_samples:
        print(f"[REPLY-AUDIO] truncate {len(samples_u8)} -> {target_samples}")
        samples_u8 = samples_u8[:target_samples]
    elif len(samples_u8) < target_samples:
        print(f"[REPLY-AUDIO] pad {len(samples_u8)} -> {target_samples}")
        samples_u8.extend([128] * (target_samples - len(samples_u8)))

    return bytes(samples_u8)


def make_fallback_beep_u8(target_samples: int = 40000, sample_rate: int = 8000) -> bytes:
    data = bytearray([128] * target_samples)
    tone_freq = 880.0
    amp = 45

    segments = [
        (0.20, 0.45),
        (0.65, 0.90),
        (1.10, 1.45),
    ]

    for start_s, end_s in segments:
        start = int(start_s * sample_rate)
        end = min(int(end_s * sample_rate), target_samples)

        for n in range(start, end):
            v = 128 + amp * math.sin(2 * math.pi * tone_freq * n / sample_rate)
            data[n] = max(0, min(255, int(round(v))))

    return bytes(data)


def raw_u8_to_debug_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    pcm16 = bytearray()
    for b in raw_u8:
        s = (int(b) - 128) * 256
        pcm16 += struct.pack("<h", clamp_s16(s))

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(pcm16))

    return out.getvalue()


def check_nls_config() -> None:
    if not NLS_APPKEY or not NLS_TOKEN:
        raise RuntimeError("NLS_APPKEY or NLS_TOKEN is empty")


def asr_once_wav(wav_bytes: bytes, sample_rate: int = 8000) -> str:
    check_nls_config()

    url = f"{NLS_GATEWAY}/stream/v1/asr"
    params = {
        "appkey": NLS_APPKEY,
        "format": "wav",
        "sample_rate": str(sample_rate),
        "enable_punctuation_prediction": "true",
        "enable_inverse_text_normalization": "true",
    }
    headers = {
        "X-NLS-Token": NLS_TOKEN,
        "Content-Type": "application/octet-stream",
    }

    resp = HTTP.post(url, params=params, headers=headers, data=wav_bytes, timeout=30)

    print("[ASR] status:", resp.status_code)
    print("[ASR] raw:", resp.text[:500])

    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != 20000000:
        raise RuntimeError(f"ASR failed: {data}")

    return data.get("result", "").strip()


def call_ra4m2_agent(user_text: str) -> str:
    payload = {
        "messages": [
            {
                "role": "system",
                "content": "你是 RA4M2 智能语音助手。请用简洁自然的中文回答，适合语音播报，尽量控制在一句话。"
            },
            {
                "role": "user",
                "content": user_text
            }
        ],
        "stream": False
    }

    resp = HTTP.post(
        AGENT_URL,
        headers={
            "content-type": "application/json",
            "x-agentrun-session-id": "ra4m2-voice-session"
        },
        json=payload,
        timeout=30,
    )

    print("[Agent] status:", resp.status_code)
    print("[Agent] raw:", resp.text[:500])

    resp.raise_for_status()
    data = resp.json()

    return data["choices"][0]["message"]["content"].strip()


def tts_wav(text: str, sample_rate: int = 8000) -> bytes:
    check_nls_config()

    url = f"{NLS_GATEWAY}/stream/v1/tts"
    params = {
        "appkey": NLS_APPKEY,
        "token": NLS_TOKEN,
        "format": "wav",
        "sample_rate": str(sample_rate),
        "voice": NLS_VOICE,
        "text": text,
    }

    resp = HTTP.post(url, params=params, timeout=30)

    print("[TTS] status:", resp.status_code)

    if resp.status_code != 200:
        print("[TTS] raw:", resp.text[:500])
        resp.raise_for_status()

    return resp.content


def recv_exact(conn: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()

    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk

    return bytes(buf)


def receive_one_upload(conn: socket.socket, addr: Tuple[str, int]) -> Optional[Tuple[int, bytes]]:
    print(f"\n[TCP-UP] client connected: {addr}")

    sessions: Dict[int, Dict[int, bytes]] = {}

    while True:
        hdr = recv_exact(conn, HEADER_SIZE)

        if hdr is None:
            print("[TCP-UP] client closed")
            break

        magic, session_id, seq, payload_len = struct.unpack(HEADER_FMT, hdr)

        if magic != MAGIC_UPLOAD:
            print(f"[TCP-UP] bad magic: 0x{magic:04X}, ignore")
            break

        payload = recv_exact(conn, payload_len)

        if payload is None:
            print("[TCP-UP] closed while reading payload")
            break

        if session_id not in sessions:
            sessions[session_id] = {}
            print(f"[TCP-UP] new session {session_id}")

        sessions[session_id][seq] = payload

        got_bytes = sum(len(x) for x in sessions[session_id].values())
        print(f"[TCP-UP] session={session_id}, seq={seq}, len={payload_len}, total={got_bytes}")

    if not sessions:
        print("[TCP-UP] no upload session received, ignore")
        return None

    session_id = sorted(sessions.keys())[0]
    chunks = sessions[session_id]
    raw = b"".join(chunks[i] for i in sorted(chunks.keys()))

    print(f"[TCP-UP] complete session={session_id}, raw_bytes={len(raw)}, chunks={len(chunks)}")

    return session_id, raw


def send_reply_to_board(session_id: int, raw_u8: bytes) -> bool:
    print("=" * 80)
    print(f"[TCP-DOWN] waiting board connect on {REPLY_TCP_HOST}:{REPLY_TCP_PORT}")
    print("[TCP-DOWN] MCU should connect answer server now")
    print("=" * 80)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((REPLY_TCP_HOST, REPLY_TCP_PORT))
            srv.listen(1)

            conn, addr = srv.accept()

            with conn:
                print(f"[TCP-DOWN] board connected: {addr}")

                off = 0
                seq = 0

                while off < len(raw_u8):
                    chunk = raw_u8[off:off + CHUNK]
                    hdr = struct.pack(HEADER_FMT, MAGIC_REPLY, int(session_id), int(seq), len(chunk))

                    conn.sendall(hdr + chunk)

                    off += len(chunk)
                    seq += 1
                    time.sleep(0.003)

                print(f"[TCP-DOWN] sent reply audio: bytes={len(raw_u8)}, packets={seq}")
                return True

    except Exception as e:
        print("[TCP-DOWN] send failed:", repr(e))
        return False


def process_upload(session_id: int, raw_u8: bytes) -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)

    raw_path = VOICE_DIR / f"session-{session_id}.raw"
    save_bytes(raw_path, raw_u8)
    print(f"[PROC] saved raw: {raw_path}")

    input_wav = raw_u8_to_input_wav(raw_u8, AUDIO_SAMPLE_RATE)
    input_wav_path = VOICE_DIR / f"session-{session_id}_input.wav"
    save_bytes(input_wav_path, input_wav)
    print(f"[PROC] saved input wav: {input_wav_path}")

    asr_text = ""
    reply_text = ""
    cloud_ok = False

    try:
        asr_text = asr_once_wav(input_wav, AUDIO_SAMPLE_RATE)
        print("[PROC] ASR text:", asr_text)
    except Exception as e:
        print("[WARN] ASR failed:", repr(e))

    try:
        if asr_text:
            reply_text = call_ra4m2_agent(asr_text)
        else:
            reply_text = "您好，我没有听清楚，请再说一遍。"
        print("[PROC] Agent/fallback reply:", reply_text)
    except Exception as e:
        print("[WARN] Agent failed:", repr(e))
        reply_text = "您好，网络异常，请稍后再试。"

    try:
        answer_wav = tts_wav(reply_text, AUDIO_SAMPLE_RATE)
        answer_wav_path = ANSWER_DIR / f"answer-{session_id}.wav"
        save_bytes(answer_wav_path, answer_wav)
        print(f"[PROC] saved answer wav: {answer_wav_path}")

        answer_raw_u8 = wav_to_mcu_u8(answer_wav, AUDIO_SAMPLE_COUNT)
        cloud_ok = True

    except Exception as e:
        print("[WARN] TTS failed, use local beep instead:", repr(e))
        answer_raw_u8 = make_fallback_beep_u8(AUDIO_SAMPLE_COUNT, AUDIO_SAMPLE_RATE)

        fallback_wav = raw_u8_to_debug_wav(answer_raw_u8, AUDIO_SAMPLE_RATE)
        fallback_wav_path = ANSWER_DIR / f"answer-{session_id}_fallback_beep.wav"
        save_bytes(fallback_wav_path, fallback_wav)
        print(f"[PROC] saved fallback wav: {fallback_wav_path}")

    answer_raw_path = ANSWER_DIR / f"answer-{session_id}.raw"
    save_bytes(answer_raw_path, answer_raw_u8)
    print(f"[PROC] saved answer raw u8: {answer_raw_path}")

    result = {
        "session_id": int(session_id),
        "asr_text": asr_text,
        "reply_text": reply_text,
        "cloud_ok": cloud_ok,
        "input_wav": str(input_wav_path),
        "answer_raw": str(answer_raw_path),
        "reply_port": REPLY_TCP_PORT,
        "created_at": int(time.time()),
    }

    result_path = ANSWER_DIR / f"result-{session_id}.json"
    save_bytes(result_path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"[PROC] saved result json: {result_path}")

    ok = send_reply_to_board(session_id, answer_raw_u8)
    print("[PROC] send reply result:", ok)


def main() -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("RA4M2 Voice Closed Loop SAFE")
    print(f"Upload TCP listen : {TCP_HOST}:{TCP_PORT}")
    print(f"Reply  TCP listen : {REPLY_TCP_HOST}:{REPLY_TCP_PORT}")
    print(f"Sample rate       : {AUDIO_SAMPLE_RATE}")
    print(f"Sample count      : {AUDIO_SAMPLE_COUNT}")
    print(f"NLS gateway       : {NLS_GATEWAY}")
    print("=" * 80)

    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((TCP_HOST, TCP_PORT))
            srv.listen(1)

            print("[TCP-UP] waiting for RA4M2 upload ...")
            conn, addr = srv.accept()

            with conn:
                item = receive_one_upload(conn, addr)

        if item is None:
            print("[MAIN] ignore empty connection, continue")
            continue

        session_id, raw = item

        try:
            process_upload(session_id, raw)
        except Exception as e:
            print("[ERROR] process_upload failed:", repr(e))

        print("[MAIN] wait next recording ...")


if __name__ == "__main__":
    main()
