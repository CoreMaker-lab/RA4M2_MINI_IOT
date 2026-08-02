from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
import wave
from pathlib import Path
from typing import Any

import requests


ASR_URL = "https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/asr"
TTS_URL = "https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/tts"

SAMPLE_RATE = 8000
AUDIO_FORMAT = "wav"

TTS_VOICE = "xiaoyun"
TTS_VOLUME = 50
TTS_SPEECH_RATE = 500
TTS_PITCH_RATE = 0

CONNECT_TIMEOUT_SEC = 10
ASR_TIMEOUT_SEC = 90
AGENT_TIMEOUT_SEC = 120
TTS_TIMEOUT_SEC = 120


class PipelineError(RuntimeError):
    """Raised when a stage in the voice pipeline fails."""


def require_env(name: str) -> str:
    """Read a required environment variable."""
    value = os.getenv(name, "").strip()
    if not value:
        raise PipelineError(f"缺少环境变量：{name}")
    return value


def decode_json_response(response: requests.Response, service_name: str) -> dict[str, Any]:
    """Decode a JSON response and provide a useful error if decoding fails."""
    try:
        payload = response.json()
    except ValueError as exc:
        preview = response.text[:500]
        raise PipelineError(
            f"{service_name} 返回内容不是有效 JSON。"
            f"HTTP={response.status_code}，响应前500字符：{preview}"
        ) from exc

    if not isinstance(payload, dict):
        raise PipelineError(f"{service_name} 返回的 JSON 不是对象：{payload!r}")
    return payload


