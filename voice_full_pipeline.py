# -*- coding: gbk -*-
"""
音频 -> 阿里云ASR(一句话识别) -> RA4M2 AgentRun -> 阿里云TTS
返回：识别文本 + 智能体回答 + 回答语音(base64)

本地测试：
    1. 和本文件同目录放一个 8kHz 单声道 WAV：up.wav
    2. 安装依赖：python -m pip install requests
    3. 运行：python voice_full_pipeline.py

函数计算 FC 使用：
    - 入口函数：voice_full_pipeline.handler
    - 触发方式推荐 HTTP / API 网关
    - event JSON 格式：{"audio_b64": "..."}  // 8kHz wav 的base64
"""

import os
import json
import base64
import requests


# ================== 配置 ==================

# 1) RA4M2 AgentRun 接口
AGENT_URL = os.environ.get(
    "AGENT_URL",
    "https://1930052576475971.agentrun-data.cn-hangzhou.aliyuncs.com/"
    "agent-runtimes/RA4M2/endpoints/Default/invocations/openai/v1/chat/completions"
)

# 2) 阿里云智能语音 NLS 配置
#    本地测试时，可以直接把 "YOUR_XXX_HERE" 换成你控制台里的真实值
#    上线到 FC 时，可以删掉这两个默认值，改由环境变量注入。
NLS_APPKEY = "hfwLYqR6T2c5KB9U"
NLS_TOKEN  = "4b367e13c23347e4943f18e123d31d9f"

NLS_VOICE  = "xiaoyun"
NLS_REGION = "cn-shanghai"
NLS_GATEWAY = f"https://nls-gateway-{NLS_REGION}.aliyuncs.com"


# ================== 1. 调用 RA4M2 AgentRun ==================

def call_ra4m2_agent(user_text: str) -> str:
    """
    调用 RA4M2 Agent，一句话问答。
    :param user_text: ASR 识别得到的用户说的话
    :return: 智能体返回的一句话文本
    """
    payload = {
        "messages": [
            {"role": "user", "content": user_text}
        ],
        "stream": False,
    }

    resp = requests.post(
        AGENT_URL,
        headers={"content-type": "application/json"},
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    print("AgentRun resp:", data)

    return data["choices"][0]["message"]["content"]


# ================== 2. 阿里云一句话识别 ASR ==================

def asr_once_wav(audio_bytes: bytes, sample_rate: int = 8000) -> str:
    """
    使用阿里云一句话识别将 WAV 音频转成文本。
    :param audio_bytes: 完整 wav(含头) 的 bytes
    :param sample_rate: 采样率，与你录音一致（8kHz）
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
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    print("ASR resp:", data)

    return data.get("result", "")


# ================== 3. 阿里云 TTS 合成 ==================

def tts_wav(text: str, sample_rate: int = 8000) -> bytes:
    """
    使用阿里云 TTS 将文本合成 WAV 音频。
    按照 curl 示例，将 token 和 text 放到 URL 参数中。
    """
    url = f"{NLS_GATEWAY}/stream/v1/tts"

    # 完全对齐你 curl 成功的那种写法
    params = {
        "appkey": NLS_APPKEY,
        "token":  NLS_TOKEN,
        "format": "wav",
        "sample_rate": str(sample_rate),
        "voice":  NLS_VOICE,
        "text":   text,
    }

    # 不需要额外 header 和 body，直接 POST 即可
    resp = requests.post(url, params=params, timeout=30)

    if resp.status_code != 200:
        # 打印一下报文，方便后面调试
        print("TTS HTTP error:", resp.status_code)
        print("TTS resp text:", resp.text)
        resp.raise_for_status()

    audio = resp.content
    print("TTS audio length:", len(audio))
    return audio



# ================== 4. 总控 handler ==================

def handler(event, context=None):
    """
    通用入口（可以直接作为 FC 入口函数）：

    event: JSON/dict，格式如下：
        {
            "audio_b64": "..."   # 8kHz 单声道 WAV 的 base64
        }

    返回：JSON 字符串
        {
            "asr_text": "识别出的用户语音文本",
            "reply_text": "智能体回答文本",
            "audio_b64": "..."   # 回答语音的 WAV base64
        }
    """
    # 解析 event
    if isinstance(event, (bytes, str)):
        payload = json.loads(event)
    else:
        payload = event

    audio_b64 = payload.get("audio_b64", "")
    if not audio_b64:
        return json.dumps({"error": "audio_b64 is empty"})

    audio_bytes = base64.b64decode(audio_b64)

    # 1. ASR：语音 -> 文本
    asr_text = asr_once_wav(audio_bytes, sample_rate=8000)
    print("ASR text:", asr_text)
    if not asr_text:
        return json.dumps({"error": "asr empty result"})

    # 2. Agent：文本 -> 回答文本
    reply_text = call_ra4m2_agent(asr_text)
    print("Agent reply:", reply_text)

    # 3. TTS：回答文本 -> 语音
    answer_audio = tts_wav(reply_text, sample_rate=8000)

    # 4. 语音转 base64，方便通过 HTTP/MQTT 传输
    answer_b64 = base64.b64encode(answer_audio).decode("ascii")

    result = {
        "asr_text": asr_text,
        "reply_text": reply_text,
        "audio_b64": answer_b64,
    }
    return json.dumps(result, ensure_ascii=False)


# ================== 5. 本地调试 ==================

if __name__ == "__main__":
    wav_path = "up.wav"
    if not os.path.exists(wav_path):
        raise SystemExit("up.wav not found in current directory.")

    with open(wav_path, "rb") as f:
        audio_bytes = f.read()

    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    test_event = {"audio_b64": audio_b64}

    # 调用整条流水线
    result_json = handler(test_event)
    print(result_json)

    # 额外：把返回的 audio_b64 存成 answer.wav
    result = json.loads(result_json)
    answer_b64 = result.get("audio_b64", "")
    if answer_b64:
        answer_bytes = base64.b64decode(answer_b64)
        with open("answer.wav", "wb") as f:
            f.write(answer_bytes)
        print("answer.wav saved, you can play it.")
    else:
        print("no audio_b64 in result")