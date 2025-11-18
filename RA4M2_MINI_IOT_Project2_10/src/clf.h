#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

    typedef struct {
        int IN_DIM;
        int OUT_DIM;
        float IN_SCALE;
        const int8_t* W_Q;  // [OUT_DIM * IN_DIM] row-major
        const int32_t* B_Q;  // [OUT_DIM]
    } dense_i8_t;

    /* 由 weights.h 提供的量化权重构建出的模型 */
    const dense_i8_t* get_trained_model(void);

    /* 量化线性分类（与 Python/MCU 推理一致）
     * feat_f32: FEATURE_DIM 浮点特征
     * 返回 argmax 类别索引
     */
    int infer_linear_i8(const dense_i8_t* model, const float feat_f32[]);

#ifdef __cplusplus
}
#endif