def validate_input_wav(audio_path: Path) -> None:
    """Validate the input format expected by the 8 kHz ASR project."""
    if not audio_path.is_file():
        raise PipelineError(f"找不到输入音频：{audio_path}")

    if audio_path.stat().st_size > 2 * 1024 * 1024:
        raise PipelineError("输入音频超过 2 MB。")

    try:
        with wave.open(str(audio_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            compression = wav_file.getcomptype()
    except (wave.Error, EOFError) as exc:
        raise PipelineError(f"无法读取 WAV 文件：{audio_path}") from exc

    duration = frame_count / sample_rate if sample_rate else 0.0

    print(
        "INPUT WAV:"
        f" channels={channels},"
        f" sample_width={sample_width * 8}bit,"
        f" sample_rate={sample_rate}Hz,"
        f" duration={duration:.2f}s,"
        f" compression={compression}"
    )

    errors: list[str] = []
    if channels != 1:
        errors.append(f"声道数应为1，当前为{channels}")
    if sample_width != 2:
        errors.append(f"采样位数应为16bit，当前为{sample_width * 8}bit")
    if sample_rate != SAMPLE_RATE:
        errors.append(f"采样率应为{SAMPLE_RATE}Hz，当前为{sample_rate}Hz")
    if compression != "NONE":
        errors.append(f"WAV应为未压缩PCM，当前压缩类型为{compression}")
    if duration <= 0:
        errors.append("音频时长为0")
    if duration > 60:
        errors.append(f"音频时长不能超过60秒，当前为{duration:.2f}秒")

    if errors:
        raise PipelineError("输入音频参数不符合要求：" + "；".join(errors))


def aliyun_asr(
    audio_path: Path,
    appkey: str,
    token: str,
    session: requests.Session,
) -> str:
    """Upload a WAV file to Alibaba Cloud ASR and return recognized text."""
    params = {
        "appkey": appkey,
        "format": AUDIO_FORMAT,
        "sample_rate": SAMPLE_RATE,
        "enable_punctuation_prediction": "true",
        "enable_inverse_text_normalization": "true",
    }
    headers = {
        "X-NLS-Token": token,
        "Content-Type": "application/octet-stream",
    }

    try:
        with audio_path.open("rb") as audio_file:
            response = session.post(
                ASR_URL,
                params=params,
                headers=headers,
                data=audio_file,
                timeout=(CONNECT_TIMEOUT_SEC, ASR_TIMEOUT_SEC),
            )
    except requests.RequestException as exc:
        raise PipelineError(f"ASR 网络请求失败：{exc}") from exc

    payload = decode_json_response(response, "ASR")
    status = payload.get("status")
    message = payload.get("message", "")
    result = str(payload.get("result", "")).strip()
    task_id = payload.get("task_id", "")

    if response.status_code != 200 or status != 20000000:
        raise PipelineError(
            f"ASR 识别失败：HTTP={response.status_code}, "
            f"status={status}, message={message}, task_id={task_id}"
        )

    if not result:
        raise PipelineError(f"ASR 请求成功但识别结果为空，task_id={task_id}")

    return result


def agentrun_chat(
    user_text: str,
    endpoint_url: str,
    session_id: str,
    session: requests.Session,
) -> str:
    """Send text to an AgentRun OpenAI-compatible endpoint."""
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-AgentRun-Session-ID": session_id,
    }
    body = {
        "messages": [
            {
                "role": "user",
                "content": user_text,
            }
        ],
        "stream": False,
    }

    try:
        response = session.post(
            endpoint_url,
            headers=headers,
            json=body,
            timeout=(CONNECT_TIMEOUT_SEC, AGENT_TIMEOUT_SEC),
        )
    except requests.RequestException as exc:
        raise PipelineError(f"AgentRun 网络请求失败：{exc}") from exc

    payload = decode_json_response(response, "AgentRun")

    if response.status_code != 200:
        error = payload.get("error", payload)
        raise PipelineError(
            f"AgentRun 调用失败：HTTP={response.status_code}, error={error}"
        )

    try:
        reply = str(payload["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise PipelineError(
            "AgentRun 返回结构中缺少 choices[0].message.content："
            + json.dumps(payload, ensure_ascii=False)[:1000]
        ) from exc

    if not reply:
        raise PipelineError("AgentRun 返回的回复文本为空。")

    return reply


def aliyun_tts(
    text: str,
    output_path: Path,
    appkey: str,
    token: str,
    session: requests.Session,
) -> None:
    """Synthesize reply text as an 8 kHz WAV file."""
    if len(text) > 300:
        raise PipelineError(f"TTS 文本超过300字符，当前为{len(text)}字符。")

    body = {
        "appkey": appkey,
        "token": token,
        "text": text,
        "format": AUDIO_FORMAT,
        "sample_rate": SAMPLE_RATE,
        "voice": TTS_VOICE,
        "volume": TTS_VOLUME,
        "speech_rate": TTS_SPEECH_RATE,
        "pitch_rate": TTS_PITCH_RATE,
    }
    headers = {
        "Content-Type": "application/json; charset=utf-8",
    }

    try:
        response = session.post(
            TTS_URL,
            headers=headers,
            json=body,
            timeout=(CONNECT_TIMEOUT_SEC, TTS_TIMEOUT_SEC),
        )
    except requests.RequestException as exc:
        raise PipelineError(f"TTS 网络请求失败：{exc}") from exc

    content_type = response.headers.get("Content-Type", "").lower()
    looks_like_json = (
        "application/json" in content_type
        or response.content.lstrip().startswith(b"{")
    )

    if response.status_code != 200 or looks_like_json:
        try:
            error_text = json.dumps(response.json(), ensure_ascii=False)
        except ValueError:
            error_text = response.text[:1000]
        raise PipelineError(
            f"TTS 合成失败：HTTP={response.status_code}, "
            f"Content-Type={content_type}, response={error_text}"
        )

    if not response.content:
        raise PipelineError("TTS 返回的音频数据为空。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(response.content)

    if output_path.suffix.lower() == ".wav" and not response.content.startswith(b"RIFF"):
        print(
            "WARNING: 返回数据没有标准 RIFF/WAV 文件头，"
            "请检查TTS格式参数和服务响应。"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="阿里云 ASR → AgentRun → TTS 完整语音交互测试"
    )
    parser.add_argument(
        "-i",
        "--input",
        default="up.wav",
        help="输入WAV文件，默认：up.wav",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="answer.wav",
        help="输出WAV文件，默认：answer.wav",
    )
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    try:
        appkey = require_env("ALIYUN_NLS_APPKEY")
        token = require_env("ALIYUN_NLS_TOKEN")
        agentrun_url = require_env("AGENTRUN_URL")

        validate_input_wav(input_path)

        session_id = os.getenv(
            "AGENTRUN_SESSION_ID",
            f"ra4m2-{uuid.uuid4().hex[:12]}",
        )

        with requests.Session() as session:
            print("\n[1/3] Uploading audio to Alibaba Cloud ASR...")
            asr_text = aliyun_asr(input_path, appkey, token, session)
            print("\nASR TEXT:")
            print(asr_text)

            print("\n[2/3] Sending recognized text to AgentRun...")
            reply_text = agentrun_chat(
                asr_text,
                agentrun_url,
                session_id,
                session,
            )
            print("\nREPLY TEXT:")
            print(reply_text)

            print("\n[3/3] Synthesizing reply audio...")
            aliyun_tts(
                reply_text,
                output_path,
                appkey,
                token,
                session,
            )

        print(f"\nanswer.wav saved: {output_path}")
        print(f"AgentRun session ID: {session_id}")
        return 0

    except PipelineError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n用户终止程序。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
