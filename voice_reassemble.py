# -*- coding: utf-8 -*-
"""
voice_reassemble.py
从阿里云 IoT 的 voice_up Topic 订阅语音分片，
按 sid/idx 重新拼成完整 PCM，并保存为 session-<sid>.wav

当前适配 MCU 端上报格式：
  {"sid":1,"idx":0,"total":200,"sr":8000,"pcm_b64":"......"}
采样：8kHz, 16bit, mono
"""

import os
import json
import time
import base64
import wave

import paho.mqtt.client as mqtt

# ========= 1. 根据你的设备信息填写这里 =========
# 和 MCU 一样的实例：
# MQTT_BROKER   = "a1fabJdOLz0.iot-as-mqtt.cn-shanghai.aliyuncs.com"

MQTT_BROKER   = "127.0.0.1"
MQTT_PORT     = 1883

# 这里先直接用你 RA4M2 那个设备的用户名/密码，方便验证
# MQTT_USERNAME = "tHV3SyEhr3BrH7JwvMuq&a1fabJdOLz0"
# MQTT_PASSWORD = "61ee1d76e4bcb1a3363bf440d4573ffd4fde4adc48b032bac58bab0e0bda4984"

# MQTT_USERNAME = None             # 先不用用户名密码
# MQTT_PASSWORD = None

MQTT_USERNAME = "tHV3SyEhr3BrH7JwvMuq&a1fabJdOLz0"
MQTT_PASSWORD = "e1df04bd20bab2c47ee2457bac232122e094267874c3ef2a35108bea5ac70e34"


# PC 这边自己起一个 clientId，千万不要和板子上的一样
# MQTT_CLIENTID = "a1fabJdOLz0.tHV3SyEhr3BrH7JwvMuq|securemode=2,signmethod=hmacsha256,timestamp=1765119201823|"
MQTT_CLIENTID = "a1fabJdOLz0.tHV3SyEhr3BrH7JwvMuq|securemode=2\\,signmethod=hmacsha256\\,timestamp=1761066873178|"

# 订阅的 Topic，必须和 MCU 上报的 MQTT_VOICE_TOPIC 完全一致
VOICE_UP_TOPIC = "/a1fabJdOLz0/tHV3SyEhr3BrH7JwvMuq/user/voice_up"

# 输出 wav 的目录
OUTPUT_DIR = "./voice_sessions"

# ========= 2. 会话缓存：按 sid 聚合 =========

# sessions:
#   key: sid
#   value: {
#       "total": <total_chunks>,
#       "sr":    <sample_rate>,
#       "chunks": { idx: pcm_bytes, ... },
#       "ts":    <first_receive_time>
#   }
sessions = {}


def ensure_dir(path: str):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


# ========= 3. MQTT 回调 =========

def on_connect(client, userdata, flags, rc, properties=None):
    print("[MQTT] Connected, rc =", rc)
    if rc == 0:
        client.subscribe(VOICE_UP_TOPIC, qos=0)
        print(f"[MQTT] Subscribed to: {VOICE_UP_TOPIC}")
    else:
        print("[MQTT] Connect failed, rc =", rc)


def on_message(client, userdata, msg):
    print(f"\n=== MQTT message on {msg.topic}, len={len(msg.payload)} ===")
    try:
        text = msg.payload.decode("utf-8").strip()
        print("[DEBUG] Payload text (head):", text[:120], "...")
        data = json.loads(text)
    except Exception as e:
        print("[ERROR] JSON decode failed:", e)
        return

    sid   = data.get("sid")
    idx   = data.get("idx")
    total = data.get("total")
    sr    = data.get("sr", 8000)
    b64   = data.get("pcm_b64", "")

    if sid is None or idx is None or total is None:
        print("[WARN] Missing sid/idx/total in payload, skip.")
        return

    try:
        pcm_bytes = base64.b64decode(b64)
    except Exception as e:
        print("[ERROR] Base64 decode failed:", e)
        return

    sess = sessions.get(sid)
    if sess is None:
        sess = {
            "total":  int(total),
            "sr":     int(sr),
            "chunks": {},
            "ts":     time.time(),
        }
        sessions[sid] = sess

    sess["chunks"][idx] = pcm_bytes
    print(f"[INFO] Session {sid}: got chunk {idx+1}/{sess['total']}, bytes={len(pcm_bytes)}")

    # 是否已经收齐所有分片
    if len(sess["chunks"]) == sess["total"]:
        print(f"[INFO] Session {sid} complete, assembling WAV...")
        save_session_to_wav(sid, sess)
        # 用完删除，防止越攒越多
        del sessions[sid]


# ========= 4. 拼接 PCM + 写 WAV =========

def save_session_to_wav(sid, sess):
    ensure_dir(OUTPUT_DIR)

    total = sess["total"]
    sr    = sess["sr"]
    chunks = sess["chunks"]

    # 按 idx 顺序拼接
    pcm_list = []
    missing = []
    for i in range(total):
        if i in chunks:
            pcm_list.append(chunks[i])
        else:
            missing.append(i)

    if missing:
        print(f"[WARN] Session {sid} missing chunks: {missing}")
        # 可以视情况继续等，但这里先直接跳过
        return

    pcm_all = b"".join(pcm_list)
    print(f"[INFO] Session {sid}: final PCM bytes = {len(pcm_all)}")

    out_path = os.path.join(OUTPUT_DIR, f"session-{sid}.wav")
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)      # mono
        wf.setsampwidth(2)      # 16bit
        wf.setframerate(sr)     # 8000
        wf.writeframes(pcm_all)

    print(f"[OK] Wrote WAV file: {out_path}")


# ========= 5. 主函数 =========

def main():
    ensure_dir(OUTPUT_DIR)

    client = mqtt.Client(
        client_id=MQTT_CLIENTID,
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"[MQTT] Connecting to {MQTT_BROKER}:{MQTT_PORT} ...")
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)

    print("[MQTT] Loop forever, waiting for voice_up messages ...")
    client.loop_forever()


if __name__ == "__main__":
    main()
