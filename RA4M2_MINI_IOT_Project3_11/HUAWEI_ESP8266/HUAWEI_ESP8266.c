#include "HUAWEI_ESP8266.h"

 char cmd[512];

/**
 * 向 ESP8266 发送 AT 指令并等待返回 "OK"
 * @param cmd      要发送的 AT 指令字符串（需以\r\n结尾）
 * @param timeout_ms 最长等待时间（毫秒）
 * @return true 表示收到 "OK"，false 表示超时或收到 "ERROR"
 */
bool ESP8266_SendCmdWaitOk(const char *cmd_s, uint32_t timeout_ms)
{
    fsp_err_t err;
    volatile uint32_t wait_time = 0;

    RxLine = 0;
    memset(wifi_rb, 0, WIFI_RX_BUF_SZ);

    // 通过 UART4 发送 AT 指令
    err = R_SCI_UART_Write(&g_uart4_ctrl, (uint8_t *)cmd_s, strlen(cmd_s));
    printf("len=%d\n",strlen(cmd_s));
    if (err != FSP_SUCCESS)
    {
        printf("UART4 command send failed: %s\n", cmd_s);
        return false;
    }
    // 清除先前的接收标志和计数，清空接收缓冲区内容
    Rx_flag_finish = 0;
    // 如果需要，可以等待 UART4 发送完成标志（由回调置位）
    // while(uart4_tx_flag == false) { /* 等待发送完毕 */ }
    // uart4_tx_flag = false;
    bool result = false;
    // 等待 ESP8266 返回信息，直到检测到 Rx_flag_finish 或超时
    while (wait_time < timeout_ms)
    {
        // 检查是否收到 "OK"
        if (strstr((char *)wifi_rb, "OK") != NULL)
        {
            printf("Command executed successfully\n");
            result = true;
            break;
        }
        // 检查是否收到 "ERROR" 或 "FAIL"
        if (strstr((char *)wifi_rb, "ERROR") != NULL || strstr((char *)wifi_rb, "FAIL") != NULL)
        {
            printf("Command execution failed\n");
            // 可选：打印详细信息 printf("指令执行失败: %s 响应: %s\n", cmd, (char*)wifi_rb);
            result = false;
            break;
        }


        R_BSP_SoftwareDelay(1, BSP_DELAY_UNITS_MILLISECONDS);  // 每次等待10ms
        wait_time += 1;
    }

    // 如果循环因为超时退出（未找到任何标志）
    if (wait_time >= timeout_ms)
    {
        printf("Timeout waiting for ESP8266 response: %s\n", cmd_s);
        result = false;
    }
    return result;
}

/**
 * 设置 ESP8266 Wi-Fi 模式为 Station 模式 (模式1)
 */
bool ESP8266_SetWiFiMode(void)
{
    // AT+CWMODE=1 将 Wi-Fi 模式设置为 Station 模式
//    const char *cmd = "AT+CWMODE=1\r\n";
    sprintf(cmd, "AT+CWMODE=1\r\n");
    printf("Set WiFi Mode Station...\n");
    printf("MCU->esp8266:--------------%s", cmd);
    bool RT;
    RT=ESP8266_SendCmdWaitOk(cmd, 10000);
    memset(cmd,0,sizeof(cmd));  //清空缓存数组
    return RT;
}

/**
 * 连接指定的 Wi-Fi 热点 (AP)
 */
bool ESP8266_ConnectWiFi(void)
{
//    char cmd[256];
    // 构造 AT+CWJAP 命令: 连接 Wi-Fi，带SSID和密码
    sprintf(cmd, "AT+CWJAP=\"%s\",\"%s\"\r\n", WIFI_SSID, WIFI_PASSWORD);
    printf("MCU->esp8266:--------------%s", cmd);
    printf("Connect to WiFi: SSID=%s ...\n", WIFI_SSID);
    bool RT;
    // Wi-Fi 连接可能需要几秒，设置较长的超时时间（例如 15 秒）
    RT=ESP8266_SendCmdWaitOk(cmd, 15000);
    memset(cmd,0,sizeof(cmd));  //清空缓存数组
    return RT;
}

