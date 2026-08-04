from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from aliyun_nls_token import NlsTokenError, NlsTokenManager

PROTOCOL_MAGIC = "RA4A"
PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 256
MAX_AUDIO_BYTES = 2 * 1024 * 1024

ASR_URL = "https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/asr"
TTS_URL = "https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/tts"

TTS_VOICE = "xiaoyun"
TTS_VOLUME = 50
TTS_SPEECH_RATE = 500
TTS_PITCH_RATE = 0

CONNECT_TIMEOUT_SEC = 10
ASR_TIMEOUT_SEC = 90
AGENT_TIMEOUT_SEC = 120
TTS_TIMEOUT_SEC = 120


class AudioServerError(RuntimeError):
    """TCP接收、音频协议或WAV保存失败。"""


class PipelineError(RuntimeError):
    """ASR、AgentRun或TTS处理失败。"""


@dataclass(frozen=True)
class AudioHeader:
    sample_rate: int
    sample_bits: int
    channels: int
    sample_count: int
    data_bytes: int


@dataclass(frozen=True)
class CloudConfig:
    appkey: str
    agentrun_url: str
    session_id: str
    token_manager: NlsTokenManager


def recv_line(conn: socket.socket, max_bytes: int = MAX_HEADER_BYTES) -> bytes:
    """接收一行ASCII协议头，不包含换行符。"""
    data = bytearray()

    while True:
        chunk = conn.recv(1)
        if not chunk:
            raise AudioServerError("连接在协议头接收完成前断开。")
        if chunk == b"\n":
            return bytes(data).rstrip(b"\r")
        data.extend(chunk)
        if len(data) > max_bytes:
            raise AudioServerError(f"协议头超过{max_bytes}字节。")


def recv_exact(conn: socket.socket, length: int) -> bytes:
    """循环接收，直到获得指定长度的数据。"""
    data = bytearray()

    while len(data) < length:
        chunk = conn.recv(min(4096, length - len(data)))
        if not chunk:
            raise AudioServerError(
                f"连接提前断开：已接收{len(data)}字节，应接收{length}字节。"
            )
        data.extend(chunk)
        print(
            f"\rReceiving audio: {len(data)}/{length} bytes",
            end="",
            flush=True,
        )

    print()
    return bytes(data)


