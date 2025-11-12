#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// 简洁特征：每轴 5 个（mean/var/rms/pp/zcr）×6轴 = 30 维
#define FEAT_DIM 30

void feat_extract(const float *win /* [N*6] */, int N, float *feat /* [FEAT_DIM] */);

#ifdef __cplusplus
}
#endif
