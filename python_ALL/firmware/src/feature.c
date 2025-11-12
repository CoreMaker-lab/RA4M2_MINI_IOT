#include "feature.h"
#include <math.h>
#include <string.h>

static void stats(const float *x, int n, float *mean, float *var, float *rms, float *pp) {
    float s=0.f, s2=0.f, mx=-1e30f, mn=1e30f;
    for (int i=0;i<n;i++){ float v=x[i]; s+=v; s2+=v*v; if(v>mx)mx=v; if(v<mn)mn=v; }
    *mean = s/n;
    float vv = s2/n - (*mean)*(*mean);
    *var = vv>0.f ? vv:0.f;
    *rms = sqrtf(s2/n);
    *pp  = mx - mn;
}
static float zcr(const float *x, int n){
    int c=0;
    for(int i=1;i<n;i++){
        float a=x[i-1], b=x[i];
        if ((a<=0 && b>0) || (a>=0 && b<0)) c++;
    }
    return (float)c/(n-1);
}

void feat_extract(const float *win, int N, float *feat){
    // 输出顺序：for axis in 0..5: mean,var,rms,pp,zcr
    int k=0;
    float buf[1024]; // WIN<=1024 （本模板 WIN=416）
    for(int ax=0;ax<6;ax++){
        for(int i=0;i<N;i++) buf[i]=win[i*6+ax];
        float mean,var,rms,pp;
        stats(buf,N,&mean,&var,&rms,&pp);
        feat[k++]=mean;
        feat[k++]=var;
        feat[k++]=rms;
        feat[k++]=pp;
        feat[k++]=zcr(buf,N);
    }
}
