#ifndef ESP8266_H
#define ESP8266_H

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

#define MQTT_BROKER     "a1fabJdOLz0.iot-as-mqtt.cn-shanghai.aliyuncs.com"  // <--- 将此处替换为实际 MQTT 服务器地址/IP
#define MQTT_PORT       "1883"                // <--- 将此处替换为实际 MQTT 服务器端口号
#define MQTT_USER       "tHV3SyEhr3BrH7JwvMuq&a1fabJdOLz0"         // <--- 若 MQTT 需要用户名认证，填写用户名，否则留空
#define MQTT_PASS       "e1df04bd20bab2c47ee2457bac232122e094267874c3ef2a35108bea5ac70e34"         // <--- 若 MQTT 需要密码认证，填写密码，否则留空
#define MQTT_CLIENTID   "a1fabJdOLz0.tHV3SyEhr3BrH7JwvMuq|securemode=2\\,signmethod=hmacsha256\\,timestamp=1761066873178|"        // MQTT 客户端 ID
#define MQTT_SUB_TOPIC  "/a1fabJdOLz0/tHV3SyEhr3BrH7JwvMuq/user/get"       // 订阅的主题
#define MQTT_PUB_TOPIC  "/sys/a1fabJdOLz0/tHV3SyEhr3BrH7JwvMuq/thing/event/property/post"      // 发布的主题

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

bool ESP8266_MQTTPublish_Humidity(float humidity);

bool ESP8266_WaitResponse(const char * expected_1,
                          const char * expected_2,
                          uint32_t timeout_ms);
bool ESP8266_UART_Write(const uint8_t * data,
                        uint16_t length,
                        uint32_t timeout_ms);
bool ESP8266_WiFiInit(void);
bool ESP8266_TCP_Open(const char * server_ip,
                      uint16_t server_port);//建立TCP连接

bool ESP8266_TCP_Send(const uint8_t * data,
                      uint16_t length);//发送一包二进制数据

void ESP8266_TCP_Close(void);//关闭TCP连接

#ifdef __cplusplus
}
#endif

#endif  // ESP8266_H

