# -*- coding: utf-8 -*-
"""
离线推理（含静止门限）
- 读 CSV (UTF-8/UTF-8-SIG)，滑窗：FS=60, WIN=120(2s), HOP=12(0.2s)
- 先用能量阈值判 idle，再走量化线性分类器（与 MCU 完全一致）
- 打印 raw 结果 + 去抖后的输出
用法：
  python offline_infer.py --csv cw2.csv --weights weights.json --labels idle,CW,UPDOWN
  # 可调静止阈值与去抖
  python offline_infer.py --csv idle2.csv --weights weights.json --labels idle,CW,UPDOWN --thr 5e-4 --debounce 4
"""
import argparse, json, numpy as np
from typing import List

# ===== 参数保持与训练一致 =====
FS  = 60
WIN = 2*FS     # 120
HOP = FS//5    # 12 -> 0.2s

# ===== CSV 读取（与 csv_to_windows.py 一致）=====
def _read_first_line_utf8(path):
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='strict') as f:
            return f.readline()
    except UnicodeError:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.readline()

def smart_read_csv(path: str):
    first = _read_first_line_utf8(path)
    has_header = any(k in first.lower() for k in ['ax','ay','az','t_ms','label'])
    if has_header:
        with open(path, 'r', encoding='utf-8-sig', errors='ignore') as fh:
            data = np.genfromtxt(fh, delimiter=',', names=True, dtype=None, encoding='utf-8')
        cols = [c.lower() for c in data.dtype.names]
        def pick(*cands):
            for nm in cands:
                if nm in cols: return nm
            return None
        axn = pick('ax','ax_g'); ayn = pick('ay','ay_g'); azn = pick('az','az_g')
        if axn and ayn and azn:
            ax = np.array(data[axn], dtype=np.float32)
            ay = np.array(data[ayn], dtype=np.float32)
            az = np.array(data[azn], dtype=np.float32)
            arr = np.stack([ax,ay,az], axis=1)
        else:
            with open(path, 'r', encoding='utf-8-sig', errors='ignore') as fh:
                next(fh)
                raw = np.loadtxt(fh, delimiter=',')
            if raw.ndim==1: raw=raw.reshape(1,-1)
            arr = (raw[:,1:4] if raw.shape[1] >= 4 else raw[:,0:3]).astype(np.float32)
    else:
        with open(path, 'r', encoding='utf-8-sig', errors='ignore') as fh:
            raw = np.loadtxt(fh, delimiter=',')
        if raw.ndim==1: raw=raw.reshape(1,-1)
        arr = (raw[:,1:4] if raw.shape[1] >= 4 else raw[:,0:3]).astype(np.float32)
    if arr.shape[1] != 3:
        raise RuntimeError(f"{path}: 列数={arr.shape[1]}，未解析到 ax,ay,az 三列")
    return arr

def sliding_windows(x: np.ndarray):
    X=[]
    for i in range(0, x.shape[0]-WIN+1, HOP):
        X.append(x[i:i+WIN, :3])
    return np.array(X, dtype=np.float32)

# ===== 与训练一致的 16 维特征 =====
def _stats(x):
    mean = float(x.mean())
    var  = float(max(x.var(), 0.0))
    rms  = float(np.sqrt((x**2).mean()))
    pp   = float(x.max() - x.min())
    zcr  = float(((x[1:] * x[:-1]) < 0).mean())
    return [mean, var, rms, pp, zcr]

def extract_feats_from_window(win):  # [WIN,3]
    ax = win[:,0].astype(np.float32)
    ay = win[:,1].astype(np.float32)
    az = win[:,2].astype(np.float32)
    feats: List[float] = []
    for ch in (ax, ay, az):
        feats += _stats(ch)
    dax = ax[1:] - ax[:-1]
    day = ay[1:] - ay[:-1]
    rot_xy = float((ax[:-1]*day - ay[:-1]*dax).mean())  # 顺时针<0, 逆时针>0
    feats.append(rot_xy)
    return np.array(feats, dtype=np.float32)  # 16 维

# ===== 量化推理（与 MCU 相同）=====
def infer_linear_i8(weights, feat_f32: np.ndarray) -> int:
    in_scale = weights["IN_SCALE"]
    W_q = np.array(weights["W_Q"], dtype=np.int8).reshape(weights["OUT_DIM"], weights["IN_DIM"])
    B_q = np.array(weights["B_Q"], dtype=np.int32)
    x_q = np.clip(np.rint(feat_f32 / in_scale), -128, 127).astype(np.int8)  # [IN_DIM]
    # int32 logits = W_q * x_q + B_q
    logits = (W_q.astype(np.int32) @ x_q.astype(np.int32)) + B_q
    return int(np.argmax(logits))

# ===== 主程序 =====
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', required=True)
    ap.add_argument('--weights', default='weights.json')
    ap.add_argument('--labels', required=True, help='例如 idle,CW,UPDOWN（顺序=类别索引）')
    ap.add_argument('--thr', type=float, default=5e-4, help='静止能量阈值（g^2），越小越严格')
    ap.add_argument('--debounce', type=int, default=3, help='去抖窗口数（每窗0.2s）')
    args = ap.parse_args()

    names = [s.strip() for s in args.labels.split(',')]
    with open(args.weights, 'r', encoding='utf-8') as f:
        weights = json.load(f)

    x = smart_read_csv(args.csv)             # [N,3], g
    X = sliding_windows(x)                   # [M,WIN,3]
    times = np.arange(X.shape[0]) * (HOP/FS) # 每步 0.2s

    last=-1; stable=0
    print()
    for i, win in enumerate(X):
        feat = extract_feats_from_window(win)
        # ---- 静止门限：ax_var+ay_var+az_var < thr 则直接 idle(0) ----
        energy = feat[1] + feat[6] + feat[11]  # 三轴方差和 (g^2)
        if energy < args.thr:
            cls = 0
        else:
            cls = infer_linear_i8(weights, feat)

        print(f"t= {times[i]:4.2f}s -> class={names[cls]} (idx={cls})")

        # ---- 去抖输出 ----
        if cls == last:
            stable += 1
            if stable == args.debounce:
                print("\nDebounced outputs:")
        else:
            last = cls
            stable = 1

        if stable >= args.debounce:
            print(f"t= {times[i]:4.2f}s => {names[cls]}")

if __name__ == '__main__':
    main()
