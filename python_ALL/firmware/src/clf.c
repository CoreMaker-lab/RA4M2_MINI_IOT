#include "clf.h"
#include "feature.h"
#include "weights.h"
#include <math.h>

/* 把 weights.h 中导出的符号封装成一个模型句柄 */
static const dense_i8_t g_model = {
    .IN_DIM = IN_DIM_,
    .OUT_DIM = OUT_DIM_,
    .IN_SCALE = IN_SCALE_,
    .W_Q = W_Q_,
    .B_Q = B_Q_,
};

static inline int8_t sat_int8(float x) {
    int xi = (int)lrintf(x);
    if (xi > 127)  xi = 127;
    if (xi < -128) xi = -128;
    return (int8_t)xi;
}

const dense_i8_t* get_trained_model(void) {
    return &g_model;
}

int infer_linear_i8(const dense_i8_t* m, const float feat_f32[]) {
    // 1) 输入量化
    int8_t x_q[FEATURE_DIM];
    float inv = (m->IN_SCALE > 1e-12f) ? (1.0f / m->IN_SCALE) : 1.0f;
    for (int i = 0; i < m->IN_DIM; ++i) {
        x_q[i] = sat_int8(feat_f32[i] * inv);
    }

    // 2) int32 logits = W_q * x_q + B_q
    int best = 0;
    int32_t bestv = INT32_MIN;
    for (int c = 0; c < m->OUT_DIM; ++c) {
        const int8_t* wrow = m->W_Q + c * m->IN_DIM;
        int32_t acc = m->B_Q[c];
        for (int i = 0; i < m->IN_DIM; ++i) {
            acc += (int32_t)wrow[i] * (int32_t)x_q[i];
        }
        if (acc > bestv) { bestv = acc; best = c; }
    }
    return best;
}
