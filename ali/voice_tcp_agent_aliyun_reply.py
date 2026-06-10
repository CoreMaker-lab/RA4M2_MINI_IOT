# -*- coding: utf-8 -*-
"""
voice_tcp_agent_aliyun_reply.py

RA4M2 语音闭环版 PC 程序：
1. 接收 RA4M2 + ESP8266 上传的录音 TCP 包，默认端口 9000
2. 生成 input.wav
3. 阿里云 ASR -> AgentRun -> 阿里云 TTS
4. 保存 answer.wav
5. 把 answer.wav 转成 MCU 可直接 DAC 播放的 uint8_t 原始音频
6. 等待板子连接 PC:9001，把回答音频按 A55B 包头发回板子
7. 板子收到后把数据写入 buzzer_num[]，再用原来的 DAC 播放逻辑播放

运行：
    python voice_tcp_agent_aliyun_reply.py

PowerShell：
    cd H:\github\RA4M2_MINI_IOT
    $env:NLS_APPKEY="你的NLS_APPKEY"
    $env:NLS_TOKEN="你的NLS_TOKEN"
    $env:AUDIO_SAMPLE_RATE="8000"
    python voice_tcp_agent_aliyun_reply.py

说明：
- 发送给板子的回答音频固定为 40000 bytes，和 MCU 端 buzzer_num[AUDIO_SAMPLE_COUNT] 匹配。
- 如果 TTS 音频超过 40000 点，会截断；不足会用 128 静音填充。
- 回传端口默认 9001，MCU 端需要连接 PC_IP:9001 接收。
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


# =========================================================
# 1. 配置
# =========================================================

TCP_HOST = os.environ.get("TCP_HOST", "0.0.0.0")
TCP_PORT = int(os.environ.get("TCP_PORT", "9000"))

REPLY_TCP_HOST = os.environ.get("REPLY_TCP_HOST", "0.0.0.0")
REPLY_TCP_PORT = int(os.environ.get("REPLY_TCP_PORT", "9001"))

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

# 录音上行包头
MAGIC_UPLOAD = 0xA55A
# 回答下行包头
MAGIC_REPLY = 0xA55B

CHUNK = 512
HEADER_FMT = "<HHHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


# =========================================================
# 2. HTTP Session
# =========================================================

def make_requests_session() -> requests.Session:
    s = requests.Session()
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
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


HTTP = make_requests_session()


# =========================================================
# 3. 文件与音频转换
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


def raw_u8_to_input_wav(raw_u8: bytes, sample_rate: int = 8000) -> bytes:
    """
    MCU 上传的 buzzer_num[] -> 16bit WAV，给 ASR 使用。
    """
    vals = [float(b) for b in raw_u8]
    if not vals:
        raise ValueError("empty audio")

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
    """
    阿里云 TTS 返回的 WAV -> MCU 可直接播放的 uint8_t buzzer_num[] 格式。

    输出：
    - 长度固定 target_samples
    - unsigned 8bit，静音中心 128
    - MCU 端可直接：R_DAC_Write(..., buzzer_num[i] * 16)
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        fr = wf.getframerate()
        nframes = wf.getnframes()
        frames = wf.readframes(nframes)

    print(f"[REPLY-AUDIO] wav channels={nch}, sampwidth={sw}, rate={fr}, frames={nframes}")

    samples_u8 = bytearray()

    if sw == 1:
        # WAV 8bit 本身就是 unsigned PCM
        # 多声道时取第一个声道
        if nch == 1:
            samples_u8.extend(frames)
        else:
            samples_u8.extend(frames[0::nch])

    elif sw == 2:
        total_samples = len(frames) // 2
        vals = struct.unpack("<%dh" % total_samples, frames)

        # 多声道时取第一个声道
        if nch > 1:
            vals = vals[0::nch]

        for s in vals:
            # signed 16bit -> unsigned 8bit
            b = (int(s) >> 8) + 128
            if b < 0:
                b = 0
            elif b > 255:
                b = 255
            samples_u8.append(b)
    else:
        raise ValueError(f"unsupported WAV sampwidth: {sw}")

    # 如果 TTS 不是 8k，简单按比例重采样到 AUDIO_SAMPLE_RATE
    # 正常情况下，TTS 请求就是 8k，这里只是兜底。
    if fr != AUDIO_SAMPLE_RATE and len(samples_u8) > 1:
        print(f"[WARN] TTS sample_rate={fr}, resample to {AUDIO_SAMPLE_RATE}")
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
                v = src[j] * (1 - t) + src[j + 1] * t
            else:
                v = src[j]
            dst.append(int(round(v)))
        samples_u8 = dst

    # 截断或补静音，确保 MCU 端固定 40000 点播放
    if len(samples_u8) > target_samples:
        print(f"[REPLY-AUDIO] truncate {len(samples_u8)} -> {target_samples}")
        samples_u8 = samples_u8[:target_samples]
    elif len(samples_u8) < target_samples:
        print(f"[REPLY-AUDIO] pad {len(samples_u8)} -> {target_samples}")
        samples_u8.extend([128] * (target_samples - len(samples_u8)))

    return bytes(samples_u8)


