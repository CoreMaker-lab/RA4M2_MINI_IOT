# -*- coding: utf-8 -*-
import argparse, os, numpy as np

FS = 60
WIN = 2*FS
HOP = FS//5  # 12 -> 0.2s

def _read_first_line_utf8(path):
    # 先用 utf-8-sig 兼容 BOM；失败再退回 utf-8
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='strict') as f:
            return f.readline()
    except UnicodeError:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.readline()

def smart_read_csv(path: str):
    """
    返回 ndarray [N,3] 仅 (ax,ay,az)。支持：
    - 带/不带表头；列名可为 ax,ay,az 或 ax_g,ay_g,az_g；也可含 t_ms,label
    - 强制用 UTF-8/UTF-8-SIG 读，避免 gbk 误判
    """
    first = _read_first_line_utf8(path)
    has_header = any(k in first.lower() for k in ['ax', 'ay', 'az', 't_ms', 'label'])

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
            # 回退：跳过首行按数值读前三/后3列
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
        raise RuntimeError(f"{path}: 解析到的列数={arr.shape[1]}，未得到3列(ax,ay,az)")
    return arr

def windows(x: np.ndarray):
    X=[]
    for i in range(0, x.shape[0]-WIN+1, HOP):
        X.append(x[i:i+WIN, :3])
    return np.array(X, dtype=np.float32)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--files', nargs='+', required=True, help='file.csv:label 多个用空格分隔')
    ap.add_argument('--labels', required=True, help='标签列表, 逗号分隔，例如 idle,CW,UPDOWN')
    ap.add_argument('--out', nargs=2, default=['X_windows.npy','y_labels.npy'])
    args = ap.parse_args()

    label_list = [s.strip() for s in args.labels.split(',')]
    label_to_id = {name:i for i,name in enumerate(label_list)}

    bigX=[]; bigY=[]; total_rows=0; total_win=0
    for spec in args.files:
        path, lab = spec.split(':',1)
        lab = lab.strip()
        if lab not in label_to_id:
            raise SystemExit(f"标签 {lab} 不在 --labels {label_list}")
        x = smart_read_csv(path)
        Xw = windows(x)
        yv = np.full((Xw.shape[0],), label_to_id[lab], dtype=np.int32)
        bigX.append(Xw); bigY.append(yv)
        total_rows += x.shape[0]; total_win += Xw.shape[0]
        print(f"[OK] {path:<14} rows={x.shape[0]:4d}  windows={Xw.shape[0]:3d}  label={lab}({label_to_id[lab]})")

    X = np.concatenate(bigX, axis=0)
    y = np.concatenate(bigY, axis=0)
    np.save(args.out[0], X); np.save(args.out[1], y)
    print(f"Saved {args.out[0]}  {args.out[1]}  -> total_windows={total_win}, total_rows={total_rows}")

if __name__ == '__main__':
    main()
