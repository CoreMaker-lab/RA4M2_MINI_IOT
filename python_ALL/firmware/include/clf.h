#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// int8 稠密层（多类逻辑回归等价于线性分类）
typedef struct {
    const int8_t  *W;      // [OUT_DIM, IN_DIM] 行主序展平
    const int32_t *b;      // [OUT_DIM]，与输入缩放一致的量化偏置
    int IN_DIM;
    int OUT_DIM;
    float in_scale;        // 输入量化尺度（训练端导出）
} dense_i8_t;

// 推理：输入为 float 特征，内部量化为 int8 点积输出 argmax 类别索引
int infer_linear_i8(const dense_i8_t *m, const float *feat /* [IN_DIM] */);

// 从 weights.h 提供的常量构建模型视图（无需动态分配）
const dense_i8_t* get_trained_model(void);

#ifdef __cplusplus
}
#endif
