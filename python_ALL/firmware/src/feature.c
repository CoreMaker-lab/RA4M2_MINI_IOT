// feature.c  —— 16维特征（3轴×5统计量 + rot_xy），与训练脚本一致
#include "feature.h"
#include <math.h>
#include <string.h>

// ---------- 基础统计 ----------
static void stats(const float* x, int n, float* mean, float* var, float* rms, float* p2p) {
    float s = 0.f, s2 = 0.f, mn = 1e30f, mx = -1e30f;
    for (int i = 0; i < n; ++i) {
        const float v = x[i];
        s += v; s2 += v * v;
        if (v < mn) mn = v;
        if (v > mx) mx = v;
    }
    const float m = s / n;
    float vv = s2 / n - m * m;
    *mean = m;
    *var = (vv > 0.f) ? vv : 0.f;        // 与 PC 端保持一致：方差下限0
    *rms = sqrtf(s2 / n);
    *p2p = mx - mn;
}

static float zcr(const float* x, int n) {
    int c = 0;
    for (int i = 1; i < n; ++i) {
        const float a = x[i - 1], b = x[i];
        if ((a <= 0 && b > 0) || (a >= 0 && b < 0)) c++;
    }
    return (float)c / (float)(n - 1);
}

// ---------- 通用“数据流”Getter（交错/环形） ----------
typedef struct { const float* ptr; int stride; int offset; } itlv_ctx_t;
static float get_itlv(int i, void* ctx) {
    itlv_ctx_t* c = (itlv_ctx_t*)ctx;
    return c->ptr[i * c->stride + c->offset];
}

typedef struct { const float* ring; int ring_len; int start; int ch; } circ_ctx_t;
static float get_circ(int i, void* ctx) {
    circ_ctx_t* c = (circ_ctx_t*)ctx;
    const int j = (c->start + i) % c->ring_len;
    return c->ring[j * 3 + c->ch];
}

// ---------- rot_xy（修正：分别传 ax/ay 的 ctx） ----------
static float rot_xy_stream2(
    int n,
    float (*get_ax)(int, void*), void* ctx_ax,
    float (*get_ay)(int, void*), void* ctx_ay)
{
    if (n <= 1) return 0.f;
    float ax_prev = get_ax(0, ctx_ax);
    float ay_prev = get_ay(0, ctx_ay);
    double acc = 0.0;

    for (int i = 1; i < n; ++i) {
        const float ax = get_ax(i, ctx_ax);
        const float ay = get_ay(i, ctx_ay);
        const float dax = ax - ax_prev;
        const float day = ay - ay_prev;
        acc += (double)(ax_prev * day - ay_prev * dax);   // mean(ax[:-1]*dAy - ay[:-1]*dAx)
        ax_prev = ax;
        ay_prev = ay;
    }
    return (float)(acc / (double)(n - 1));
}

// ---------- 公开接口：交错窗口 ----------
void feat_extract(const float* win, int win_len, float* out) {
    // win 形如 [ax0,ay0,az0, ax1,ay1,az1, ...]，长度= win_len*3
    itlv_ctx_t axc = { win, 3, 0 };
    itlv_ctx_t ayc = { win, 3, 1 };
    itlv_ctx_t azc = { win, 3, 2 };

    // 拷贝到连续缓冲做统计（避免重复调用 getter 的开销）
    // 若栈空间紧张，可改为边取边统计
    float ax[1024], ay[1024], az[1024];   // 假设 win_len <= 1024
    for (int i = 0; i < win_len; ++i) {
        ax[i] = get_itlv(i, &axc);
        ay[i] = get_itlv(i, &ayc);
        az[i] = get_itlv(i, &azc);
    }

    int k = 0; float mean, var, rms, p2p_;
    stats(ax, win_len, &mean, &var, &rms, &p2p_); out[k++] = mean; out[k++] = var; out[k++] = rms; out[k++] = p2p_; out[k++] = zcr(ax, win_len);
    stats(ay, win_len, &mean, &var, &rms, &p2p_); out[k++] = mean; out[k++] = var; out[k++] = rms; out[k++] = p2p_; out[k++] = zcr(ay, win_len);
    stats(az, win_len, &mean, &var, &rms, &p2p_); out[k++] = mean; out[k++] = var; out[k++] = rms; out[k++] = p2p_; out[k++] = zcr(az, win_len);

    // 第16维：rot_xy（修正后的双 ctx 版本）
    out[k++] = rot_xy_stream2(win_len, get_itlv, &axc, get_itlv, &ayc);
}

// ---------- 公开接口：环形缓冲视图 ----------
void feat_extract_from_circ3(const float* ring, int ring_len, int start, float* out) {
    // ring 为三轴交错的环形缓冲（长度= ring_len*3），start 为窗口起点（最近 WIN）
    circ_ctx_t axc = { ring, ring_len, start, 0 };
    circ_ctx_t ayc = { ring, ring_len, start, 1 };
    circ_ctx_t azc = { ring, ring_len, start, 2 };

    float ax[1024], ay[1024], az[1024];   // 假设 ring_len <= 1024
    for (int i = 0; i < ring_len; ++i) {
        ax[i] = get_circ(i, &axc);
        ay[i] = get_circ(i, &ayc);
        az[i] = get_circ(i, &azc);
    }

    int k = 0; float mean, var, rms, p2p_;
    stats(ax, ring_len, &mean, &var, &rms, &p2p_); out[k++] = mean; out[k++] = var; out[k++] = rms; out[k++] = p2p_; out[k++] = zcr(ax, ring_len);
    stats(ay, ring_len, &mean, &var, &rms, &p2p_); out[k++] = mean; out[k++] = var; out[k++] = rms; out[k++] = p2p_; out[k++] = zcr(ay, ring_len);
    stats(az, ring_len, &mean, &var, &rms, &p2p_); out[k++] = mean; out[k++] = var; out[k++] = rms; out[k++] = p2p_; out[k++] = zcr(az, ring_len);

    // 第16维：rot_xy（修正后的双 ctx 版本）
    out[k++] = rot_xy_stream2(ring_len, get_circ, &axc, get_circ, &ayc);
}
