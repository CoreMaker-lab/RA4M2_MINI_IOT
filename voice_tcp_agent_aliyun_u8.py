# -*- coding: utf-8 -*-
"""
voice_tcp_agent_aliyun_u8.py

适配当前 RA4M2_MINI_IOT 工程的 PC 端完整语音链路程序。

功能：
1. PC 作为 TCP Server，监听 0.0.0.0:9000
2. 接收 RA4M2 通过 ESP8266 上传的录音分片
3. 按 MCU 端 A55A 包头重组音频
4. 同时生成两个 WAV：
   - session-xxx_u8.wav：8-bit unsigned PCM，尽量保持和 MCU 喇叭播放一致，用于人工试听
   - session-xxx_asr.wav：16-bit PCM WAV，用于阿里云 ASR
5. 调用阿里云 ASR：语音转文字
6. 调用 AgentRun：生成回答文本
7. 调用阿里云 TTS：回答文本转语音 answer-xxx.wav
8. 可选：把 asr_text、reply_text、音频长度等结果上报到阿里云 IoT MQTT

运行：
    python voice_tcp_agent_aliyun_u8.py

依赖：
    python -m pip install requests paho-mqtt

PowerShell 环境变量示例：
    $env:NLS_APPKEY="你的NLS_APPKEY"
    $env:NLS_TOKEN="你的NLS_TOKEN"
    $env:ALIYUN_IOT_ENABLE="0"

如果要开启阿里云 IoT MQTT 上报：
    $env:ALIYUN_IOT_ENABLE="1"
    $env:ALIYUN_MQTT_HOST="xxx.iot-as-mqtt.cn-shanghai.aliyuncs.com"
    $env:ALIYUN_PRODUCT_KEY="xxx"
    $env:ALIYUN_DEVICE_NAME="xxx"
    $env:ALIYUN_MQTT_CLIENT_ID="xxx|securemode=2,signmethod=hmacsha256,timestamp=xxx|"
    $env:ALIYUN_MQTT_USERNAME="DeviceName&ProductKey"
    $env:ALIYUN_MQTT_PASSWORD="你的password签名"

注意：
- 不要把 NLS_TOKEN、MQTT_PASSWORD 等密钥上传到 GitHub。
- 如果 PC 和 ESP8266 使用同一个阿里云 IoT 设备三元组同时登录，可能会互相踢下线。
  建议在阿里云 IoT 里给 PC 端单独创建一个设备。
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
# 1. 配置区
# =========================================================

# TCP 接收配置：对应 MCU 端 Audio_Send_To_PC_TCP("PC_IP", 9000, session)
TCP_HOST = os.environ.get("TCP_HOST", "0.0.0.0")
TCP_PORT = int(os.environ.get("TCP_PORT", "9000"))

# 当前 MCU 工程按 8kHz / 5s / 40000 点设计
AUDIO_SAMPLE_RATE = int(os.environ.get("AUDIO_SAMPLE_RATE", "8000"))

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
# 3. WAV 合成函数
# =========================================================

def raw_u8_to_u8_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    """
    试听用 WAV：
    直接把 MCU 上传的 buzzer_num[] 保存为 8-bit unsigned PCM WAV。

    这个版本不做滤波、不做增益、不做 16bit 转换，
    目的是最大程度接近 MCU 端 DAC 播放效果。
    """
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)       # 8-bit unsigned PCM
        wf.setframerate(sample_rate)
        wf.writeframes(raw_u8)
    return out.getvalue()


def raw_u8_to_s16_wav_for_asr(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    """
    ASR 用 WAV：
    把 8-bit unsigned PCM 转成 16-bit signed PCM。

    注意：这里故意不做高通滤波、不做自动增益，
    避免出现“变声器”效果。
    """
    pcm16 = bytearray()

    for b in raw_u8:
        # unsigned 8bit: 0~255, 中心 128
        # signed 16bit: -32768~32512
        x = (int(b) - 128) << 8
        if x > 32767:
            x = 32767
        elif x < -32768:
            x = -32768
        pcm16 += struct.pack("<h", x)

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)       # 16-bit signed PCM
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(pcm16))
    return out.getvalue()


def save_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def print_audio_stats(raw_u8: bytes) -> None:
    if not raw_u8:
        print("[AUDIO] empty")
        return

    values = list(raw_u8)
    v_min = min(values)
    v_max = max(values)
    v_mean = sum(values) / len(values)
    print(f"[AUDIO] samples={len(values)}, min={v_min}, max={v_max}, mean={v_mean:.2f}")

    if v_max - v_min < 10:
        print("[WARN] 音频动态范围很小，可能麦克风信号太小或 ADC 采样不正常。")

    if v_mean < 60 or v_mean > 200:
        print("[WARN] 音频直流偏置偏离 128 较多，建议检查麦克风偏置电路。")


# =========================================================
# 4. 阿里云 ASR / AgentRun / TTS
# =========================================================

def check_nls_config() -> None:
    if not NLS_APPKEY or not NLS_TOKEN:
        raise RuntimeError(
            "NLS_APPKEY 或 NLS_TOKEN 为空。请先在 PowerShell 设置：\n"
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
                "content": (
                    "你是 RA4M2 智能语音助手。"
                    "请用简洁、自然的中文回答，适合语音播报。"
                    "如果用户问天气、温度、常识问题，就直接回答。"
                )
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
# 5. 阿里云 IoT MQTT 上报
# =========================================================

def aliyun_iot_publish(
    session_id: int,
    asr_text: str,
    reply_text: str,
    answer_wav: bytes,
    input_wav_u8: bytes,
) -> None:
    """
    上报到阿里云 IoT。

    默认只上报文本和音频长度，不直接上报整段音频。
    因为 answer_wav 转 base64 后比较大，MQTT payload 容易超限。
    """
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
            "input_audio_len": len(input_wav_u8),
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
# 6. TCP 接收
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

    # MCU 端发完会 close TCP，所以连接关闭后开始处理 session
    for session_id, chunks in sessions.items():
        process_session(session_id, chunks)


# =========================================================
# 7. Session 处理主流程
# =========================================================

def process_session(session_id: int, chunks: Dict[int, bytes]) -> None:
    if not chunks:
        print(f"[PROC] session={session_id} empty")
        return

    # 按 seq 排序拼接
    seq_list = sorted(chunks.keys())
    expected_seq = list(range(seq_list[0], seq_list[-1] + 1))
    missing = [i for i in expected_seq if i not in chunks]

    if missing:
        print(f"[WARN] session={session_id} missing seq:", missing[:20], "..." if len(missing) > 20 else "")

    raw_u8 = b"".join(chunks[i] for i in seq_list)

    print(f"[PROC] session={session_id}, raw_u8_bytes={len(raw_u8)}, chunks={len(chunks)}")
    print_audio_stats(raw_u8)

    # 额外保存原始 bin，方便后续分析
    raw_path = VOICE_DIR / f"session-{session_id}.raw"
    save_bytes(raw_path, raw_u8)
    print(f"[PROC] saved raw: {raw_path}")

    # 1. 试听用：8-bit unsigned WAV，最接近 MCU DAC 播放
    wav_u8 = raw_u8_to_u8_wav(raw_u8, AUDIO_SAMPLE_RATE)
    wav_u8_path = VOICE_DIR / f"session-{session_id}_u8.wav"
    save_bytes(wav_u8_path, wav_u8)
    print(f"[PROC] saved listen wav: {wav_u8_path}")

    # 2. ASR 用：16-bit PCM WAV
    wav_asr = raw_u8_to_s16_wav_for_asr(raw_u8, AUDIO_SAMPLE_RATE)
    wav_asr_path = VOICE_DIR / f"session-{session_id}_asr.wav"
    save_bytes(wav_asr_path, wav_asr)
    print(f"[PROC] saved asr wav: {wav_asr_path}")

    try:
        # 3. ASR
        asr_text = asr_once_wav(wav_asr, AUDIO_SAMPLE_RATE)
        print("[PROC] ASR text:", asr_text)

        if not asr_text:
            print("[PROC] ASR empty, skip Agent/TTS")
            return

        # 4. AgentRun
        reply_text = call_ra4m2_agent(asr_text)
        print("[PROC] Agent reply:", reply_text)

        # 5. TTS
        answer_wav = tts_wav(reply_text, AUDIO_SAMPLE_RATE)
        answer_path = ANSWER_DIR / f"answer-{session_id}.wav"
        save_bytes(answer_path, answer_wav)
        print(f"[PROC] saved answer wav: {answer_path}")

        # 6. 保存文本结果
        result = {
            "session_id": int(session_id),
            "asr_text": asr_text,
            "reply_text": reply_text,
            "input_wav_u8": str(wav_u8_path),
            "input_wav_asr": str(wav_asr_path),
            "answer_wav": str(answer_path),
            "created_at": int(time.time()),
        }

        result_path = ANSWER_DIR / f"result-{session_id}.json"
        save_bytes(result_path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        print(f"[PROC] saved result json: {result_path}")

        # 7. 上报阿里云 IoT
        aliyun_iot_publish(
            session_id=session_id,
            asr_text=asr_text,
            reply_text=reply_text,
            answer_wav=answer_wav,
            input_wav_u8=wav_u8,
        )

    except Exception as e:
        print("[ERROR] process failed:", repr(e))


# =========================================================
# 8. 主函数
# =========================================================

def main() -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("RA4M2 Voice TCP -> WAV(u8/asr) -> ASR -> AgentRun -> TTS -> Aliyun IoT")
    print(f"TCP listen      : {TCP_HOST}:{TCP_PORT}")
    print(f"Sample rate     : {AUDIO_SAMPLE_RATE}")
    print(f"Voice dir       : {VOICE_DIR}")
    print(f"Answer dir      : {ANSWER_DIR}")
    print(f"NLS gateway     : {NLS_GATEWAY}")
    print(f"Agent URL       : {AGENT_URL}")
    print(f"Aliyun IoT      : {'enable' if ALIYUN_IOT_ENABLE else 'disable'}")
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
