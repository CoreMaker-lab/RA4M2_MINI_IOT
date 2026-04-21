# -*- coding: utf-8 -*-
"""
ra4m2_aliyun_bridge_ascii.py

Flow:
1. Listen for TCP audio packets from RA4M2/ESP8266
2. Rebuild one WAV file from packets
3. Send the WAV to Aliyun ASR
4. Send ASR text to AgentRun
5. Send reply text to Aliyun TTS
6. Save:
   - session_x.wav
   - up.wav
   - answer.wav
   - result.json
"""

import os
import io
import json
import wave
import base64
import array
import socket
import struct
import requests


HOST = "0.0.0.0"
PORT = 9000
MAGIC = 0xA55A
SR = 8000

HTTP_TIMEOUT_ASR = 30
HTTP_TIMEOUT_AGENT = 30
HTTP_TIMEOUT_TTS = 30


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required.")
    return value


def build_config() -> dict:
    nls_region = os.environ.get("NLS_REGION", "cn-shanghai").strip() or "cn-shanghai"
    return {
        "AGENT_URL": require_env("AGENT_URL"),
        "NLS_APPKEY": require_env("NLS_APPKEY"),
        "NLS_TOKEN": require_env("NLS_TOKEN"),
        "NLS_VOICE": os.environ.get("NLS_VOICE", "xiaoyun").strip() or "xiaoyun",
        "NLS_REGION": nls_region,
        "NLS_GATEWAY": f"https://nls-gateway-{nls_region}.aliyuncs.com",
    }


def safe_json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def write_wav(path: str, pcm_u8: bytes, sample_rate: int = SR):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(sample_rate)
        w.writeframes(pcm_u8)


def wav_u8_to_pcm16_wav(wav_bytes: bytes, out_sample_rate: int = 8000) -> bytes:
    in_buf = io.BytesIO(wav_bytes)

    with wave.open(in_buf, "rb") as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes = wf.getnframes()
        frames = wf.readframes(nframes)

    if nchannels != 1:
        raise ValueError(f"only mono wav is supported, got {nchannels} channels")
    if framerate != out_sample_rate:
        raise ValueError(f"sample rate must be {out_sample_rate}, got {framerate}")

    if sampwidth == 2:
        return wav_bytes

    if sampwidth != 1:
        raise ValueError(f"unsupported sample width: {sampwidth} bytes")

    pcm16 = array.array("h")
    for b in frames:
        pcm16.append((b - 128) << 8)

    out_buf = io.BytesIO()
    with wave.open(out_buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(out_sample_rate)
        wf.writeframes(pcm16.tobytes())

    return out_buf.getvalue()


def asr_once_wav(audio_bytes: bytes, cfg: dict, sample_rate: int = 8000) -> str:
    url = f"{cfg['NLS_GATEWAY']}/stream/v1/asr"
    params = {
        "appkey": cfg["NLS_APPKEY"],
        "format": "wav",
        "sample_rate": str(sample_rate),
        "enable_punctuation_prediction": "true",
        "enable_inverse_text_normalization": "true",
    }
    headers = {
        "X-NLS-Token": cfg["NLS_TOKEN"],
        "Content-Type": "application/octet-stream",
    }

    resp = requests.post(
        url,
        params=params,
        headers=headers,
        data=audio_bytes,
        timeout=HTTP_TIMEOUT_ASR,
    )

    print("ASR status:", resp.status_code)
    print("ASR raw text:", resp.text)
    resp.raise_for_status()

    data = resp.json()
    print("ASR response:")
    print(safe_json_dumps(data))
    return data.get("result", "").strip()


def call_ra4m2_agent(user_text: str, cfg: dict) -> str:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "You are a voice assistant. Reply naturally and briefly in Chinese. "
                    "If it is a normal question, answer normally. "
                    "If it is related to device control, answer naturally as well. "
                    f"\nUser text: {user_text}"
                )
            }
        ],
        "stream": False
    }

    resp = requests.post(
        cfg["AGENT_URL"],
        headers={"content-type": "application/json"},
        json=payload,
        timeout=HTTP_TIMEOUT_AGENT
    )

    print("Agent status:", resp.status_code)
    print("Agent raw text:", resp.text)
    resp.raise_for_status()

    data = resp.json()
    print("Agent response:")
    print(safe_json_dumps(data))

    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        raise RuntimeError(f"Agent response format unexpected: {e}")


def tts_wav(text: str, cfg: dict, sample_rate: int = 8000) -> bytes:
    url = f"{cfg['NLS_GATEWAY']}/stream/v1/tts"
    params = {
        "appkey": cfg["NLS_APPKEY"],
        "token": cfg["NLS_TOKEN"],
        "format": "wav",
        "sample_rate": str(sample_rate),
        "voice": cfg["NLS_VOICE"],
        "text": text,
    }

    resp = requests.post(
        url,
        params=params,
        timeout=HTTP_TIMEOUT_TTS
    )

    print("TTS status:", resp.status_code)
    if resp.status_code != 200:
        print("TTS raw text:", resp.text)
        resp.raise_for_status()

    return resp.content


def process_wav_file(wav_path: str, cfg: dict):
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()

    wav16 = wav_u8_to_pcm16_wav(wav_bytes, out_sample_rate=8000)

    asr_text = asr_once_wav(wav16, cfg, sample_rate=8000)
    if not asr_text:
        raise RuntimeError("ASR empty result")

    reply_text = call_ra4m2_agent(asr_text, cfg)

    answer_audio = tts_wav(reply_text, cfg, sample_rate=8000)
    with open("answer.wav", "wb") as f:
        f.write(answer_audio)

    result = {
        "asr_text": asr_text,
        "reply_text": reply_text,
        "audio_b64": base64.b64encode(answer_audio).decode("ascii"),
    }
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return result


def receive_one_session():
    sessions = {}

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(1)
        print("TCP server listening on", PORT)

        conn, addr = s.accept()
        print("Connected from", addr)

        try:
            with conn:
                while True:
                    hdr = recv_exact(conn, 8)
                    magic, session, seq, ln = struct.unpack("<HHHH", hdr)
                    if magic != MAGIC:
                        raise ValueError(f"bad magic: {hex(magic)}")

                    payload = recv_exact(conn, ln)
                    sessions.setdefault(session, {})[seq] = payload
                    print(f"session={session}, seq={seq}, len={ln}")
        except Exception as e:
            print("Receive finished:", e)

    if not sessions:
        raise RuntimeError("No audio received")

    latest_session = sorted(sessions.keys())[-1]
    pkts = sessions[latest_session]
    seqs = sorted(pkts.keys())

    missing = [i for i in range(seqs[0], seqs[-1] + 1) if i not in pkts]
    if missing:
        print(f"WARNING: missing seq: {missing}")

    pcm = b"".join(pkts[i] for i in seqs)

    session_wav = f"session_{latest_session}.wav"
    write_wav(session_wav, pcm)
    write_wav("up.wav", pcm)

    print("Wrote", session_wav)
    print("Wrote up.wav")

    return latest_session, session_wav


def main():
    cfg = build_config()
    session_id, wav_path = receive_one_session()
    result = process_wav_file(wav_path, cfg)

    print("\n===== FINAL RESULT =====")
    print("session:", session_id)
    print("wav:", wav_path)
    print("asr_text:", result["asr_text"])
    print("reply_text:", result["reply_text"])
    print("answer.wav saved")
    print("result.json saved")


if __name__ == "__main__":
    main()
