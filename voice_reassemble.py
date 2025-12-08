# -*- coding: utf-8 -*-
"""
voice_reassemble.py

功能：
- 作为 MQTT 客户端连接到本地 Mosquitto (127.0.0.1:1884)
- 订阅 ESP8266 上传的语音分片 JSON（含 sid/idx/total/sr/pcm_b64）
- 按 sid/idx 重组为完整的 uint16 采样流
- 按 12bit ADC 规则映射为有符号 16bit PCM（v12 - 2048 再左移 4bit）
- 做一次 5 点滑动平均，轻微平滑高频噪声
- 输出单声道 16bit/8kHz 的 WAV：voice_sessions/session-<sid>.wav
"""

import os
import json
import base64
import wave
import struct
import time
from typing import Dict, Any

import paho.mqtt.client as mqtt

# ---------- MQTT 配置 ----------
MQTT_BROKER   = "127.0.0.1"
MQTT_PORT     = 1884
MQTT_CLIENTID = "PC-voice-reassembler"

# 调试阶段直接订阅全部，后面你也可以改回具体 Topic
SUBSCRIBE_TOPIC = "#"

OUTPUT_DIR = "voice_sessions"

# sid -> { "total":int, "sr":int, "chunks":{idx:bytes}, "created_at":float }
sessions: Dict[int, Dict[str, Any]] = {}


def ensure_dir(path: str) -> None:
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def u16_to_s16_simple(raw_u16_bytes: bytes) -> bytes:
    """
    最朴素、最贴近硬件的一版 12bit -> 16bit 映射：

      1) 解成 uint16 数组
      2) 只取低 12bit：v12 = v & 0x0FFF
      3) 以 2048 为中心：center = v12 - 2048
      4) 左移 4bit（*16），得到有符号 16bit：x = center << 4
      5) 做一次 5 点滑动平均，轻微平滑高频噪声

    这样做出来的波形和 MCU 侧 DAC 播放的理论波形是一致的，
    只是在 PC 端额外加了一点点平滑，让听感更接近板子上小喇叭。
    """
    n = len(raw_u16_bytes) // 2
    if n == 0:
        return b""

    u16_samples = struct.unpack("<%dH" % n, raw_u16_bytes)

    v_min = min(u16_samples)
    v_max = max(u16_samples)
    v_mean = sum(u16_samples) / n
    print(f"[STAT] u16: samples={n}, min={v_min}, max={v_max}, mean={v_mean:.2f}")

    # 第一步：12bit -> 16bit 线性映射
    s16 = []
    for v in u16_samples:
        v12 = v & 0x0FFF        # 只保留 12bit
        center = v12 - 2048     # 以 2048 为 0
        x = center << 4         # *16, 映射到 -32768..+32752 附近
        if x > 32767:
            x = 32767
        elif x < -32768:
            x = -32768
        s16.append(x)

    # 第二步：简单 5 点滑动平均，模拟一点点低通滤波
    if n >= 5:
        smooth = [0] * n
        # 头尾简单处理，不参与完整窗口
        smooth[0] = s16[0]
        smooth[1] = (s16[0] + s16[1]) // 2
        smooth[-1] = s16[-1]
        smooth[-2] = (s16[-1] + s16[-2]) // 2
        for i in range(2, n - 2):
            smooth[i] = (
                s16[i - 2] + s16[i - 1] + s16[i] + s16[i + 1] + s16[i + 2]
            ) // 5
        s16 = smooth

    return struct.pack("<%dh" % n, *s16)


