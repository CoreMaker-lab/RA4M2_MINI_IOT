# -*- coding: utf-8 -*-
"""
voice_tcp_agent_aliyun.py

RA4M2 录音 -> TCP 上传到 PC -> 阿里云 ASR -> AgentRun -> 阿里云 TTS -> 上报阿里云 IoT MQTT

运行：
    python voice_tcp_agent_aliyun.py

依赖：
    python -m pip install requests paho-mqtt

说明：
- RA4M2 当前通过 Audio_Send_To_PC_TCP() 发送的是 uint8_t 原始音频采样，不是 WAV 文件。
- 本程序监听 TCP 9000，接收 A55A 包头 + 音频 payload，重组为 WAV。
- 默认只把识别文本、回答文本、answer.wav 长度上报阿里云 IoT，避免 MQTT payload 太大。
"""

import os
import io
import json
import wave
import time
import base64
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
# 1. 基本配置
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
NLS_APPKEY = os.environ.get("NLS_APPKEY", "请填写你的NLS_APPKEY")
NLS_TOKEN = os.environ.get("NLS_TOKEN", "请填写你的NLS_TOKEN")
NLS_VOICE = os.environ.get("NLS_VOICE", "xiaoyun")

# 阿里云 IoT MQTT。建议用环境变量设置，避免把密钥写进 GitHub。
ALIYUN_IOT_ENABLE = os.environ.get("ALIYUN_IOT_ENABLE", "1") == "1"
ALIYUN_MQTT_HOST = os.environ.get(
    "ALIYUN_MQTT_HOST",
    "a1fabJdOLz0.iot-as-mqtt.cn-shanghai.aliyuncs.com"
)
ALIYUN_MQTT_PORT = int(os.environ.get("ALIYUN_MQTT_PORT", "1883"))
PRODUCT_KEY = os.environ.get("ALIYUN_PRODUCT_KEY", "a1fabJdOLz0")
DEVICE_NAME = os.environ.get("ALIYUN_DEVICE_NAME", "tHV3SyEhr3BrH7JwvMuq")

ALIYUN_MQTT_CLIENT_ID = os.environ.get(
    "ALIYUN_MQTT_CLIENT_ID",
    "请填写clientId，例如 deviceName|securemode=2,signmethod=hmacsha256,timestamp=xxx|"
)
ALIYUN_MQTT_USERNAME = os.environ.get(
    "ALIYUN_MQTT_USERNAME",
    f"{DEVICE_NAME}&{PRODUCT_KEY}"
)
ALIYUN_MQTT_PASSWORD = os.environ.get(
    "ALIYUN_MQTT_PASSWORD",
    "请填写password签名"
)
ALIYUN_PUB_TOPIC = os.environ.get(
    "ALIYUN_PUB_TOPIC",
    f"/sys/{PRODUCT_KEY}/{DEVICE_NAME}/thing/event/property/post"
)


# =========================================================
# 2. RA4M2 音频包格式
# =========================================================

MAGIC = 0xA55A
HEADER_FMT = "<HHHH"   # magic, session, seq, len
HEADER_SIZE = struct.calcsize(HEADER_FMT)

def raw_u8_to_pcm16_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    """
    和 MCU 喇叭播放最接近的版本：
    直接把 buzzer_num[] 保存成 8-bit unsigned PCM WAV。
    不做 16bit 转换，不做增益，不做滤波。
    """
    import io
    import wave

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)       # 关键：8bit WAV
        wf.setframerate(sample_rate)
        wf.writeframes(raw_u8)   # 关键：原始 buzzer_num[] 直接写进去

    return out.getvalue()

def raw_u8_to_u8_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    import io
    import wave

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(sample_rate)
        wf.writeframes(raw_u8)

    return out.getvalue()


def raw_u8_to_s16_wav_for_asr(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    import io
    import wave
    import struct

    pcm16 = bytearray()

    # 只做最基础 unsigned 8bit -> signed 16bit
    # 不做变声器式增益/滤波
    for b in raw_u8:
        x = (int(b) - 128) << 8
        pcm16 += struct.pack("<h", x)

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(pcm16))

    return out.getvalue()


def save_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# =========================================================
# 3. ASR / AgentRun / TTS
# =========================================================


