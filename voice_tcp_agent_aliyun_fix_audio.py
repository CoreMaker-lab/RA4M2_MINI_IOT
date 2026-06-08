# -*- coding: utf-8 -*-
"""
voice_tcp_agent_aliyun_fix_audio.py

修复重点：
- MCU 喇叭播放正常，但 PC 端 WAV 像“变声器”，主要原因通常是：
  MCU 的 buzzer_num[] 不是标准 8-bit PCM。
  标准 8-bit WAV 的静音中心是 128，但你的上传数据中心可能是 7、20、80 等。
- 本程序不再直接把 raw_u8 写成 WAV，也不再固定按 128 做中心。
- 它会根据每次录音的实际 min / max / mean 自动重映射，生成更接近 MCU 喇叭听感的 WAV。

输出：
voice_sessions/session-1.raw
voice_sessions/session-1_fixed.wav      推荐试听
voice_sessions/session-1_asr.wav        给阿里云 ASR
answer_sessions/answer-1.wav            TTS 回复

运行：
python voice_tcp_agent_aliyun_fix_audio.py

依赖：
python -m pip install requests paho-mqtt

环境变量：
$env:NLS_APPKEY="你的NLS_APPKEY"
$env:NLS_TOKEN="你的NLS_TOKEN"
$env:ALIYUN_IOT_ENABLE="0"
"""

import os
import io
import json
import wave
import time
import socket
import struct
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None


# =========================================================
# 1. 配置
# =========================================================

TCP_HOST = os.environ.get("TCP_HOST", "0.0.0.0")
TCP_PORT = int(os.environ.get("TCP_PORT", "9000"))

AUDIO_SAMPLE_RATE = int(os.environ.get("AUDIO_SAMPLE_RATE", "8000"))

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

ALIYUN_IOT_ENABLE = os.environ.get("ALIYUN_IOT_ENABLE", "0") == "1"
ALIYUN_MQTT_HOST = os.environ.get("ALIYUN_MQTT_HOST", "")
ALIYUN_MQTT_PORT = int(os.environ.get("ALIYUN_MQTT_PORT", "1883"))
ALIYUN_PRODUCT_KEY = os.environ.get("ALIYUN_PRODUCT_KEY", "")
ALIYUN_DEVICE_NAME = os.environ.get("ALIYUN_DEVICE_NAME", "")
ALIYUN_MQTT_CLIENT_ID = os.environ.get("ALIYUN_MQTT_CLIENT_ID", "")
ALIYUN_MQTT_USERNAME = os.environ.get("ALIYUN_MQTT_USERNAME", "")
ALIYUN_MQTT_PASSWORD = os.environ.get("ALIYUN_MQTT_PASSWORD", "")
ALIYUN_PUB_TOPIC = os.environ.get(
    "ALIYUN_PUB_TOPIC",
    f"/sys/{ALIYUN_PRODUCT_KEY}/{ALIYUN_DEVICE_NAME}/thing/event/property/post"
    if ALIYUN_PRODUCT_KEY and ALIYUN_DEVICE_NAME else ""
)


# =========================================================
# 2. MCU 包头
# =========================================================

MAGIC = 0xA55A
HEADER_FMT = "<HHHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


# =========================================================
# 3. 音频修复函数
# =========================================================

def _clamp_s16(x: float) -> int:
    x = int(round(x))
    if x > 32767:
        return 32767
    if x < -32768:
        return -32768
    return x


def raw_u8_to_fixed_s16_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    """
    推荐试听版本 / ASR 版本。

    核心思路：
    1. 不假设 raw_u8 的中心是 128。
    2. 用整段平均值 mean 作为静音中心。
    3. 自动估计峰值并放大。
    4. 加非常轻的平滑，降低 8bit/低动态范围带来的“机器人/变声器”颗粒感。
    5. 不改变采样率，不做变调处理。
    """
    if not raw_u8:
        return b""

    vals = [float(b) for b in raw_u8]

    v_min = min(vals)
    v_max = max(vals)
    v_mean = sum(vals) / len(vals)
    dynamic = v_max - v_min

    print(f"[AUDIO] samples={len(vals)}, min={v_min:.1f}, max={v_max:.1f}, mean={v_mean:.2f}, dynamic={dynamic:.1f}")

    # 去直流：以实际均值作为中心，而不是固定 128
    x = [v - v_mean for v in vals]

    # 如果动态范围很小，说明原始数据只有很少几个台阶。
    # MCU 喇叭会被模拟电路自然平滑，PC 端需要轻微平滑，否则会像机器人。
    # 这里用 3 点平滑，力度不要太大，避免声音变闷。
    if len(x) >= 3:
        y = [0.0] * len(x)
        y[0] = x[0]
        y[-1] = x[-1]
        for i in range(1, len(x) - 1):
            y[i] = 0.25 * x[i - 1] + 0.5 * x[i] + 0.25 * x[i + 1]
    else:
        y = x

    # 自动增益
    peak = max(abs(min(y)), abs(max(y)))
    if peak < 1.0:
        peak = 1.0

    # 动态范围小的时候，不要拉满到 32767，否则量化噪声会被放大得很明显。
    # 26000 比较自然；如果声音小可以改成 30000。
    gain = 26000.0 / peak

    pcm16 = bytearray()
    for v in y:
        pcm16 += struct.pack("<h", _clamp_s16(v * gain))

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(pcm16))

    return out.getvalue()