/**
 * 配置 MQTT 用户参数（包括 ClientID、用户名、密码等）
 * 使用 AT+MQTTUSERCFG 指令
 */
bool ESP8266_MQTTUserConfig(void)
{
//    char cmd[400];
    // 构造 AT+MQTTUSERCFG 命令:
    // <LinkID>=0, <scheme>=1 (MQTT over TCP), <client_id>, <username>, <password>, <cert_key_ID>=0, <CA_ID>=0, <path>=""
    sprintf(cmd, "AT+MQTTUSERCFG=0,1,\"%s\",\"%s\",\"%s\",0,0,\"\"\r\n",
            MQTT_CLIENTID,MQTT_USER, MQTT_PASS);
    printf("MCU->esp8266:--------------%s", cmd);
    printf("Configure MQTT user parameters...\n");
    bool RT;
    RT=ESP8266_SendCmdWaitOk(cmd, 10000);
    memset(cmd,0,sizeof(cmd));  //清空缓存数组
    return RT;
}





/**
 * 连接到 MQTT 服务器
 * 使用 AT+MQTTCONN 指令
 */
bool ESP8266_MQTTConnect(void)
{
//    char cmd[512];
    // 构造 AT+MQTTCONN 命令: 使用上一步配置的参数连接 MQTT Broker
    // <reconnect>参数设为0表示不自动重连
    sprintf(cmd,"AT+MQTTCONN=0,\"%s\",%s,1\r\n", MQTT_BROKER, MQTT_PORT);
    printf("MCU->esp8266:--------------%s", cmd);
    printf("Configure MQTT user parameters %s:%s ...\n", MQTT_BROKER, MQTT_PORT);
    // MQTT 建立连接可能稍有延迟，超时设为10秒
    bool RT;
    RT=ESP8266_SendCmdWaitOk(cmd, 10000);
    memset(cmd,0,sizeof(cmd));  //清空缓存数组
    return RT;
}

/**
 * 订阅 MQTT 主题
 * 使用 AT+MQTTSUB 指令订阅预定义主题
 */
bool ESP8266_MQTTSubscribe(void)
{
//    char cmd[256];
    // 构造 AT+MQTTSUB 命令: 订阅主题，QoS=0
    sprintf(cmd, "AT+MQTTSUB=0,\"%s\",1\r\n", MQTT_SUB_TOPIC);
    printf("MCU->esp8266:--------------%s", cmd);
    printf("Subscribe to MQTT Topic: %s ...\n", MQTT_SUB_TOPIC);
    bool RT;
    RT=ESP8266_SendCmdWaitOk(cmd, 10000);
    memset(cmd,0,sizeof(cmd));  //清空缓存数组
    return RT;
}

/**
 * 发布 MQTT 消息（温度数据）
 * @param temperature 要发布的温度值 (float)
 * 使用 AT+MQTTPUB 指令发布到预定义主题
 */
bool ESP8266_MQTTPublish(float temperature)
{
//    char cmd[256];
    char payload[128];
    char temp_buf[16];

    // 使用 sprintf 将温度值转换为字符串（保留两位小数）
    sprintf(temp_buf, "%.2f", temperature);

    // 构造 JSON 格式的消息体
    sprintf(payload, "{\"params\":{\"temperature\":%s}}\r\n", temp_buf);
    sprintf(payload, "{\"services\":[{\"service_id\":\"temp\",\"properties\":{\"RA4M2_temp\":%s}}]}\r\n", temp_buf);
    // 将温度值转换为字符串形式（保留两位小数）
//    snprintf(temp_str, sizeof(temp_str), "%.2f", temperature);
    // 构造 AT+MQTTPUB 指令: 发布消息，QoS=0，retain=0
    sprintf(cmd, "AT+MQTTPUBRAW=0,\"%s\",70,1,0\r\n", MQTT_PUB_TOPIC);
    printf("MCU->esp8266:--------------%s", cmd);
    printf("Publish Temperature Data: %.2f to topic %s\n", temperature, MQTT_PUB_TOPIC);
    bool RT;
    RT=ESP8266_SendCmdWaitOk(cmd, 10000);

    if(RT==true)
    {
        // 发布消息体
        ESP8266_SendCmdWaitOk(payload, 10000);
        printf("MCU->esp8266:--------------%s", payload);
        printf("Publish temperature data: %s to topic %s\n", temp_buf, MQTT_PUB_TOPIC);
        return true;


    }
    else
    {
        printf("Failed to send MQTT publish command.\n");
        return false;
    }
    memset(cmd,0,sizeof(cmd));  //清空缓存数组
    return RT;

}


