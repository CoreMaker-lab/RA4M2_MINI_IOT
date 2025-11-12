# -*- coding: utf-8 -*-
import numpy as np, json, os
from sklearn.linear_model import LogisticRegression

import numpy as np
FS = 60
WIN = 2*FS
HOP = FS//5
def _stats(x):
    mean = float(x.mean()); var = float(max(x.var(),0.0)); rms = float(np.sqrt((x**2).mean()))
    pp = float(x.max()-x.min()); zcr = float(((x[1:]*x[:-1])<0).mean())
    return [mean,var,rms,pp,zcr]
def extract_feats_from_window(win):
    ax = win[:,0].astype(np.float32); ay = win[:,1].astype(np.float32); az = win[:,2].astype(np.float32)
    feats = []; 
    for ch in (ax,ay,az): feats += _stats(ch)
    dax = ax[1:]-ax[:-1]; day = ay[1:]-ay[:-1]
    rot_xy = float((ax[:-1]*day - ay[:-1]*dax).mean())
    feats.append(rot_xy)
    return np.array(feats, dtype=np.float32)

X = np.load('X_windows.npy')
y = np.load('y_labels.npy')
Xf = np.vstack([extract_feats_from_window(w) for w in X])
clf = LogisticRegression(max_iter=300, solver='lbfgs')
clf.fit(Xf, y)
in_scale = float(max(abs(Xf).max(), 1e-6) / 127.0)
W = clf.coef_.astype(np.float32); b = clf.intercept_.astype(np.float32)
W_q = np.clip(np.rint(W / in_scale), -128, 127).astype(np.int8)
B_q = np.rint(b / in_scale).astype(np.int32)
hdr = (
    "#pragma once\n"
    "// Auto-generated (60Hz accel-only)\n"
    f"static const int IN_DIM_ = {Xf.shape[1]};\n"
    f"static const int OUT_DIM_ = {W.shape[0]};\n"
    f"static const float IN_SCALE_ = {in_scale:.9e}f;\n"
    f"static const int8_t  W_Q_[{W_q.size}] = {{ " + ",".join(map(str, W_q.flatten())) + " }};\n"
    f"static const int32_t B_Q_[{B_q.size}] = {{ " + ",".join(map(str, B_q)) + " }};\n"
)
os.makedirs('../firmware/include', exist_ok=True)
open('../firmware/include/weights.h','w',encoding='utf-8').write(hdr)
print('Exported weights to ../firmware/include/weights.h')
weights = {"IN_DIM":int(Xf.shape[1]),"OUT_DIM":int(W.shape[0]),"IN_SCALE":float(in_scale),
           "W_Q":W_q.flatten().tolist(),"B_Q":B_q.tolist()}
open('weights.json','w',encoding='utf-8').write(json.dumps(weights, ensure_ascii=False))
print('Also wrote weights.json')