# =========================================================
# 4. ASR / Agent / TTS
# =========================================================

def check_nls_config() -> None:
    if not NLS_APPKEY or not NLS_TOKEN:
        raise RuntimeError(
            "NLS_APPKEY 或 NLS_TOKEN 为空。请设置：\n"
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


# =========================================================
# 5. TCP 接收上行音频
# =========================================================

def recv_exact(conn: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def receive_one_upload(conn: socket.socket, addr: Tuple[str, int]) -> Tuple[int, bytes]:
    print(f"\n[TCP-UP] client connected: {addr}")

    sessions: Dict[int, Dict[int, bytes]] = {}

    while True:
        hdr = recv_exact(conn, HEADER_SIZE)
        if hdr is None:
            print("[TCP-UP] client closed")
            break

        magic, session_id, seq, payload_len = struct.unpack(HEADER_FMT, hdr)

        if magic != MAGIC_UPLOAD:
            print(f"[TCP-UP] bad magic: 0x{magic:04X}")
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
        raise RuntimeError("no upload session received")

    # 一次只处理一个 session
    session_id = sorted(sessions.keys())[0]
    chunks = sessions[session_id]
    raw = b"".join(chunks[i] for i in sorted(chunks.keys()))

    print(f"[TCP-UP] complete session={session_id}, raw_bytes={len(raw)}, chunks={len(chunks)}")
    return session_id, raw


# =========================================================
# 6. TCP 回传回答音频
# =========================================================

def send_reply_to_board(session_id: int, raw_u8: bytes) -> None:
    """
    等待板子连接 PC:9001，然后按 A55B 包头发送回答音频。
    """
    print("=" * 80)
    print(f"[TCP-DOWN] waiting board connect on {REPLY_TCP_HOST}:{REPLY_TCP_PORT}")
    print("[TCP-DOWN] board should call Audio_Receive_Answer_From_PC_TCP(...) now")
    print("=" * 80)

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
                time.sleep(0.002)

            print(f"[TCP-DOWN] sent reply audio: bytes={len(raw_u8)}, packets={seq}")


# =========================================================
# 7. 主处理
# =========================================================

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

    asr_text = asr_once_wav(input_wav, AUDIO_SAMPLE_RATE)
    print("[PROC] ASR text:", asr_text)

    reply_text = call_ra4m2_agent(asr_text)
    print("[PROC] Agent reply:", reply_text)

    answer_wav = tts_wav(reply_text, AUDIO_SAMPLE_RATE)
    answer_wav_path = ANSWER_DIR / f"answer-{session_id}.wav"
    save_bytes(answer_wav_path, answer_wav)
    print(f"[PROC] saved answer wav: {answer_wav_path}")

    answer_raw_u8 = wav_to_mcu_u8(answer_wav, AUDIO_SAMPLE_COUNT)
    answer_raw_path = ANSWER_DIR / f"answer-{session_id}.raw"
    save_bytes(answer_raw_path, answer_raw_u8)
    print(f"[PROC] saved answer raw u8: {answer_raw_path}")

    result = {
        "session_id": int(session_id),
        "asr_text": asr_text,
        "reply_text": reply_text,
        "input_wav": str(input_wav_path),
        "answer_wav": str(answer_wav_path),
        "answer_raw": str(answer_raw_path),
        "reply_port": REPLY_TCP_PORT,
        "created_at": int(time.time()),
    }

    result_path = ANSWER_DIR / f"result-{session_id}.json"
    save_bytes(result_path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"[PROC] saved result json: {result_path}")

    # 回传给板子
    send_reply_to_board(session_id, answer_raw_u8)


def main() -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("RA4M2 Voice Closed Loop: Upload -> ASR -> Agent -> TTS -> Download")
    print(f"Upload TCP listen : {TCP_HOST}:{TCP_PORT}")
    print(f"Reply  TCP listen : {REPLY_TCP_HOST}:{REPLY_TCP_PORT}")
    print(f"Sample rate       : {AUDIO_SAMPLE_RATE}")
    print(f"Sample count      : {AUDIO_SAMPLE_COUNT}")
    print(f"Agent URL         : {AGENT_URL}")
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
                session_id, raw = receive_one_upload(conn, addr)

        try:
            process_upload(session_id, raw)
        except Exception as e:
            print("[ERROR] process failed:", repr(e))

        print("[MAIN] wait next recording ...")


if __name__ == "__main__":
    main()