bool ESP8266_MQTTPublish_Humidity(float humidity)
{
    char payload[128];
    char hum_buf[16];

    // 湿度转字符串，保留两位小数
    sprintf(hum_buf, "%.2f", humidity);

    // 简化 JSON：只带一个 params.Humidity
    // 实际上发出去就是：{"params":{"Humidity":21.80}}
//    sprintf(payload, "{\"params\":{\"Humidity\":%s}}\r\n", hum_buf);
    sprintf(payload, "{\"services\":[{\"service_id\":\"temp\",\"properties\":{\"Humidity\":%s}}]}\r\n", hum_buf);

    // 这里可以和温度一样先写死长度 32，也可以算真实长度
    // 建议用真实长度，稍微稳一点：
    int len = (int)strlen(payload);
    sprintf(cmd, "AT+MQTTPUBRAW=0,\"%s\",%d,1,0\r\n", MQTT_PUB_TOPIC, len);

    printf("MCU->esp8266:--------------%s", cmd);
    printf("Publish Humidity Data: %.2f to topic %s\n", humidity, MQTT_PUB_TOPIC);
    bool RT;
    RT=ESP8266_SendCmdWaitOk(cmd, 10000);
    if (RT == true)
    {
        // 发布消息体
        ESP8266_SendCmdWaitOk(payload, 10000);
        printf("MCU->esp8266:--------------%s", payload);
        printf("Publish humidity data: %s to topic %s\n", hum_buf, MQTT_PUB_TOPIC);
        return true;
    }
    else
    {
        printf("Failed to send MQTT publish command (humidity).\n");
        return false;
    }

    memset(cmd, 0, sizeof(cmd));  //清空缓存数组
    return RT;
}


/**
 * ESP8266 模块初始化函数：
 * 顺序调用上述封装的函数以完成模块上电后的初始化和连接过程。
 * 包括设置WiFi模式、连接WiFi、配置MQTT参数、连接MQTT服务器和订阅主题。
 */
void ESP8266_Init(void)
{
    bool ok;
    //初始化 ESP8266 模块
    printf("Initialize ESP8266 module...\n");
    printf("ESP8266_SetWiFiMode\n");
    ok = ESP8266_SetWiFiMode();
    if (!ok) {
        //ESP8266_SetWiFiMode 失败！停止初始化。
        printf("ESP8266_SetWiFiMode failed! Initialization aborted.\n");
        return;
    }
    printf("ESP8266_ConnectWiFi\n");
    ok = ESP8266_ConnectWiFi();
    if (!ok) {
        //ESP8266_ConnectWiFi 失败！停止初始化。
        printf("ESP8266_ConnectWiFi failed! Initialization aborted.\n");
        return;
    }
    printf("ESP8266_MQTTUserConfig\n");
    ok = ESP8266_MQTTUserConfig();
    if (!ok) {
        //ESP8266_MQTTUserConfig 失败！停止初始化。
        printf("ESP8266_MQTTUserConfig failed! Initialization aborted.\n");
        return;
    }




    printf("ESP8266_MQTTConnect\n");
    ok = ESP8266_MQTTConnect();
    if (!ok) {
        //ESP8266_MQTTConnect 失败！停止初始化。
        printf("ESP8266_MQTTConnect failed! Initialization aborted.\n");
        return;
    }
    printf("ESP8266_MQTTSubscribe\n");
    ok = ESP8266_MQTTSubscribe();
    if (!ok) {
        //ESP8266_MQTTSubscribe 失败！
        printf("ESP8266_MQTTSubscribe failed!\n");
        // 订阅失败不一定影响发布，可继续
    }
    //ESP8266 初始化和连接流程完成。
    printf("ESP8266 initialization and connection process completed.\n");
}





