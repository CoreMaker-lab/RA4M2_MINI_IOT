#ifndef HUAWEI_ESP8266_H
#define HUAWEI_ESP8266_H

#include "hal_data.h"    // 包含硬件抽象层数据结构（提供 UART4 和 GPT 定义）
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Wi-Fi 和 MQTT 参数配置 */
#define WIFI_SSID       "xh1"        // <--- 将此处替换为实际 Wi-Fi 名称
#define WIFI_PASSWORD   "11111111"    // <--- 将此处替换为实际 Wi-Fi 密码

#define MQTT_BROKER     "637b70af24.st1.iotda-device.cn-north-4.myhuaweicloud.com"  // <--- 将此处替换为实际 MQTT 服务器地址/IP
#define MQTT_PORT       "1883"                // <--- 将此处替换为实际 MQTT 服务器端口号
#define MQTT_USER       "69370912cbb0cf6bb9286d48_RA4M2_01"         // <--- 若 MQTT 需要用户名认证，填写用户名，否则留空
#define MQTT_PASS       "fbd536aa85d5d5b8b23f4628dccf9f98a9010c0dc9d4fba02c20b3f185e411e3"         // <--- 若 MQTT 需要密码认证，填写密码，否则留空
#define MQTT_CLIENTID   "69370912cbb0cf6bb9286d48_RA4M2_01_0_0_2025120817"        // MQTT 客户端 ID

#define MQTT_SUB_TOPIC  "$oc/devices/69370912cbb0cf6bb9286d48_RA4M2_01/sys/messages/down"       // 订阅的主题
#define MQTT_PUB_TOPIC  "$oc/devices/69370912cbb0cf6bb9286d48_RA4M2_01/sys/properties/report"      // 发布的主题


/* 声明与 UART4 接收缓冲相关的外部变量（由 hal_entry.c 定义并使用） */
#define WIFI_RX_BUF_SZ  1024
extern uint8_t  wifi_rb[WIFI_RX_BUF_SZ];          // UART4 接收缓冲区
extern  uint16_t RxLine;                  // 当前已接收的数据长度
extern  uint8_t  Rx_flag_finish;          // 接收完成标志（由 GPT 定时器超时回调置位）
extern  uint8_t  gpt0_flag;               // GPT0 定时标志（定时中断置位）
extern  bool     uart4_tx_flag;           // UART4 发送完成标志（UART4 TX 回调置位）

/* 函数声明 */
void ESP8266_Init(void);
void ESP8266_Loop(void);
bool ESP8266_SetWiFiMode(void);
bool ESP8266_ConnectWiFi(void);
bool ESP8266_MQTTUserConfig(void);
bool ESP8266_MQTTConnect(void);
bool ESP8266_MQTTSubscribe(void);
bool ESP8266_MQTTPublish(float temperature);
bool ESP8266_SendCmdWaitOk(const char *cmd_s, uint32_t timeout_ms);
#ifdef __cplusplus
}
#endif

#endif  // HUAWEI_ESP8266_H

