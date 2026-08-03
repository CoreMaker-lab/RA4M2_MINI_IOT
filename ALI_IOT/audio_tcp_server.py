from __future__ import annotations

import argparse
import socket
import sys
import wave
from dataclasses import dataclass
from pathlib import Path


PROTOCOL_MAGIC = "RA4A"
PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 256
MAX_AUDIO_BYTES = 2 * 1024 * 1024


class AudioServerError(RuntimeError):
    """TCP音频接收或协议解析失败。"""


@dataclass(frozen=True)
class AudioHeader:
    sample_rate: int
    sample_bits: int
    channels: int
    sample_count: int
    data_bytes: int


def recv_line(conn: socket.socket, max_bytes: int = MAX_HEADER_BYTES) -> bytes:
    """接收一行ASCII协议头，不包含结尾的换行符。"""
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
    """循环接收，直到得到指定长度的数据。"""
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
    """
    解析：
    RA4A,1,8000,16,1,40000,80000
    """
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

    if sample_rate <= 0:
        raise AudioServerError("采样率必须大于0。")

    if sample_bits != 16:
        raise AudioServerError(
            f"当前服务器仅支持16 bit PCM，收到{sample_bits} bit。"
        )

    if channels not in (1, 2):
        raise AudioServerError(
            f"当前服务器仅支持1或2声道，收到{channels}声道。"
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


def receive_one_session(
    conn: socket.socket,
    address: tuple[str, int],
    output_path: Path,
) -> None:
    print(f"\nClient connected: {address[0]}:{address[1]}")

    header_bytes = recv_line(conn)

    try:
        header_text = header_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AudioServerError(
            f"协议头不是ASCII文本：{header_bytes!r}"
        ) from exc

    print(f"Header: {header_text}")

    header = parse_header(header_text)
    duration = header.sample_count / header.sample_rate

    print(
        "Audio parameters: "
        f"{header.sample_rate} Hz, "
        f"{header.sample_bits} bit, "
        f"{header.channels} channel(s), "
        f"{header.sample_count} samples, "
        f"{duration:.2f} s, "
        f"{header.data_bytes} bytes"
    )

    pcm_data = recv_exact(conn, header.data_bytes)
    save_pcm_as_wav(pcm_data, header, output_path)

    print(f"Saved WAV: {output_path.resolve()}")

    try:
        conn.sendall(b"OK\n")
    except OSError:
        pass


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="接收RA4M2通过ESP8266上传的PCM音频并保存为WAV。"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="监听地址，默认：0.0.0.0",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="监听端口，默认：9000",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="up.wav",
        help="输出WAV文件，默认：up.wav",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="处理一次连接后退出；默认持续监听。",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = build_argument_parser().parse_args()
    output_path = Path(args.output).expanduser().resolve()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((args.host, args.port))
            server.listen(1)

            print(f"TCP audio server listening on {args.host}:{args.port}")
            print(f"Output file: {output_path}")
            print("Press Ctrl+C to stop.")

            while True:
                conn, address = server.accept()

                with conn:
                    conn.settimeout(30.0)

                    try:
                        receive_one_session(conn, address, output_path)
                    except (AudioServerError, socket.timeout, OSError) as exc:
                        print(f"Session error: {exc}", file=sys.stderr)

                if args.once:
                    break

        return 0

    except PermissionError as exc:
        print(
            f"ERROR: 无权监听端口{args.port}：{exc}",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(
            f"ERROR: 无法启动TCP服务器：{exc}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nServer stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