def asr_once_wav(wav_bytes: bytes, sample_rate: int = 8000) -> str:
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
                "content": "你是RA4M2智能语音助手，请用简洁自然的中文回答，适合语音播报。"
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


# =========================================================
# 4. 阿里云 IoT MQTT 上报
# =========================================================


def aliyun_iot_publish(asr_text: str, reply_text: str, answer_wav: bytes, session_id: int) -> None:
    if not ALIYUN_IOT_ENABLE:
        print("[IoT] disabled")
        return

    if mqtt is None:
        print("[IoT] paho-mqtt not installed, skip publish")
        return

    if "请填写" in ALIYUN_MQTT_CLIENT_ID or "请填写" in ALIYUN_MQTT_PASSWORD:
        print("[IoT] MQTT clientId/password not configured, skip publish")
        print("[IoT] 请先设置 ALIYUN_MQTT_CLIENT_ID 和 ALIYUN_MQTT_PASSWORD")
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
            # 如需上传音频 base64，并且物模型有该字符串属性，可打开下面这一行。
            # 注意 5 秒音频 base64 很大，MQTT payload 可能超限。
            # "answer_audio_b64": base64.b64encode(answer_wav).decode("ascii"),
        }
    }

    client = mqtt.Client(client_id=ALIYUN_MQTT_CLIENT_ID, protocol=mqtt.MQTTv311)
    client.username_pw_set(ALIYUN_MQTT_USERNAME, ALIYUN_MQTT_PASSWORD)

    print(f"[IoT] connecting {ALIYUN_MQTT_HOST}:{ALIYUN_MQTT_PORT}")
    client.connect(ALIYUN_MQTT_HOST, ALIYUN_MQTT_PORT, keepalive=60)

    msg = json.dumps(payload, ensure_ascii=False)
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
            print(f"[TCP] bad magic: 0x{magic:04X}, drop connection")
            break

        payload = recv_exact(conn, payload_len)
        if payload is None:
            print("[TCP] closed while reading payload")
            break

        if session_id not in sessions:
            sessions[session_id] = {}
            print(f"[TCP] new session {session_id}")

        sessions[session_id][seq] = payload
        got_bytes = sum(len(x) for x in sessions[session_id].values())
        print(f"[TCP] session={session_id}, seq={seq}, len={payload_len}, total_bytes={got_bytes}")

    for session_id, chunks in sessions.items():
        if not chunks:
            continue

        raw_u8 = b"".join(chunks[i] for i in sorted(chunks.keys()))
        print(f"[PROC] session={session_id}, raw_u8_bytes={len(raw_u8)}, chunks={len(chunks)}")

        wav_bytes = raw_u8_to_pcm16_wav(raw_u8, AUDIO_SAMPLE_RATE)
        input_wav_path = VOICE_DIR / f"session-{session_id}.wav"
        save_bytes(input_wav_path, wav_bytes)
        print(f"[PROC] saved input wav: {input_wav_path}")

        try:
            asr_text = asr_once_wav(wav_bytes, AUDIO_SAMPLE_RATE)
            print("[PROC] ASR text:", asr_text)
            if not asr_text:
                print("[PROC] ASR empty, skip agent/tts")
                continue

            reply_text = call_ra4m2_agent(asr_text)
            print("[PROC] Agent reply:", reply_text)

            answer_wav = tts_wav(reply_text, AUDIO_SAMPLE_RATE)
            answer_wav_path = ANSWER_DIR / f"answer-{session_id}.wav"
            save_bytes(answer_wav_path, answer_wav)
            print(f"[PROC] saved answer wav: {answer_wav_path}")

            aliyun_iot_publish(asr_text, reply_text, answer_wav, session_id)

        except Exception as e:
            print("[ERROR] process failed:", repr(e))


def main() -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("RA4M2 Voice TCP -> ASR -> AgentRun -> TTS -> Aliyun IoT")
    print(f"TCP listen: {TCP_HOST}:{TCP_PORT}")
    print(f"NLS gateway: {NLS_GATEWAY}")
    print(f"Agent URL: {AGENT_URL}")
    print(f"Aliyun IoT enable: {ALIYUN_IOT_ENABLE}")
    print("=" * 80)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((TCP_HOST, TCP_PORT))
        srv.listen(1)

        while True:
            conn, addr = srv.accept()
            with conn:
                handle_one_connection(conn, addr)


if __name__ == "__main__":
    main()
