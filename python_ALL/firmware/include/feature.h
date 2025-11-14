#ifndef FEATURE_H_
#define FEATURE_H_

#ifdef __cplusplus
extern "C" {
#endif

    /* ------------------------------------------------------------
     *  Feature definition
     *  - 3-axis accelerometer window -> 16-D features:
     *    [mean,var,rms,peak2peak,zcr] × (ax, ay, az)  = 15
     *    + rot_xy (mean(ax[:-1]*dAy - ay[:-1]*dAx))   =  1
     * ------------------------------------------------------------ */
#define FEATURE_DIM  (16)

     /* ------------------------------------------------------------
      *  APIs
      *  1) feat_extract:
      *     输入为交错排列的一段窗口: [ax0, ay0, az0, ax1, ay1, az1, ...]
      *     - win_len: 该窗口包含的采样点数（每点含3轴）
      *     - out: 输出 16 维特征 (float[FEATURE_DIM])
      *
      *  2) feat_extract_from_circ3:
      *     输入为 3轴交错的环形缓冲区（长度 = ring_len * 3），
      *     以及该窗口的起始下标 start（指向窗口中最旧样本）。
      *     - ring:   环形缓冲基址（交错存放 ax/ay/az）
      *     - ring_len: 环形缓冲中“每轴”的数据量（=窗口长度）
      *     - start:   当前窗口的起点（相对 ring 的索引，0..ring_len-1）
      *     - out:     输出 16 维特征
      * ------------------------------------------------------------ */

      /**
       * @brief 从交错窗口提取 16 维特征
       * @param win      [in] 交错序列 (长度 = win_len * 3)
       * @param win_len  [in] 窗口长度（采样点数）
       * @param out      [out] float[FEATURE_DIM]
       */
    void feat_extract(const float* win, int win_len, float* out);

    /**
     * @brief 从 3 轴交错的环形缓冲视图提取 16 维特征
     * @param ring      [in] 交错环形缓冲 (长度 = ring_len * 3)
     * @param ring_len  [in] 窗口长度（采样点数）
     * @param start     [in] 窗口起点（最旧样本在 ring 中的索引）
     * @param out       [out] float[FEATURE_DIM]
     */
    void feat_extract_from_circ3(const float* ring, int ring_len, int start, float* out);

    /* ------------------------------------------------------------
     *  Optional: 编译期一致性检查宏（放在任意 .c 里调用）
     *  用法：
     *      #include "feature.h"
     *      #include "weights.h"
     *      FEATURE_STATIC_ASSERT_DIM_MATCH(IN_DIM_)
     * ------------------------------------------------------------ */
#if !defined(FEATURE_STATIC_ASSERT_DIM_MATCH)
#if __STDC_VERSION__ >= 201112L
#define FEATURE_STATIC_ASSERT_DIM_MATCH(IN_DIM_MACRO) \
        _Static_assert(FEATURE_DIM == (IN_DIM_MACRO), "FEATURE_DIM must match model input dim");
#else
#define FEATURE_STATIC_ASSERT_DIM_MATCH(IN_DIM_MACRO) \
        typedef char feature_dim_must_match[(FEATURE_DIM==(IN_DIM_MACRO))?1:-1]
#endif
#endif

#ifdef __cplusplus
} /* extern "C" */
#endif
#endif /* FEATURE_H_ */
