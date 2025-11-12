#include "clf.h"
#include "weights.h"
#include <math.h>

static inline int8_t quantize(float x, float scale){
    int q = (int)lrintf(x/scale);
    if(q>127) q=127; if(q<-128) q=-128;
    return (int8_t)q;
}

int infer_linear_i8(const dense_i8_t *m, const float *feat){
    // 量化输入
    int8_t xq[512]; // 足够容纳 FEAT_DIM<=512
    for(int i=0;i<m->IN_DIM;i++) xq[i]=quantize(feat[i], m->in_scale);

    // y = W*x + b
    int best=-1; int bestv=-2147483647;
    for(int o=0;o<m->OUT_DIM;o++){
        const int8_t *wrow = m->W + o*m->IN_DIM;
        int acc = m->b[o];
        for(int i=0;i<m->IN_DIM;i++) acc += (int)wrow[i]*(int)xq[i];
        if(acc>bestv){ bestv=acc; best=o; }
    }
    return best;
}

// ---- link-time provided constants from weights.h ----
extern const int IN_DIM_;
extern const int OUT_DIM_;
extern const float IN_SCALE_;
extern const int8_t  W_Q_[];
extern const int32_t B_Q_[];

// 按 weights.h 中的导出构建只读模型
const dense_i8_t* get_trained_model(void){
    static dense_i8_t model = {0};
    model.W = W_Q_;
    model.b = B_Q_;
    model.IN_DIM = IN_DIM_;
    model.OUT_DIM= OUT_DIM_;
    model.in_scale = IN_SCALE_;
    return &model;
}