def raw_u8_to_debug_u8_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    """
    调试用 8-bit WAV。
    注意：这不是直接写 raw_u8，而是把 raw_u8 重新映射到以 128 为中心的 8-bit PCM。
    标准 8-bit WAV 的静音中心必须是 128。
    """
    if not raw_u8:
        return b""

    vals = [float(b) for b in raw_u8]
    v_mean = sum(vals) / len(vals)
    x = [v - v_mean for v in vals]

    peak = max(abs(min(x)), abs(max(x)))
    if peak < 1.0:
        peak = 1.0

    # 映射到 8-bit WAV: 128 ± 110，留一点余量
    gain = 110.0 / peak

    out_u8 = bytearray()
    for v in x:
        b = int(round(128 + v * gain))
        if b < 0:
            b = 0
        elif b > 255:
            b = 255
        out_u8.append(b)

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(out_u8))

    return out.getvalue()


def save_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# =========================================================
# 4. ASR / Agent / TTS
# =========================================================

def check_nls_config() -> None:
    if not NLS_APPKEY or not NLS_TOKEN:
        raise RuntimeError(
            "NLS_APPKEY 或 NLS_TOKEN 为空。请先设置：\n"
            '$env:NLS_APPKEY="你的NLS_APPKEY"\n'
            '$env:NLS_TOKEN="你的NLS_TOKEN"'
        )


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

    resp = requests.post(url, params=params, headers=headers, data=wav_bytes, timeout=30)
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
                "content": "你是 RA4M2 智能语音助手。请用简洁自然的中文回答，适合语音播报。"
            },
            {
                "role": "user",
                "content": user_text
            }
        ],
        "stream": False
    }

    print("[Agent] URL:", AGENT_URL)

    resp = requests.post(
        AGENT_URL,
        headers={
            "content-type": "application/json",
            "x-agentrun-session-id": "ra4m2-voice-session"
        },
        json=payload,
        timeout=30
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

    resp = requests.post(url, params=params, timeout=30)
    print("[TTS] status:", resp.status_code)

    if resp.status_code != 200:
        print("[TTS] raw:", resp.text[:500])
        resp.raise_for_status()

    return resp.content


def aliyun_iot_publish(session_id: int, asr_text: str, reply_text: str, answer_wav: bytes) -> None:
    if not ALIYUN_IOT_ENABLE:
        print("[IoT] disabled")
        return

    if mqtt is None:
        print("[IoT] paho-mqtt not installed")
        return

    required = [
        ALIYUN_MQTT_HOST,
        ALIYUN_MQTT_CLIENT_ID,
        ALIYUN_MQTT_USERNAME,
        ALIYUN_MQTT_PASSWORD,
        ALIYUN_PUB_TOPIC,
    ]
    if not all(required):
        print("[IoT] config incomplete, skip")
        return

    payload = {
        "id": str(int(time.time() * 1000)),
        "version": "1.0",
        "method": "thing.event.property.post",
        "params": {
            "session_id": int(session_id),
            "asr_text": asr_text,
            "reply_text": reply_text,
            "answer_audio_len": len(answer_wav),
        }
    }

    msg = json.dumps(payload, ensure_ascii=False)

    client = mqtt.Client(client_id=ALIYUN_MQTT_CLIENT_ID, protocol=mqtt.MQTTv311)
    client.username_pw_set(ALIYUN_MQTT_USERNAME, ALIYUN_MQTT_PASSWORD)

    print(f"[IoT] connecting {ALIYUN_MQTT_HOST}:{ALIYUN_MQTT_PORT}")
    client.connect(ALIYUN_MQTT_HOST, ALIYUN_MQTT_PORT, keepalive=60)

    print("[IoT] topic:", ALIYUN_PUB_TOPIC)
    print("[IoT] payload:", msg[:500])

    info = client.publish(ALIYUN_PUB_TOPIC, msg, qos=1)
    info.wait_for_publish(timeout=10)

    client.disconnect()
    print("[IoT] publish done")


# =========================================================
# 5. TCP 接收
# =========================================================

def recv_exact(conn: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def handle_one_connection(conn: socket.socket, addr: Tuple[str, int]) -> None:
    print(f"\n[TCP] client connected: {addr}")

    sessions: Dict[int, Dict[int, bytes]] = {}

    while True:
        hdr = recv_exact(conn, HEADER_SIZE)
        if hdr is None:
            print("[TCP] client closed")
            break

        magic, session_id, seq, payload_len = struct.unpack(HEADER_FMT, hdr)

        if magic != MAGIC:
            print(f"[TCP] bad magic: 0x{magic:04X}, expected 0x{MAGIC:04X}")
            break

        payload = recv_exact(conn, payload_len)
        if payload is None:
            print("[TCP] client closed while reading payload")
            break

        if session_id not in sessions:
            sessions[session_id] = {}
            print(f"[TCP] new session {session_id}")

        sessions[session_id][seq] = payload

        got_bytes = sum(len(x) for x in sessions[session_id].values())
        print(f"[TCP] session={session_id}, seq={seq}, payload_len={payload_len}, total_bytes={got_bytes}")

    for session_id, chunks in sessions.items():
        process_session(session_id, chunks)


# =========================================================
# 6. Session 处理
# =========================================================

def process_session(session_id: int, chunks: Dict[int, bytes]) -> None:
    if not chunks:
        return

    seqs = sorted(chunks.keys())
    missing = [i for i in range(seqs[0], seqs[-1] + 1) if i not in chunks]
    if missing:
        print("[WARN] missing seq:", missing[:30], "..." if len(missing) > 30 else "")

    raw_u8 = b"".join(chunks[i] for i in seqs)

    print(f"[PROC] session={session_id}, raw_bytes={len(raw_u8)}, chunks={len(chunks)}")

    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)

    raw_path = VOICE_DIR / f"session-{session_id}.raw"
    save_bytes(raw_path, raw_u8)
    print(f"[PROC] saved raw: {raw_path}")

    # 调试 8-bit WAV：已经重映射到标准 8-bit PCM 中心 128
    debug_u8_wav = raw_u8_to_debug_u8_wav(raw_u8, AUDIO_SAMPLE_RATE)
    debug_u8_path = VOICE_DIR / f"session-{session_id}_debug_u8.wav"
    save_bytes(debug_u8_path, debug_u8_wav)
    print(f"[PROC] saved debug u8 wav: {debug_u8_path}")

    # 推荐试听 / ASR 共用的 16-bit WAV
    fixed_wav = raw_u8_to_fixed_s16_wav(raw_u8, AUDIO_SAMPLE_RATE)
    fixed_path = VOICE_DIR / f"session-{session_id}_fixed.wav"
    save_bytes(fixed_path, fixed_wav)
    print(f"[PROC] saved fixed wav: {fixed_path}")

    try:
        asr_text = asr_once_wav(fixed_wav, AUDIO_SAMPLE_RATE)
        print("[PROC] ASR text:", asr_text)

        if not asr_text:
            print("[PROC] ASR empty, skip Agent/TTS")
            return

        reply_text = call_ra4m2_agent(asr_text)
        print("[PROC] Agent reply:", reply_text)

        answer_wav = tts_wav(reply_text, AUDIO_SAMPLE_RATE)
        answer_path = ANSWER_DIR / f"answer-{session_id}.wav"
        save_bytes(answer_path, answer_wav)
        print(f"[PROC] saved answer wav: {answer_path}")

        result = {
            "session_id": int(session_id),
            "asr_text": asr_text,
            "reply_text": reply_text,
            "raw": str(raw_path),
            "debug_u8_wav": str(debug_u8_path),
            "fixed_wav": str(fixed_path),
            "answer_wav": str(answer_path),
            "created_at": int(time.time()),
        }
        result_path = ANSWER_DIR / f"result-{session_id}.json"
        save_bytes(result_path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        print(f"[PROC] saved result json: {result_path}")

        aliyun_iot_publish(session_id, asr_text, reply_text, answer_wav)

    except Exception as e:
        print("[ERROR] process failed:", repr(e))


# =========================================================
# 7. main
# =========================================================

def main() -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("RA4M2 TCP Voice Receiver - Fixed Audio Mapping")
    print(f"TCP listen : {TCP_HOST}:{TCP_PORT}")
    print(f"SampleRate : {AUDIO_SAMPLE_RATE}")
    print(f"VoiceDir   : {VOICE_DIR}")
    print(f"AnswerDir  : {ANSWER_DIR}")
    print(f"Agent URL  : {AGENT_URL}")
    print(f"IoT upload : {'enable' if ALIYUN_IOT_ENABLE else 'disable'}")
    print("=" * 80)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((TCP_HOST, TCP_PORT))
        srv.listen(1)

        print("[TCP] waiting for RA4M2 connection ...")

        while True:
            conn, addr = srv.accept()
            with conn:
                handle_one_connection(conn, addr)
            print("[TCP] waiting for next connection ...")


if __name__ == "__main__":
    main()
