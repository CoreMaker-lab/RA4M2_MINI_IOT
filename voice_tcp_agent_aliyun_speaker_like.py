# -*- coding: utf-8 -*-
"""
voice_tcp_agent_aliyun_speaker_like.py

RA4M2_MINI_IOT PC 端完整语音链路程序。

适用场景：
- MCU 端录音后本地喇叭播放正常；
- 但 PC 端生成 WAV 听起来像“变声器 / 机器人音”；
- 本程序在 PC 端模拟 DAC + 功放 + 喇叭的平滑效果，生成 speaker_like WAV。

功能：
1. PC 作为 TCP Server，监听 0.0.0.0:9000
2. 接收 RA4M2 通过 ESP8266 TCP 上传的录音分片
3. 按 MCU 端 A55A 包头重组音频
4. 保存多个调试文件：
   - session-x.raw                 原始上传数据
   - session-x_debug_u8.wav         8-bit 标准中心重映射版本
   - session-x_fixed.wav            基础 16-bit PCM 修复版本
   - session-x_speaker_like.wav     推荐试听版本，模拟 MCU 喇叭平滑效果
5. 调用阿里云 ASR：语音转文字
6. 调用 AgentRun：生成回答文本
7. 调用阿里云 TTS：回答文本转语音 answer-x.wav
8. 可选上报阿里云 IoT MQTT：上报 asr_text / reply_text / 音频长度

运行：
    python voice_tcp_agent_aliyun_speaker_like.py

依赖：
    python -m pip install requests paho-mqtt

PowerShell 环境变量示例：
    $env:NLS_APPKEY="你的NLS_APPKEY"
    $env:NLS_TOKEN="你的NLS_TOKEN"
    $env:ALIYUN_IOT_ENABLE="0"
    python voice_tcp_agent_aliyun_speaker_like.py

如需开启阿里云 IoT MQTT 上报：
    $env:ALIYUN_IOT_ENABLE="1"
    $env:ALIYUN_MQTT_HOST="xxx.iot-as-mqtt.cn-shanghai.aliyuncs.com"
    $env:ALIYUN_MQTT_PORT="1883"
    $env:ALIYUN_MQTT_CLIENT_ID="xxx|securemode=2,signmethod=hmacsha256,timestamp=xxx|"
    $env:ALIYUN_MQTT_USERNAME="DeviceName&ProductKey"
    $env:ALIYUN_MQTT_PASSWORD="你的password签名"
    $env:ALIYUN_PRODUCT_KEY="xxx"
    $env:ALIYUN_DEVICE_NAME="xxx"

注意：
- 不要把 NLS_TOKEN / MQTT_PASSWORD 上传到 GitHub。
- 如果 PC 和 ESP8266 使用同一个阿里云 IoT 设备三元组同时登录，可能会互相踢下线。
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
# 1. 配置区
# =========================================================

# TCP 接收配置：对应 MCU 端 Audio_Send_To_PC_TCP("PC_IP", 9000, session)
TCP_HOST = os.environ.get("TCP_HOST", "0.0.0.0")
TCP_PORT = int(os.environ.get("TCP_PORT", "9000"))

# MCU 端当前设计：8kHz / 5s / 40000 点
AUDIO_SAMPLE_RATE = int(os.environ.get("AUDIO_SAMPLE_RATE", "8000"))

VOICE_DIR = Path(os.environ.get("VOICE_DIR", "voice_sessions"))
ANSWER_DIR = Path(os.environ.get("ANSWER_DIR", "answer_sessions"))

# speaker_like 输出是否给 ASR 使用：
# 0：ASR 使用 fixed.wav，采样率 8kHz
# 1：ASR 使用 speaker_like.wav，采样率 32kHz
# 如果 ASR 对 32kHz 支持不稳定，可以保持 0。
USE_SPEAKER_LIKE_FOR_ASR = os.environ.get("USE_SPEAKER_LIKE_FOR_ASR", "0") == "1"


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


# 阿里云 IoT MQTT 上报配置
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
# 2. MCU TCP 音频包格式
# =========================================================
# MCU 端结构体：
# typedef struct __attribute__((packed)) {
#     uint16_t magic;    // 0xA55A
#     uint16_t session;  // 会话号
#     uint16_t seq;      // 包序号
#     uint16_t len;      // payload长度
# } audio_pkt_hdr_t;

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


def print_audio_stats(raw_u8: bytes) -> Dict[str, float]:
    if not raw_u8:
        print("[AUDIO] empty")
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "dynamic": 0.0}

    vals = [float(b) for b in raw_u8]
    v_min = min(vals)
    v_max = max(vals)
    v_mean = sum(vals) / len(vals)
    dynamic = v_max - v_min

    print(
        f"[AUDIO] samples={len(vals)}, "
        f"min={v_min:.1f}, max={v_max:.1f}, "
        f"mean={v_mean:.2f}, dynamic={dynamic:.1f}"
    )

    if dynamic < 20:
        print("[WARN] 音频动态范围偏小，PC 端放大会放大量化噪声。")
    if v_mean < 60 or v_mean > 200:
        print("[WARN] 音频中心偏离标准 8-bit WAV 的 128，必须重新映射。")

    return {"min": v_min, "max": v_max, "mean": v_mean, "dynamic": dynamic}


# =========================================================
# 4. WAV 合成函数
# =========================================================

def raw_u8_to_debug_u8_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    """
    调试用 8-bit WAV。
    注意：不是直接写 raw_u8，而是把 raw_u8 以实际均值为中心，重新映射到标准 8-bit PCM。
    标准 8-bit WAV 静音中心必须是 128。
    """
    if not raw_u8:
        return b""

    vals = [float(b) for b in raw_u8]
    mean = sum(vals) / len(vals)
    x = [v - mean for v in vals]

    peak = max(abs(min(x)), abs(max(x)))
    if peak < 1.0:
        peak = 1.0

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


def raw_u8_to_fixed_s16_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    """
    基础修复版 16-bit WAV：
    - 使用实际 mean 作为中心；
    - 3 点轻微平滑；
    - 自动增益；
    - 输出 8kHz/16bit PCM。
    """
    if not raw_u8:
        return b""

    vals = [float(b) for b in raw_u8]
    mean = sum(vals) / len(vals)
    x = [v - mean for v in vals]

    # 3 点轻微平滑，减少 8bit 阶梯噪声
    if len(x) >= 3:
        y = [0.0] * len(x)
        y[0] = x[0]
        y[-1] = x[-1]
        for i in range(1, len(x) - 1):
            y[i] = 0.25 * x[i - 1] + 0.5 * x[i] + 0.25 * x[i + 1]
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


def raw_u8_to_speaker_like_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    """
    推荐试听版本：模拟 MCU DAC + 功放 + 喇叭的平滑效果。

    为什么需要这个：
    - MCU 本地喇叭播放经过 DAC、功放、喇叭、电容等模拟链路，自然会平滑 8bit 阶梯波；
    - PC 直接播放数字 WAV 时没有这层模拟平滑，就容易听起来像“变声器/机器人音”；
    - 这里通过 4 倍线性插值升采样 + 多级低通，模拟这种平滑效果。

    输出：
    - 32kHz / 16bit / mono WAV
    """
    if not raw_u8:
        return b""

    vals = [float(b) for b in raw_u8]

    v_min = min(vals)
    v_max = max(vals)
    v_mean = sum(vals) / len(vals)
    dynamic = v_max - v_min

    print(
        f"[SPEAKER] input min={v_min:.1f}, max={v_max:.1f}, "
        f"mean={v_mean:.2f}, dynamic={dynamic:.1f}"
    )

    # 1. 实际中心去偏置
    x = [v - v_mean for v in vals]

    # 2. 噪声门限：很小的抖动直接归零
    # 如果人声尾音被吃掉，把 1.5 改小到 0.8；
    # 如果底噪明显，把 1.5 改大到 2.0。
    noise_gate = 0.0
    x = [0.0 if abs(v) < noise_gate else v for v in x]

    # 3. 4 倍线性插值升采样：8k -> 32k，减少阶梯感
    up = 4
    y = []

    for i in range(len(x) - 1):
        a = x[i]
        b = x[i + 1]
        for k in range(up):
            t = k / up
            y.append(a * (1.0 - t) + b * t)

    # 补最后一个点，保持结尾
    y.append(x[-1])

    # 4. 多级一阶低通，模拟喇叭/功放平滑
    # alpha 越小越平滑，越大越接近原始数据：
    #   还像变声器/机器人：改小，如 0.12
    #   太闷：改大，如 0.25
    def lowpass(data, alpha=0.16):
        out = []
        last = 0.0
        for v in data:
            last = last + alpha * (v - last)
            out.append(last)
        return out

    y = lowpass(y, 0.08)
    y = lowpass(y, 0.08)
    y = lowpass(y, 0.08)

    # 5. 自动增益
    peak = max(abs(min(y)), abs(max(y)))
    if peak < 1.0:
        peak = 1.0

    # 不要拉太满，否则阶梯/量化噪声又会被放大
    gain = 24000.0 / peak

    pcm16 = bytearray()
    for v in y:
        pcm16 += struct.pack("<h", clamp_s16(v * gain))

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate * up)
        wf.writeframes(bytes(pcm16))

    return out.getvalue()


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


def asr_once_wav(wav_bytes: bytes, sample_rate: int) -> str:
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

    resp = requests.post(
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


# =========================================================
# 6. 阿里云 IoT MQTT 上报
# =========================================================

def aliyun_iot_publish(session_id: int, asr_text: str, reply_text: str, answer_wav: bytes) -> None:
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

    # session_id -> {seq: payload}
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

    raw_u8 = b"".join(chunks[i] for i in seq_list)

    print(f"[PROC] session={session_id}, raw_bytes={len(raw_u8)}, chunks={len(chunks)}")
    stats = print_audio_stats(raw_u8)

    # 保存 raw
    raw_path = VOICE_DIR / f"session-{session_id}.raw"
    save_bytes(raw_path, raw_u8)
    print(f"[PROC] saved raw: {raw_path}")

    # 1. debug u8 wav
    debug_u8_wav = raw_u8_to_debug_u8_wav(raw_u8, AUDIO_SAMPLE_RATE)
    debug_u8_path = VOICE_DIR / f"session-{session_id}_debug_u8.wav"
    save_bytes(debug_u8_path, debug_u8_wav)
    print(f"[PROC] saved debug u8 wav: {debug_u8_path}")

    # 2. fixed 8k 16-bit wav
    fixed_wav = raw_u8_to_fixed_s16_wav(raw_u8, AUDIO_SAMPLE_RATE)
    fixed_path = VOICE_DIR / f"session-{session_id}_fixed.wav"
    save_bytes(fixed_path, fixed_wav)
    print(f"[PROC] saved fixed wav: {fixed_path}")

    # 3. speaker-like 32k 16-bit wav，推荐试听
    speaker_wav = raw_u8_to_speaker_like_wav(raw_u8, AUDIO_SAMPLE_RATE)
    speaker_path = VOICE_DIR / f"session-{session_id}_speaker_like.wav"
    save_bytes(speaker_path, speaker_wav)
    print(f"[PROC] saved speaker-like wav: {speaker_path}")

    # ASR 默认用 fixed 8k，防止 NLS 对 32k wav 支持不稳定
    if USE_SPEAKER_LIKE_FOR_ASR:
        asr_wav = speaker_wav
        asr_sample_rate = AUDIO_SAMPLE_RATE * 4
        asr_source = str(speaker_path)
    else:
        asr_wav = fixed_wav
        asr_sample_rate = AUDIO_SAMPLE_RATE
        asr_source = str(fixed_path)

    try:
        # 4. ASR
        asr_text = asr_once_wav(asr_wav, asr_sample_rate)
        print("[PROC] ASR text:", asr_text)

        if not asr_text:
            print("[PROC] ASR empty, skip Agent/TTS")
            return

        # 5. AgentRun
        reply_text = call_ra4m2_agent(asr_text)
        print("[PROC] Agent reply:", reply_text)

        # 6. TTS
        answer_wav = tts_wav(reply_text, AUDIO_SAMPLE_RATE)
        answer_path = ANSWER_DIR / f"answer-{session_id}.wav"
        save_bytes(answer_path, answer_wav)
        print(f"[PROC] saved answer wav: {answer_path}")

        # 7. 保存结果
        result = {
            "session_id": int(session_id),
            "asr_text": asr_text,
            "reply_text": reply_text,
            "raw": str(raw_path),
            "debug_u8_wav": str(debug_u8_path),
            "fixed_wav": str(fixed_path),
            "speaker_like_wav": str(speaker_path),
            "asr_source": asr_source,
            "answer_wav": str(answer_path),
            "audio_stats": stats,
            "created_at": int(time.time()),
        }

        result_path = ANSWER_DIR / f"result-{session_id}.json"
        save_bytes(result_path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        print(f"[PROC] saved result json: {result_path}")

        # 8. 上报阿里云 IoT
        aliyun_iot_publish(session_id, asr_text, reply_text, answer_wav)

    except Exception as e:
        print("[ERROR] process failed:", repr(e))


# =========================================================
# 9. 主函数
# =========================================================

def main() -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("RA4M2 TCP Voice Receiver - Speaker Like WAV")
    print(f"TCP listen              : {TCP_HOST}:{TCP_PORT}")
    print(f"Sample rate             : {AUDIO_SAMPLE_RATE}")
    print(f"Voice dir               : {VOICE_DIR}")
    print(f"Answer dir              : {ANSWER_DIR}")
    print(f"Agent URL               : {AGENT_URL}")
    print(f"NLS gateway             : {NLS_GATEWAY}")
    print(f"Use speaker-like for ASR: {USE_SPEAKER_LIKE_FOR_ASR}")
    print(f"IoT upload              : {'enable' if ALIYUN_IOT_ENABLE else 'disable'}")
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
