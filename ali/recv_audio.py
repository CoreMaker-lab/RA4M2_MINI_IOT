import socket, struct, wave

HOST = "0.0.0.0"
PORT = 9000
MAGIC = 0xA55A
SR = 8000

def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind((HOST, PORT))
    s.listen(1)
    print("TCP server listening on", PORT)
    conn, addr = s.accept()
    print("Connected from", addr)

    sessions = {}  # session_id -> {seq:payload}

    try:
        while True:
            hdr = recv_exact(conn, 8)
            magic, session, seq, ln = struct.unpack("<HHHH", hdr)
            if magic != MAGIC:
                print("bad magic", hex(magic))
                break
            payload = recv_exact(conn, ln)
            sessions.setdefault(session, {})[seq] = payload
    except Exception as e:
        print("done:", e)

    # 写 WAV（按 seq 顺序拼）
    for session, pkts in sessions.items():
        pcm = b"".join(pkts[i] for i in sorted(pkts.keys()))
        out = f"session_{session}.wav"
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)   # 8-bit unsigned
            w.setframerate(SR)
            w.writeframes(pcm)
        print("Wrote", out, "bytes=", len(pcm))