def save_session_to_wav(sid: int) -> None:
    """把某个 sid 的所有分片按 idx 排好顺序，拼成一个 WAV 文件。"""
    sess = sessions.get(sid)
    if not sess:
        print(f"[WARN] save_session_to_wav: session {sid} not found")
        return

    total  = sess["total"]
    sr     = sess["sr"]
    chunks = sess["chunks"]

    if len(chunks) != total:
        print(f"[WARN] Session {sid}: expected {total} chunks, got {len(chunks)}")

    ordered = []
    missing = []
    for i in range(total):
        if i in chunks:
            ordered.append(chunks[i])
        else:
            missing.append(i)

    if missing:
        print(f"[WARN] Session {sid} missing chunks: {missing}")

    raw_u16 = b"".join(ordered)
    print(f"[INFO] Session {sid}: raw bytes={len(raw_u16)}, "
          f"samples={len(raw_u16)//2}, sr={sr}")

    pcm_s16 = u16_to_s16_simple(raw_u16)

    ensure_dir(OUTPUT_DIR)
    path = os.path.join(OUTPUT_DIR, f"session-{sid}.wav")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)        # 单声道
        wf.setsampwidth(2)        # 16bit
        wf.setframerate(sr)       # ESP 上报的 sr（你现在是 8000）
        wf.writeframes(pcm_s16)

    print(f"[OK] Wrote WAV: {path} (frames={len(pcm_s16)//2})")

    # 用完清内存
    del sessions[sid]


# ---------- MQTT 回调 ----------

def on_connect(client, userdata, flags, rc):
    print("[MQTT] Connected, rc =", rc)
    if rc == 0:
        result, mid = client.subscribe(SUBSCRIBE_TOPIC, qos=0)
        print(f"[MQTT] Subscribe result={result}, mid={mid}, topic='{SUBSCRIBE_TOPIC}'")
    else:
        print("[MQTT] Connect failed, rc =", rc)


def on_message(client, userdata, msg):
    print(f"\n=== MQTT message on '{msg.topic}', payload_len={len(msg.payload)} ===")
    try:
        text = msg.payload.decode("utf-8", errors="ignore")
        print("[DEBUG] head:", text[:120].replace("\n", "\\n"), "...")
    except Exception as e:
        print("[ERROR] decode failed:", e)
        return

    # 解析 JSON
    try:
        data = json.loads(text)
    except Exception as e:
        print("[WARN] not JSON, ignore:", e)
        return

    required = {"sid", "idx", "total", "pcm_b64"}
    if not required.issubset(data.keys()):
        print("[WARN] JSON missing keys, ignore. keys:", data.keys())
        return

    try:
        sid   = int(data["sid"])
        idx   = int(data["idx"])
        total = int(data["total"])
        sr    = int(data.get("sr", 8000))
        b64   = data["pcm_b64"]
    except Exception as e:
        print("[ERROR] JSON fields invalid:", e)
        return

    try:
        chunk = base64.b64decode(b64)
    except Exception as e:
        print("[ERROR] base64 decode failed:", e)
        return

    sess = sessions.get(sid)
    if sess is None:
        sess = {"total": total, "sr": sr, "chunks": {}, "created_at": time.time()}
        sessions[sid] = sess
        print(f"[INFO] New session {sid}: total={total}, sr={sr}")
    else:
        if sess["total"] != total:
            print(f"[WARN] Session {sid}: total changed {sess['total']} -> {total}")
            sess["total"] = total
        if sess["sr"] != sr:
            print(f"[WARN] Session {sid}: sr changed {sess['sr']} -> {sr}")
            sess["sr"] = sr

    prev = sess["chunks"].get(idx)
    sess["chunks"][idx] = chunk
    got = len(sess["chunks"])
    print(f"[INFO] Session {sid}: got chunk {idx}/{total-1}, unique_chunks={got}")
    if prev is not None and len(prev) != len(chunk):
        print(f"[WARN] Session {sid}: chunk {idx} overwritten, len {len(prev)} -> {len(chunk)}")

    if got >= total:
        save_session_to_wav(sid)


# ---------- 主函数 ----------

def main():
    ensure_dir(OUTPUT_DIR)

    # 显式指定 Callback API v1，适配你现在的 paho 版本
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                         client_id=MQTT_CLIENTID)
    client.enable_logger()

    client.on_connect = on_connect
    client.on_message = on_message

    print(f"[MQTT] Connecting to {MQTT_BROKER}:{MQTT_PORT} ...")
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    print("[MQTT] Loop forever, waiting for messages ...")
    client.loop_forever()


if __name__ == "__main__":
    main()
