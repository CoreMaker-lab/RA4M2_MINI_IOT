# -*- coding: utf-8 -*-
"""
voice_tcp_agent_aliyun_u16.py

用途：
- 解决 RA4M2 本地喇叭播放正常，但 PC 端生成 WAV 像“变声器”的问题。
- 该版本要求 MCU 端上传 uint16_t ADC 原始数据，而不是上传 uint8_t buzzer_num[]。
- MCU 本地播放可以继续使用原来的 buzzer_num[]，不影响喇叭播放。

MCU 端上传格式：
- 包头仍然是 8 字节：
  uint16_t magic   = 0xA55A
  uint16_t session
  uint16_t seq
  uint16_t len     = payload 字节数
- payload 改为 uint16_t audio_adc_raw[] 的原始小端字节流。
- 总字节数应为 40000 * 2 = 80000 bytes。

PC 端输出：
- voice_sessions/session-x.raw16
- voice_sessions/session-x_u16.wav
- answer_sessions/answer-x.wav
- answer_sessions/result-x.json

运行：
    python voice_tcp_agent_aliyun_u16.py

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
# 2. TCP 包格式
# =========================================================

MAGIC = 0xA55A
HEADER_FMT = "<HHHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


# =========================================================
# 3. 工具函数
# =========================================================

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


def u16_raw_to_s16_wav(raw_u16_bytes: bytes, sample_rate: int = 8000) -> bytes:
    """
    uint16_t ADC 原始数据 -> 标准 16-bit PCM WAV。

    处理方式：
    1. 按 little-endian 解析 uint16_t。
    2. 使用整段均值作为中心点，去除直流偏置。
    3. 自动增益到合适音量。
    4. 不做变调、不升采样、不强行滤波。
    """
    if len(raw_u16_bytes) < 2:
        raise ValueError("raw_u16_bytes too short")

    if len(raw_u16_bytes) % 2 != 0:
        print("[WARN] raw_u16_bytes length is odd, drop last byte")
        raw_u16_bytes = raw_u16_bytes[:-1]

    sample_count = len(raw_u16_bytes) // 2
    samples = list(struct.unpack("<%dH" % sample_count, raw_u16_bytes))

    v_min = min(samples)
    v_max = max(samples)
    v_mean = sum(samples) / len(samples)
    dynamic = v_max - v_min

    print(
        f"[AUDIO-U16] samples={sample_count}, "
        f"min={v_min}, max={v_max}, mean={v_mean:.2f}, dynamic={dynamic}"
    )

    x = [float(v) - v_mean for v in samples]

    # 轻微 3 点平滑，只用于减少采样毛刺，不改变音调
    if len(x) >= 3:
        y = [0.0] * len(x)
        y[0] = x[0]
        y[-1] = x[-1]
        for i in range(1, len(x) - 1):
            y[i] = 0.2 * x[i - 1] + 0.6 * x[i] + 0.2 * x[i + 1]
    else:
        y = x

    peak = max(abs(min(y)), abs(max(y)))
    if peak < 1.0:
        peak = 1.0

    gain = 26000.0 / peak

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


# =========================================================
# 4. ASR / Agent / TTS / IoT
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

    resp = requests.post(
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
        print("[IoT] paho-mqtt not installed, skip")
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
        },
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
# 5. TCP 接收与处理
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


def process_session(session_id: int, chunks: Dict[int, bytes]) -> None:
    if not chunks:
        return

    seqs = sorted(chunks.keys())
    missing = [i for i in range(seqs[0], seqs[-1] + 1) if i not in chunks]
    if missing:
        print("[WARN] missing seq:", missing[:30], "..." if len(missing) > 30 else "")

    raw_u16_bytes = b"".join(chunks[i] for i in seqs)

    print(f"[PROC] session={session_id}, raw_bytes={len(raw_u16_bytes)}, chunks={len(chunks)}")
    if len(raw_u16_bytes) == 40000:
        print("[WARN] 当前只收到 40000 bytes，像是 MCU 仍在上传 uint8_t 数据；u16 上传应为 80000 bytes。")
    elif len(raw_u16_bytes) == 80000:
        print("[OK] 收到 80000 bytes，符合 40000 个 uint16_t 采样。")

    raw_path = VOICE_DIR / f"session-{session_id}.raw16"
    save_bytes(raw_path, raw_u16_bytes)
    print(f"[PROC] saved raw16: {raw_path}")

    wav_bytes = u16_raw_to_s16_wav(raw_u16_bytes, AUDIO_SAMPLE_RATE)
    wav_path = VOICE_DIR / f"session-{session_id}_u16.wav"
    save_bytes(wav_path, wav_bytes)
    print(f"[PROC] saved wav: {wav_path}")

    try:
        asr_text = asr_once_wav(wav_bytes, AUDIO_SAMPLE_RATE)
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
            "raw16": str(raw_path),
            "input_wav": str(wav_path),
            "answer_wav": str(answer_path),
            "created_at": int(time.time()),
        }
        result_path = ANSWER_DIR / f"result-{session_id}.json"
        save_bytes(result_path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        print(f"[PROC] saved result json: {result_path}")

        aliyun_iot_publish(session_id, asr_text, reply_text, answer_wav)

    except Exception as e:
        print("[ERROR] process failed:", repr(e))


def main() -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("RA4M2 TCP Voice Receiver - U16 ADC Raw")
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
