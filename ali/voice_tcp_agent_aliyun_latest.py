# -*- coding: utf-8 -*-
"""
voice_tcp_agent_aliyun_latest.py

RA4M2_MINI_IOT 最新稳定版 PC 端程序

功能：
1. PC 作为 TCP Server，监听 0.0.0.0:9000
2. 接收 RA4M2 + ESP8266 通过 TCP 上传的录音分片
3. 按 A55A 包头重组音频
4. 默认按 8kHz 生成 WAV
5. 自动判断上传音频格式：
   - 40000 bytes 左右：按 uint8_t 音频处理
   - 80000 bytes 左右：按 uint16_t ADC 原始音频处理
6. 保存：
   - voice_sessions/session-x.raw 或 session-x.raw16
   - voice_sessions/session-x_input.wav
   - answer_sessions/answer-x.wav
   - answer_sessions/result-x.json
7. 调用阿里云 ASR：语音 -> 文本
8. 调用 AgentRun：文本 -> 回复文本
9. 调用阿里云 TTS：回复文本 -> answer.wav
10. 可选上报阿里云 IoT MQTT：asr_text、reply_text、音频长度等

运行：
    python voice_tcp_agent_aliyun_latest.py

依赖：
    python -m pip install requests paho-mqtt

PowerShell 示例：
    cd H:\github\RA4M2_MINI_IOT
    $env:NLS_APPKEY="你的NLS_APPKEY"
    $env:NLS_TOKEN="你的NLS_TOKEN"
    $env:ALIYUN_IOT_ENABLE="0"
    $env:AUDIO_SAMPLE_RATE="8000"
    python voice_tcp_agent_aliyun_latest.py

如果暂时只想生成 WAV，不调用 ASR / Agent / TTS：
    $env:RUN_CLOUD_PIPELINE="0"
    python voice_tcp_agent_aliyun_latest.py

注意：
- 不要把 NLS_TOKEN、MQTT_PASSWORD 上传到 GitHub。
- PC 和 ESP8266 不建议使用同一个阿里云 IoT 设备三元组同时登录，可能互相踢下线。
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None


# =========================================================
# 1. 基本配置
# =========================================================

TCP_HOST = os.environ.get("TCP_HOST", "0.0.0.0")
TCP_PORT = int(os.environ.get("TCP_PORT", "9000"))

# 最新统一沿用 8kHz
AUDIO_SAMPLE_RATE = int(os.environ.get("AUDIO_SAMPLE_RATE", "8000"))

# 云端链路开关
RUN_CLOUD_PIPELINE = os.environ.get("RUN_CLOUD_PIPELINE", "1") == "1"

VOICE_DIR = Path(os.environ.get("VOICE_DIR", "voice_sessions"))
ANSWER_DIR = Path(os.environ.get("ANSWER_DIR", "answer_sessions"))

# AgentRun OpenAI 兼容接口
AGENT_URL = os.environ.get(
    "AGENT_URL",
    "https://1930052576475971.agentrun-data.cn-hangzhou.aliyuncs.com/"
    "agent-runtimes/RA4M2_1/endpoints/Default/invocations/"
    "openai/v1/chat/completions"
)

# 阿里云 NLS
NLS_REGION = os.environ.get("NLS_REGION", "cn-shanghai")
NLS_GATEWAY = f"https://nls-gateway-{NLS_REGION}.aliyuncs.com"
NLS_APPKEY = os.environ.get("NLS_APPKEY", "")
NLS_TOKEN = os.environ.get("NLS_TOKEN", "")
NLS_VOICE = os.environ.get("NLS_VOICE", "xiaoyun")

# 阿里云 IoT MQTT 上报
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

# 音频格式自动判断，也可以强制指定：auto / u8 / u16
AUDIO_RAW_FORMAT = os.environ.get("AUDIO_RAW_FORMAT", "auto").lower().strip()


# =========================================================
# 2. MCU TCP 包格式
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


def make_requests_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


HTTP = make_requests_session()


# =========================================================
# 4. 音频处理
# =========================================================

def detect_audio_format(raw: bytes) -> str:
    if AUDIO_RAW_FORMAT in ("u8", "uint8", "raw8"):
        return "u8"

    if AUDIO_RAW_FORMAT in ("u16", "uint16", "raw16"):
        return "u16"

    # auto
    if len(raw) >= 70000 and len(raw) % 2 == 0:
        return "u16"

    return "u8"


def pcm16_from_u8(raw_u8: bytes) -> Tuple[bytes, Dict[str, float]]:
    if not raw_u8:
        raise ValueError("empty raw_u8")

    vals = [float(b) for b in raw_u8]

    v_min = min(vals)
    v_max = max(vals)
    v_mean = sum(vals) / len(vals)
    dynamic = v_max - v_min

    print(
        f"[AUDIO-U8] samples={len(vals)}, "
        f"min={v_min:.1f}, max={v_max:.1f}, "
        f"mean={v_mean:.2f}, dynamic={dynamic:.1f}"
    )

    x = [v - v_mean for v in vals]

    # 轻微三点平滑，降低 8bit 阶梯感，不改变采样率
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

    gain = 24000.0 / peak

    pcm16 = bytearray()
    for v in y:
        pcm16 += struct.pack("<h", clamp_s16(v * gain))

    stats = {
        "format": "u8",
        "samples": len(vals),
        "min": float(v_min),
        "max": float(v_max),
        "mean": float(v_mean),
        "dynamic": float(dynamic),
    }

    return bytes(pcm16), stats


def pcm16_from_u16(raw_u16_bytes: bytes) -> Tuple[bytes, Dict[str, float]]:
    if len(raw_u16_bytes) < 2:
        raise ValueError("raw_u16_bytes too short")

    if len(raw_u16_bytes) % 2 != 0:
        print("[WARN] raw_u16 length is odd, drop last byte")
        raw_u16_bytes = raw_u16_bytes[:-1]

    sample_count = len(raw_u16_bytes) // 2
    vals = list(struct.unpack("<%dH" % sample_count, raw_u16_bytes))

    v_min = min(vals)
    v_max = max(vals)
    v_mean = sum(vals) / len(vals)
    dynamic = v_max - v_min

    print(
        f"[AUDIO-U16] samples={sample_count}, "
        f"min={v_min}, max={v_max}, "
        f"mean={v_mean:.2f}, dynamic={dynamic}"
    )

    x = [float(v) - v_mean for v in vals]

    # 轻微三点平滑
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

    stats = {
        "format": "u16",
        "samples": int(sample_count),
        "min": float(v_min),
        "max": float(v_max),
        "mean": float(v_mean),
        "dynamic": float(dynamic),
    }

    return bytes(pcm16), stats


def write_wav_from_pcm16(pcm16: bytes, sample_rate: int) -> bytes:
    out = io.BytesIO()

    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)

    return out.getvalue()


def build_input_wav(raw: bytes, sample_rate: int) -> Tuple[bytes, Dict[str, float]]:
    fmt = detect_audio_format(raw)
    print(f"[AUDIO] detected format: {fmt}, raw_bytes={len(raw)}")

    if fmt == "u16":
        pcm16, stats = pcm16_from_u16(raw)
    else:
        pcm16, stats = pcm16_from_u8(raw)

    wav_bytes = write_wav_from_pcm16(pcm16, sample_rate)
    return wav_bytes, stats


# =========================================================
# 5. 阿里云 ASR / AgentRun / TTS
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

    resp = HTTP.post(
        url,
        params=params,
        headers=headers,
        data=wav_bytes,
        timeout=30,
    )

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


# =========================================================
# 6. 阿里云 IoT MQTT 上报
# =========================================================

def aliyun_iot_publish(
    session_id: int,
    asr_text: str,
    reply_text: str,
    answer_wav: bytes,
    audio_stats: Dict[str, float],
) -> None:
    if not ALIYUN_IOT_ENABLE:
        print("[IoT] disabled")
        return

    if mqtt is None:
        print("[IoT] paho-mqtt 未安装，跳过上报。")
        return

    required = {
        "ALIYUN_MQTT_HOST": ALIYUN_MQTT_HOST,
        "ALIYUN_MQTT_CLIENT_ID": ALIYUN_MQTT_CLIENT_ID,
        "ALIYUN_MQTT_USERNAME": ALIYUN_MQTT_USERNAME,
        "ALIYUN_MQTT_PASSWORD": ALIYUN_MQTT_PASSWORD,
        "ALIYUN_PUB_TOPIC": ALIYUN_PUB_TOPIC,
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        print("[IoT] 配置不完整，跳过上报。缺少:", ", ".join(missing))
        return

    payload = {
        "id": str(int(time.time() * 1000)),
        "version": "1.0",
        "method": "thing.event.property.post",
        "params": {
            "session_id": int(session_id),
            "asr_text": asr_text,
            "reply_text": reply_text,
            "audio_format": audio_stats.get("format", ""),
            "audio_samples": int(audio_stats.get("samples", 0)),
            "audio_min": audio_stats.get("min", 0),
            "audio_max": audio_stats.get("max", 0),
            "audio_mean": audio_stats.get("mean", 0),
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
# 7. TCP 接收
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
        print(
            f"[TCP] session={session_id}, seq={seq}, "
            f"payload_len={payload_len}, total_bytes={got_bytes}"
        )

    for session_id, chunks in sessions.items():
        process_session(session_id, chunks)


# =========================================================
# 8. Session 处理主流程
# =========================================================

def process_session(session_id: int, chunks: Dict[int, bytes]) -> None:
    if not chunks:
        print(f"[PROC] session={session_id} empty")
        return

    seq_list = sorted(chunks.keys())

    missing = [i for i in range(seq_list[0], seq_list[-1] + 1) if i not in chunks]
    if missing:
        print("[WARN] missing seq:", missing[:30], "..." if len(missing) > 30 else "")

    raw = b"".join(chunks[i] for i in seq_list)

    print(f"[PROC] session={session_id}, raw_bytes={len(raw)}, chunks={len(chunks)}")

    # 保存 raw
    raw_suffix = "raw16" if detect_audio_format(raw) == "u16" else "raw"
    raw_path = VOICE_DIR / f"session-{session_id}.{raw_suffix}"
    save_bytes(raw_path, raw)
    print(f"[PROC] saved raw: {raw_path}")

    # 生成输入 WAV
    wav_bytes, audio_stats = build_input_wav(raw, AUDIO_SAMPLE_RATE)
    input_wav_path = VOICE_DIR / f"session-{session_id}_input.wav"
    save_bytes(input_wav_path, wav_bytes)
    print(f"[PROC] saved input wav: {input_wav_path}")

    result = {
        "session_id": int(session_id),
        "raw_path": str(raw_path),
        "input_wav": str(input_wav_path),
        "sample_rate": AUDIO_SAMPLE_RATE,
        "audio_stats": audio_stats,
        "created_at": int(time.time()),
    }

    if not RUN_CLOUD_PIPELINE:
        result_path = ANSWER_DIR / f"result-{session_id}.json"
        save_bytes(result_path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        print("[PROC] RUN_CLOUD_PIPELINE=0, skip ASR/Agent/TTS")
        print(f"[PROC] saved result json: {result_path}")
        return

    try:
        # 1. ASR
        asr_text = asr_once_wav(wav_bytes, AUDIO_SAMPLE_RATE)
        print("[PROC] ASR text:", asr_text)

        result["asr_text"] = asr_text

        if not asr_text:
            print("[PROC] ASR empty, skip Agent/TTS")
            result_path = ANSWER_DIR / f"result-{session_id}.json"
            save_bytes(result_path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
            return

        # 2. AgentRun
        reply_text = call_ra4m2_agent(asr_text)
        print("[PROC] Agent reply:", reply_text)

        result["reply_text"] = reply_text

        # 3. TTS
        answer_wav = tts_wav(reply_text, AUDIO_SAMPLE_RATE)
        answer_path = ANSWER_DIR / f"answer-{session_id}.wav"
        save_bytes(answer_path, answer_wav)
        print(f"[PROC] saved answer wav: {answer_path}")

        result["answer_wav"] = str(answer_path)

        # 4. 保存结果
        result_path = ANSWER_DIR / f"result-{session_id}.json"
        save_bytes(result_path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        print(f"[PROC] saved result json: {result_path}")

        # 5. 上报阿里云 IoT
        aliyun_iot_publish(session_id, asr_text, reply_text, answer_wav, audio_stats)

    except Exception as e:
        result["error"] = repr(e)
        result_path = ANSWER_DIR / f"result-{session_id}.json"
        save_bytes(result_path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))

        print("[ERROR] process failed:", repr(e))
        print(f"[PROC] saved error result json: {result_path}")


# =========================================================
# 9. 主函数
# =========================================================

def main() -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("RA4M2 TCP Voice Receiver - Latest 8kHz")
    print(f"TCP listen        : {TCP_HOST}:{TCP_PORT}")
    print(f"Sample rate       : {AUDIO_SAMPLE_RATE}")
    print(f"Raw format        : {AUDIO_RAW_FORMAT}")
    print(f"Cloud pipeline    : {'enable' if RUN_CLOUD_PIPELINE else 'disable'}")
    print(f"Voice dir         : {VOICE_DIR}")
    print(f"Answer dir        : {ANSWER_DIR}")
    print(f"Agent URL         : {AGENT_URL}")
    print(f"NLS gateway       : {NLS_GATEWAY}")
    print(f"IoT upload        : {'enable' if ALIYUN_IOT_ENABLE else 'disable'}")
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
