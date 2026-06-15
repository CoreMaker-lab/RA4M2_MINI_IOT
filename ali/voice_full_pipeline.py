# -*- coding: gbk -*-
"""
voice_full_pipeline.py

完整语音交互版：
1. 读取本地 up.wav
2. 若是 8-bit wav，则自动转为 16-bit PCM wav
3. 调阿里云一句话识别 ASR
4. 把识别文本发给 AgentRun
5. 调阿里云 TTS 合成回答语音
6. 返回：
   - asr_text
   - reply_text
   - audio_b64

运行：
    python voice_full_pipeline.py

依赖：
    python -m pip install requests
"""

import os
import io
import json
import wave
import base64
import array
import urllib.parse
import requests


# =========================================================
# 1. 配置区
# =========================================================

AGENT_URL = "https://1930052576475971.agentrun-data.cn-hangzhou.aliyuncs.com/agent-runtimes/RA4M2_1/endpoints/Default/invocations/openai/v1/chat/completions"

NLS_APPKEY = os.environ.get("NLS_APPKEY", "SKcAB1cZOqjhjElQ")
NLS_TOKEN = os.environ.get("NLS_TOKEN", "f526dc6a72d2439e867f783d78642c86")
NLS_REGION = os.environ.get("NLS_REGION", "cn-shanghai")
NLS_VOICE = os.environ.get("NLS_VOICE", "xiaoyun")

NLS_GATEWAY = f"https://nls-gateway-{NLS_REGION}.aliyuncs.com"

HTTP_TIMEOUT_ASR = 30
HTTP_TIMEOUT_AGENT = 30
HTTP_TIMEOUT_TTS = 30


# =========================================================
# 2. 工具函数
# =========================================================

def safe_json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def wav_u8_to_pcm16_wav(wav_bytes: bytes, out_sample_rate: int = 8000) -> bytes:
    """
    把 8-bit unsigned mono wav 转成 16-bit signed mono wav
    适配阿里云 ASR
    """
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


# =========================================================
# 3. 阿里云 ASR
# =========================================================

def asr_once_wav(audio_bytes: bytes, sample_rate: int = 8000) -> str:
    """
    阿里云一句话识别
    输入：16-bit PCM WAV
    """
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


# =========================================================
# 4. AgentRun 普通问答
# =========================================================

def call_ra4m2_agent(user_text: str) -> str:
    """
    调用 AgentRun，做普通聊天问答
    """
    payload = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "你是一个智能语音助手，请根据用户说的话进行自然中文回答。"
                    "如果是日常问答，就正常回答；"
                    "如果是设备控制相关问题，也可以自然回答。"
                    "回答尽量简洁自然，适合语音播报。"
                    f"\n用户语句：{user_text}"
                )
            }
        ],
        "stream": False
    }

    resp = requests.post(
        AGENT_URL,
        headers={"content-type": "application/json"},
        json=payload,
        timeout=HTTP_TIMEOUT_AGENT
    )

    print("Agent status:", resp.status_code)
    print("Agent raw text:", resp.text)

    resp.raise_for_status()

    data = resp.json()
    print("AgentRun response:")
    print(safe_json_dumps(data))

    try:
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"AgentRun response format unexpected: {e}")


# =========================================================
# 5. 阿里云 TTS
# =========================================================

def tts_wav(text: str, sample_rate: int = 8000) -> bytes:
    """
    阿里云 TTS，返回 WAV bytes
    """
    url = f"{NLS_GATEWAY}/stream/v1/tts"

    params = {
        "appkey": NLS_APPKEY,
        "token": NLS_TOKEN,
        "format": "wav",
        "sample_rate": str(sample_rate),
        "voice": NLS_VOICE,
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


# =========================================================
# 6. 总控 handler
# =========================================================

def handler(event, context=None):
    try:
        if isinstance(event, bytes):
            payload = json.loads(event.decode("utf-8"))
        elif isinstance(event, str):
            payload = json.loads(event)
        else:
            payload = event or {}

        audio_b64 = payload.get("audio_b64", "")
        if not audio_b64:
            return json.dumps({"error": "audio_b64 is empty"}, ensure_ascii=False)

        audio_bytes = base64.b64decode(audio_b64)

        # 8bit -> 16bit
        audio_bytes_16 = wav_u8_to_pcm16_wav(audio_bytes, out_sample_rate=8000)

        # 1. ASR
        asr_text = asr_once_wav(audio_bytes_16, sample_rate=8000)
        print("ASR text:", asr_text)
        if not asr_text:
            return json.dumps({"error": "asr empty result"}, ensure_ascii=False)

        # 2. Agent
        reply_text = call_ra4m2_agent(asr_text)
        print("Agent reply:", reply_text)

        # 3. TTS
        audio_reply_b64 = ""
        try:
            answer_audio = tts_wav(reply_text, sample_rate=8000)
            audio_reply_b64 = base64.b64encode(answer_audio).decode("ascii")
        except Exception as e:
            print("TTS failed:", e)

        result = {
            "asr_text": asr_text,
            "reply_text": reply_text,
            "audio_b64": audio_reply_b64
        }
        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# =========================================================
# 7. 本地调试入口
# =========================================================

if __name__ == "__main__":
    wav_path = "up.wav"
    if not os.path.exists(wav_path):
        raise SystemExit("up.wav not found in current directory.")

    with open(wav_path, "rb") as f:
        audio_bytes = f.read()

    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    event = {"audio_b64": audio_b64}

    result_json = handler(event)
    print("\n===== FINAL RESULT =====")
    print(result_json)

    result = json.loads(result_json)

    print("\nASR TEXT:")
    print(result.get("asr_text", ""))

    print("\nREPLY TEXT:")
    print(result.get("reply_text", ""))

    audio_reply_b64 = result.get("audio_b64", "")
    if audio_reply_b64:
        answer_bytes = base64.b64decode(audio_reply_b64)
        with open("answer.wav", "wb") as f:
            f.write(answer_bytes)
        print("\nanswer.wav saved.")
    else:
        print("\naudio_b64 is empty")