def parse_header(header_text: str) -> AudioHeader:
    """解析协议头：RA4A,1,8000,16,1,40000,80000"""
    fields = header_text.split(",")
    if len(fields) != 7:
        raise AudioServerError(
            f"协议头字段数量错误，应为7个，实际为{len(fields)}：{header_text!r}"
        )

    magic = fields[0]
    try:
        version = int(fields[1])
        sample_rate = int(fields[2])
        sample_bits = int(fields[3])
        channels = int(fields[4])
        sample_count = int(fields[5])
        data_bytes = int(fields[6])
    except ValueError as exc:
        raise AudioServerError(f"协议头包含非数字字段：{header_text!r}") from exc

    if magic != PROTOCOL_MAGIC:
        raise AudioServerError(
            f"协议标识错误：{magic!r}，应为{PROTOCOL_MAGIC!r}。"
        )
    if version != PROTOCOL_VERSION:
        raise AudioServerError(f"不支持的协议版本：{version}。")
    if sample_rate not in (8000, 16000):
        raise AudioServerError(
            f"当前程序仅允许8000或16000 Hz，收到{sample_rate} Hz。"
        )
    if sample_bits != 16:
        raise AudioServerError(
            f"当前程序仅支持16 bit PCM，收到{sample_bits} bit。"
        )
    if channels != 1:
        raise AudioServerError(
            f"当前云端识别流程要求单声道，收到{channels}声道。"
        )
    if sample_count <= 0:
        raise AudioServerError("采样点数量必须大于0。")

    expected_bytes = sample_count * channels * (sample_bits // 8)
    if data_bytes != expected_bytes:
        raise AudioServerError(
            f"PCM长度不匹配：协议头声明{data_bytes}字节，"
            f"按采样参数计算应为{expected_bytes}字节。"
        )
    if data_bytes > MAX_AUDIO_BYTES:
        raise AudioServerError(
            f"音频数据过大：{data_bytes}字节，上限为{MAX_AUDIO_BYTES}字节。"
        )

    duration = sample_count / sample_rate
    if duration > 60:
        raise AudioServerError(
            f"音频时长不能超过60秒，当前为{duration:.2f}秒。"
        )

    return AudioHeader(
        sample_rate=sample_rate,
        sample_bits=sample_bits,
        channels=channels,
        sample_count=sample_count,
        data_bytes=data_bytes,
    )


def save_pcm_as_wav(
    pcm_data: bytes,
    header: AudioHeader,
    output_path: Path,
) -> None:
    """将PCM16小端数据封装为标准WAV文件。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(header.channels)
        wav_file.setsampwidth(header.sample_bits // 8)
        wav_file.setframerate(header.sample_rate)
        wav_file.writeframes(pcm_data)


def receive_audio(
    conn: socket.socket,
    address: tuple[str, int],
    output_path: Path,
) -> AudioHeader:
    """接收一段完整音频并保存为WAV。"""
    print(f"\nClient connected: {address[0]}:{address[1]}")

    header_bytes = recv_line(conn)
    try:
        header_text = header_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AudioServerError(f"协议头不是ASCII文本：{header_bytes!r}") from exc

    print(f"Header: {header_text}")
    header = parse_header(header_text)
    duration = header.sample_count / header.sample_rate

    print(
        "Audio parameters: "
        f"{header.sample_rate} Hz, "
        f"{header.sample_bits} bit, "
        f"{header.channels} channel, "
        f"{header.sample_count} samples, "
        f"{duration:.2f} s, "
        f"{header.data_bytes} bytes"
    )

    pcm_data = recv_exact(conn, header.data_bytes)
    save_pcm_as_wav(pcm_data, header, output_path)
    print(f"Saved WAV: {output_path.resolve()}")

    return header


def load_cloud_config() -> CloudConfig | None:
    """
    读取云端配置，并在启动阶段自动获取一次NLS Token。

    不再读取ALIYUN_NLS_TOKEN；Token由ALIYUN_AK_ID和
    ALIYUN_AK_SECRET通过CreateToken自动生成。
    """
    appkey = os.getenv("ALIYUN_NLS_APPKEY", "").strip()
    agentrun_url = os.getenv("AGENTRUN_URL", "").strip()

    missing: list[str] = []

    if not appkey:
        missing.append("ALIYUN_NLS_APPKEY")
    if not agentrun_url:
        missing.append("AGENTRUN_URL")
    if not os.getenv("ALIYUN_AK_ID", "").strip():
        missing.append("ALIYUN_AK_ID")
    if not os.getenv("ALIYUN_AK_SECRET", "").strip():
        missing.append("ALIYUN_AK_SECRET")

    if missing:
        print(
            "Cloud pipeline disabled. Missing environment variables: "
            + ", ".join(missing)
        )
        return None

    session_id = os.getenv("AGENTRUN_SESSION_ID", "").strip()

    if not session_id:
        session_id = f"ra4m2-{uuid.uuid4().hex[:12]}"

    try:
        token_manager = NlsTokenManager.from_env(
            refresh_before_seconds=600
        )

        # 启动时先验证AccessKey和SDK配置，避免板端上传后才发现鉴权失败。
        token_manager.get_token()
    except NlsTokenError as exc:
        raise PipelineError(
            f"NLS Token初始化失败：{exc}"
        ) from exc

    return CloudConfig(
        appkey=appkey,
        agentrun_url=agentrun_url,
        session_id=session_id,
        token_manager=token_manager,
    )

def decode_json_response(
    response: requests.Response,
    service_name: str,
) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        preview = response.text[:500]
        raise PipelineError(
            f"{service_name}返回内容不是有效JSON。"
            f"HTTP={response.status_code}，响应前500字符：{preview}"
        ) from exc

    if not isinstance(payload, dict):
        raise PipelineError(f"{service_name}返回的JSON不是对象：{payload!r}")
    return payload


def aliyun_asr(
    audio_path: Path,
    sample_rate: int,
    config: CloudConfig,
    session: requests.Session,
) -> str:
    params = {
        "appkey": config.appkey,
        "format": "wav",
        "sample_rate": sample_rate,
        "enable_punctuation_prediction": "true",
        "enable_inverse_text_normalization": "true",
    }
    try:
        nls_token = config.token_manager.get_token()
    except NlsTokenError as exc:
        raise PipelineError(f"获取ASR Token失败：{exc}") from exc

    headers = {
        "X-NLS-Token": nls_token,
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
        raise PipelineError(f"ASR网络请求失败：{exc}") from exc

    payload = decode_json_response(response, "ASR")
    status = payload.get("status")
    message = payload.get("message", "")
    result = str(payload.get("result", "")).strip()
    task_id = payload.get("task_id", "")

    if response.status_code != 200 or status != 20000000:
        raise PipelineError(
            f"ASR识别失败：HTTP={response.status_code}, "
            f"status={status}, message={message}, task_id={task_id}"
        )
    if not result:
        raise PipelineError(f"ASR请求成功但识别结果为空，task_id={task_id}")

    return result


def agentrun_chat(
    user_text: str,
    config: CloudConfig,
    session: requests.Session,
) -> str:
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-AgentRun-Session-ID": config.session_id,
    }
    body = {
        "messages": [{"role": "user", "content": user_text}],
        "stream": False,
    }

    try:
        response = session.post(
            config.agentrun_url,
            headers=headers,
            json=body,
            timeout=(CONNECT_TIMEOUT_SEC, AGENT_TIMEOUT_SEC),
        )
    except requests.RequestException as exc:
        raise PipelineError(f"AgentRun网络请求失败：{exc}") from exc

    payload = decode_json_response(response, "AgentRun")
    if response.status_code != 200:
        raise PipelineError(
            f"AgentRun调用失败：HTTP={response.status_code}, "
            f"error={payload.get('error', payload)}"
        )

    try:
        reply = str(payload["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise PipelineError(
            "AgentRun返回结构中缺少choices[0].message.content："
            + json.dumps(payload, ensure_ascii=False)[:1000]
        ) from exc

    if not reply:
        raise PipelineError("AgentRun返回的回复文本为空。")
    return reply


def aliyun_tts(
    text: str,
    sample_rate: int,
    output_path: Path,
    config: CloudConfig,
    session: requests.Session,
) -> None:
    if len(text) > 300:
        raise PipelineError(f"TTS文本超过300字符，当前为{len(text)}字符。")

    try:
        nls_token = config.token_manager.get_token()
    except NlsTokenError as exc:
        raise PipelineError(f"获取TTS Token失败：{exc}") from exc

    body = {
        "appkey": config.appkey,
        "token": nls_token,
        "text": text,
        "format": "wav",
        "sample_rate": sample_rate,
        "voice": TTS_VOICE,
        "volume": TTS_VOLUME,
        "speech_rate": TTS_SPEECH_RATE,
        "pitch_rate": TTS_PITCH_RATE,
    }
    headers = {"Content-Type": "application/json; charset=utf-8"}

    try:
        response = session.post(
            TTS_URL,
            headers=headers,
            json=body,
            timeout=(CONNECT_TIMEOUT_SEC, TTS_TIMEOUT_SEC),
        )
    except requests.RequestException as exc:
        raise PipelineError(f"TTS网络请求失败：{exc}") from exc

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
            f"TTS合成失败：HTTP={response.status_code}, "
            f"Content-Type={content_type}, response={error_text}"
        )

    if not response.content:
        raise PipelineError("TTS返回的音频数据为空。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(response.content)

    if not response.content.startswith(b"RIFF"):
        print("WARNING: TTS返回数据没有标准RIFF/WAV文件头，请检查服务响应。")


def save_result_log(
    input_text: str,
    reply_text: str,
    config: CloudConfig,
    output_path: Path,
) -> None:
    result = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "session_id": config.session_id,
        "asr_text": input_text,
        "reply_text": reply_text,
    }
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_cloud_pipeline(
    audio_path: Path,
    answer_path: Path,
    result_path: Path,
    sample_rate: int,
    config: CloudConfig,
) -> None:
    with requests.Session() as session:
        print("\n[1/3] Uploading audio to Alibaba Cloud ASR...")
        asr_text = aliyun_asr(audio_path, sample_rate, config, session)
        print("\nASR TEXT:")
        print(asr_text)

        print("\n[2/3] Sending recognized text to AgentRun...")
        reply_text = agentrun_chat(asr_text, config, session)
        print("\nREPLY TEXT:")
        print(reply_text)

        print("\n[3/3] Synthesizing reply audio...")
        aliyun_tts(reply_text, sample_rate, answer_path, config, session)

    save_result_log(asr_text, reply_text, config, result_path)
    print(f"\nanswer.wav saved: {answer_path.resolve()}")
    print(f"Result log saved: {result_path.resolve()}")
    print(f"AgentRun session ID: {config.session_id}")



MAX_REPLY_SAMPLES = 40000
REPLY_SEND_CHUNK_SIZE = 512


def load_reply_wav(answer_path: Path) -> tuple[int, int, bytes]:
    """
    读取answer.wav并返回：
    sample_rate、实际sample_count、PCM16小端数据。

    部分TTS生成的WAV文件头可能声明了大于实际数据的帧数。
    因此不能直接使用getnframes()计算发送长度，而应以实际读取到的
    PCM字节数为准。
    """
    if not answer_path.exists():
        raise PipelineError(f"回复音频不存在：{answer_path}")

    try:
        with wave.open(str(answer_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            declared_samples = wav_file.getnframes()
            compression = wav_file.getcomptype()

            if compression != "NONE":
                raise PipelineError(
                    f"answer.wav必须为未压缩PCM，当前压缩类型：{compression}"
                )

            if channels != 1:
                raise PipelineError(
                    f"answer.wav必须为单声道，当前为{channels}声道。"
                )

            if sample_width != 2:
                raise PipelineError(
                    f"answer.wav必须为16 bit，当前采样宽度为"
                    f"{sample_width * 8} bit。"
                )

            if sample_rate != 8000:
                raise PipelineError(
                    f"answer.wav必须为8000 Hz，当前为{sample_rate} Hz。"
                )

            # 最多读取5秒。即使WAV头声明的帧数异常，readframes()
            # 也只会返回文件中实际存在的PCM数据。
            pcm_data = wav_file.readframes(MAX_REPLY_SAMPLES)

    except wave.Error as exc:
        raise PipelineError(f"读取answer.wav失败：{exc}") from exc

    if not pcm_data:
        raise PipelineError("answer.wav没有可发送的PCM数据。")

    if (len(pcm_data) % 2) != 0:
        raise PipelineError(
            f"answer.wav PCM长度不是2的整数倍：{len(pcm_data)}字节。"
        )

    actual_samples = len(pcm_data) // 2

    if declared_samples != actual_samples:
        print(
            "WARNING: answer.wav header sample count does not match "
            f"actual PCM data: header={declared_samples}, "
            f"actual={actual_samples}. Using actual PCM length."
        )

    if actual_samples >= MAX_REPLY_SAMPLES:
        print(
            f"Reply audio limited to {MAX_REPLY_SAMPLES} samples "
            "(5.00 s)."
        )

    return sample_rate, actual_samples, pcm_data


def send_answer_audio(
    conn: socket.socket,
    answer_path: Path,
) -> None:
    """通过当前TCP连接向RA4M2下发RA4R协议头和PCM16数据。"""
    sample_rate, sample_count, pcm_data = load_reply_wav(answer_path)
    data_bytes = len(pcm_data)

    reply_header = (
        f"RA4R,1,{sample_rate},16,1,"
        f"{sample_count},{data_bytes}\n"
    ).encode("ascii")

    print(f"\nReply header: {reply_header.decode('ascii').rstrip()}")

    try:
        conn.sendall(reply_header)

        sent = 0
        packet_index = 0

        while sent < data_bytes:
            chunk = pcm_data[sent:sent + REPLY_SEND_CHUNK_SIZE]
            conn.sendall(chunk)

            sent += len(chunk)
            packet_index += 1

            if ((packet_index % 16) == 0) or (sent == data_bytes):
                print(
                    f"\rSending reply: {sent}/{data_bytes} bytes, "
                    f"packet={packet_index}",
                    end="",
                    flush=True,
                )

    except (socket.timeout, OSError) as exc:
        raise PipelineError(f"回复音频TCP下发失败：{exc}") from exc

    print()
    print(
        f"Reply audio sent: {sample_count} samples, "
        f"{data_bytes} bytes, {packet_index} packets."
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "接收RA4M2上传的音频，生成up.wav，"
            "自动获取NLS Token，执行ASR、AgentRun和TTS，"
            "并将answer.wav下发给RA4M2。"
        )
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--input-wav", default="up.wav")
    parser.add_argument("--answer-wav", default="answer.wav")
    parser.add_argument("--result-json", default="last_result.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-cloud", action="store_true")
    parser.add_argument(
        "--fixed-answer",
        default="",
        help=(
            "跳过云端生成，直接下发指定的8kHz/16bit/单声道WAV，"
            "用于先测试TCP下行和DAC播放。"
        ),
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = build_argument_parser().parse_args()
    input_path = Path(args.input_wav).expanduser().resolve()
    answer_path = Path(args.answer_wav).expanduser().resolve()
    result_path = Path(args.result_json).expanduser().resolve()
    fixed_answer_path = (
        Path(args.fixed_answer).expanduser().resolve()
        if args.fixed_answer
        else None
    )

    if args.no_cloud or fixed_answer_path is not None:
        cloud_config = None
    else:
        try:
            cloud_config = load_cloud_config()
        except PipelineError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((args.host, args.port))
            server.listen(1)

            print(f"TCP audio cloud server listening on {args.host}:{args.port}")
            print(f"Input WAV:  {input_path}")
            print(f"Answer WAV: {answer_path}")

            if fixed_answer_path is not None:
                print(f"Reply mode: fixed WAV -> {fixed_answer_path}")
            elif args.no_cloud:
                print("Reply mode: disabled by --no-cloud")
            elif cloud_config is None:
                print("Reply mode: disabled because cloud configuration is missing")
            else:
                print("Reply mode: cloud pipeline enabled")

            print("Press Ctrl+C to stop.")

            while True:
                conn, address = server.accept()

                with conn:
                    # 上传接收和回复下发共用同一条TCP连接。
                    conn.settimeout(180.0)

                    try:
                        header = receive_audio(conn, address, input_path)

                        if fixed_answer_path is not None:
                            print(
                                "\nCloud processing skipped. "
                                "Sending fixed answer WAV..."
                            )
                            send_answer_audio(conn, fixed_answer_path)

                        elif cloud_config is not None:
                            run_cloud_pipeline(
                                input_path,
                                answer_path,
                                result_path,
                                header.sample_rate,
                                cloud_config,
                            )
                            send_answer_audio(conn, answer_path)

                        else:
                            print(
                                "\nAudio received successfully, but no reply "
                                "audio was sent. Use cloud configuration or "
                                "--fixed-answer."
                            )

                    except (
                        AudioServerError,
                        PipelineError,
                        socket.timeout,
                        OSError,
                    ) as exc:
                        print(f"\nVoice session error: {exc}", file=sys.stderr)

                print("\nWaiting for the next recording...")

                if args.once:
                    break

        return 0

    except PermissionError as exc:
        print(f"ERROR: 无权监听端口{args.port}：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: 无法启动TCP服务器：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nServer stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
