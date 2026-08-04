#include "ESP8266.h"



char cmd[512];

/* ---------- TCP被动接收内部状态 ---------- */
#define ESP8266_TCP_MAX_READ_SIZE       512U
#define ESP8266_TCP_LINE_READ_CHUNK     96U
#define ESP8266_TCP_PENDING_SIZE        ESP8266_TCP_LINE_READ_CHUNK
#define ESP8266_TCP_RECV_QUERY_MS       1000U

static uint8_t  s_tcp_pending[ESP8266_TCP_PENDING_SIZE];
static uint16_t s_tcp_pending_pos = 0U;
static uint16_t s_tcp_pending_len = 0U;

 /**
  * 向 ESP8266 发送 AT 指令并等待返回 "OK"
  * @param cmd      要发送的 AT 指令字符串（需以\r\n结尾）
  * @param timeout_ms 最长等待时间（毫秒）
  * @return true 表示收到 "OK"，false 表示超时或收到 "ERROR"
  */
 static void ESP8266_ClearRxBuffer(void)
 {
     RxLine = 0U;
     Rx_flag_finish = 0U;
     memset((void *) wifi_rb, 0, WIFI_RX_BUF_SZ);
 }

 static void ESP8266_TCP_ResetPending(void)
 {
     s_tcp_pending_pos = 0U;
     s_tcp_pending_len = 0U;
     memset(s_tcp_pending, 0, sizeof(s_tcp_pending));
 }

 static int32_t ESP8266_FindBytes(const uint8_t * data,
                                  uint16_t data_length,
                                  const uint8_t * pattern,
                                  uint16_t pattern_length,
                                  uint16_t start_index)
 {
     if ((NULL == data) ||
         (NULL == pattern) ||
         (0U == pattern_length) ||
         (data_length < pattern_length) ||
         (start_index >= data_length))
     {
         return -1;
     }

     for (uint16_t i = start_index;
          ((uint32_t) i + pattern_length) <= data_length;
          i++)
     {
         if (0 == memcmp(&data[i], pattern, pattern_length))
         {
             return (int32_t) i;
         }
     }

     return -1;
 }

 static bool ESP8266_TCP_SavePending(const uint8_t * data,
                                     uint16_t length)
 {
     if (0U == length)
     {
         return true;
     }

     if ((NULL == data) ||
         (length > ESP8266_TCP_PENDING_SIZE))
     {
         return false;
     }

     memcpy(s_tcp_pending, data, length);
     s_tcp_pending_pos = 0U;
     s_tcp_pending_len = length;

     return true;
 }

 static uint16_t ESP8266_TCP_ReadPending(uint8_t * buffer,
                                         uint16_t request_length)
 {
     if ((NULL == buffer) ||
         (0U == request_length) ||
         (s_tcp_pending_pos >= s_tcp_pending_len))
     {
         return 0U;
     }

     uint16_t pending_length =
         (uint16_t) (s_tcp_pending_len - s_tcp_pending_pos);

     uint16_t copy_length =
         (pending_length > request_length) ?
         request_length :
         pending_length;

     memcpy(buffer,
            &s_tcp_pending[s_tcp_pending_pos],
            copy_length);

     s_tcp_pending_pos =
         (uint16_t) (s_tcp_pending_pos + copy_length);

     if (s_tcp_pending_pos >= s_tcp_pending_len)
     {
         ESP8266_TCP_ResetPending();
     }

     return copy_length;
 }

 static bool ESP8266_BufferContains(const char * expected)
 {
     if ((NULL == expected) || ('\0' == expected[0]))
     {
         return false;
     }

     /*
      * UART4 回调始终在当前数据末尾补 '\0'，
      * 因此这里可以按字符串查找 AT 响应。
      */
     return (NULL != strstr((const char *) wifi_rb, expected));
 }

 bool ESP8266_UART_Write(const uint8_t * data,
                         uint16_t length,
                         uint32_t timeout_ms)
 {
     if ((NULL == data) || (0U == length))
     {
         return false;
     }

     uart4_tx_flag = false;

     fsp_err_t uart_err =
         R_SCI_UART_Write(&g_uart4_ctrl, data, length);

     if (FSP_SUCCESS != uart_err)
     {
         printf("UART4 write failed, err=%d\r\n", (int) uart_err);
         return false;
     }

     uint32_t elapsed_ms = 0U;

     while (!uart4_tx_flag)
     {
         if (elapsed_ms >= timeout_ms)
         {
             printf("UART4 TX timeout.\r\n");
             return false;
         }

         R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
         elapsed_ms++;
     }

     uart4_tx_flag = false;
     return true;
 }

 bool ESP8266_WaitResponse(const char * expected_1,
                           const char * expected_2,
                           uint32_t timeout_ms)
 {
     uint32_t elapsed_ms = 0U;

     while (elapsed_ms < timeout_ms)
     {
         if (ESP8266_BufferContains(expected_1) ||
             ESP8266_BufferContains(expected_2))
         {
             return true;
         }

         if (ESP8266_BufferContains("ERROR") ||
             ESP8266_BufferContains("FAIL") ||
             ESP8266_BufferContains("busy p") ||
             ESP8266_BufferContains("link is not valid"))
         {
             printf("ESP8266 error response: %.*s\r\n",
                    (int) RxLine,
                    (const char *) wifi_rb);
             return false;
         }

         R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
         elapsed_ms++;
     }

     printf("ESP8266 response timeout: %.*s\r\n",
            (int) RxLine,
            (const char *) wifi_rb);
     return false;
 }

 bool ESP8266_SendCmdWaitResponse(const char * cmd_s,
                                  const char * expected_1,
                                  const char * expected_2,
                                  uint32_t timeout_ms)
 {
     if (NULL == cmd_s)
     {
         return false;
     }

     ESP8266_ClearRxBuffer();

     if (!ESP8266_UART_Write((const uint8_t *) cmd_s,
                             (uint16_t) strlen(cmd_s),
                             3000U))
     {
         return false;
     }

     if (!ESP8266_WaitResponse(expected_1,
                               expected_2,
                               timeout_ms))
     {
         printf("AT command failed: %s", cmd_s);
         return false;
     }

     return true;
 }



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
    sprintf(cmd, "AT+MQTTUSERCFG=0,1,\"NULL\",\"%s\",\"%s\",0,0,\"\"\r\n",
             MQTT_USER, MQTT_PASS);
    printf("MCU->esp8266:--------------%s", cmd);
    printf("Configure MQTT user parameters...\n");
    bool RT;
    RT=ESP8266_SendCmdWaitOk(cmd, 10000);
    memset(cmd,0,sizeof(cmd));  //清空缓存数组
    return RT;
}


bool ESP8266_MQTTClientIDConfig(void)
{
    sprintf(cmd, "AT+MQTTCLIENTID=0,\"%s\"\r\n", MQTT_CLIENTID);
    printf("MCU->esp8266:--------------%s", cmd);
    printf("Configure MQTT client ID...\n");
    bool RT;
    RT = ESP8266_SendCmdWaitOk(cmd, 10000);
    memset(cmd, 0, sizeof(cmd));  // 清空缓存数组
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

    // 将温度值转换为字符串形式（保留两位小数）
//    snprintf(temp_str, sizeof(temp_str), "%.2f", temperature);
    // 构造 AT+MQTTPUB 指令: 发布消息，QoS=0，retain=0
    sprintf(cmd, "AT+MQTTPUBRAW=0,\"%s\",32,1,0\r\n", MQTT_PUB_TOPIC);
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
    sprintf(payload, "{\"params\":{\"Humidity\":%s}}\r\n", hum_buf);

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
    printf("ESP8266_MQTTClientIDConfig\n");
    ok = ESP8266_MQTTClientIDConfig();
    if (!ok) {
        printf("ESP8266_MQTTClientIDConfig failed! Initialization aborted.\n");
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

static bool ESP8266_TCP_GetRecvLength(uint32_t * available_length,
                                       uint32_t timeout_ms)
{
    static const char command[] = "AT+CIPRECVLEN?\r\n";
    static const char prefix[]  = "+CIPRECVLEN:";

    if (NULL == available_length)
    {
        return false;
    }

    *available_length = 0U;

    ESP8266_ClearRxBuffer();

    if (!ESP8266_UART_Write((const uint8_t *) command,
                            (uint16_t) (sizeof(command) - 1U),
                            3000U))
    {
        return false;
    }

    uint32_t elapsed_ms = 0U;

    while (elapsed_ms < timeout_ms)
    {
        char * response =
            strstr((char *) wifi_rb, prefix);

        if (NULL != response)
        {
            response += strlen(prefix);

            if ((*response >= '0') &&
                (*response <= '9'))
            {
                char * end_pointer = NULL;
                unsigned long value =
                    strtoul(response, &end_pointer, 10);

                if ((end_pointer != response) &&
                    ESP8266_BufferContains("OK"))
                {
                    *available_length = (uint32_t) value;
                    return true;
                }
            }
        }

        if (ESP8266_BufferContains("ERROR") ||
            ESP8266_BufferContains("FAIL") ||
            ESP8266_BufferContains("link is not valid"))
        {
            printf("Query TCP receive length failed.\r\n");
            return false;
        }

        R_BSP_SoftwareDelay(1U,
                            BSP_DELAY_UNITS_MILLISECONDS);
        elapsed_ms++;
    }

    return false;
}

static bool ESP8266_TCP_WaitData(uint32_t * available_length,
                                 uint32_t timeout_ms)
{
    if (NULL == available_length)
    {
        return false;
    }

    /*
     * 先主动查询一次，避免+IPD提示已经被其他AT响应覆盖。
     */
    if (ESP8266_TCP_GetRecvLength(available_length,
                                  ESP8266_TCP_RECV_QUERY_MS) &&
        (*available_length > 0U))
    {
        return true;
    }

    ESP8266_ClearRxBuffer();

    uint32_t elapsed_ms = 0U;
    uint32_t query_elapsed_ms = 0U;

    while (elapsed_ms < timeout_ms)
    {
        /*
         * 单连接被动接收模式下，新数据到达时ESP8266提示：
         * +IPD,<len>
         */
        if (ESP8266_BufferContains("+IPD,"))
        {
            if (ESP8266_TCP_GetRecvLength(available_length,
                                          ESP8266_TCP_RECV_QUERY_MS) &&
                (*available_length > 0U))
            {
                return true;
            }

            ESP8266_ClearRxBuffer();
            query_elapsed_ms = 0U;
        }

        if (ESP8266_BufferContains("CLOSED") ||
            ESP8266_BufferContains("link is not valid"))
        {
            printf("TCP connection closed while waiting data.\r\n");
            return false;
        }

        /*
         * 每隔约1秒查询一次缓存长度。
         * 即使+IPD提示被清除，也能恢复接收。
         */
        if (query_elapsed_ms >= 1000U)
        {
            if (ESP8266_TCP_GetRecvLength(available_length,
                                          ESP8266_TCP_RECV_QUERY_MS) &&
                (*available_length > 0U))
            {
                return true;
            }

            ESP8266_ClearRxBuffer();
            query_elapsed_ms = 0U;
        }

        R_BSP_SoftwareDelay(1U,
                            BSP_DELAY_UNITS_MILLISECONDS);

        elapsed_ms++;
        query_elapsed_ms++;
    }

    printf("Wait TCP data timeout.\r\n");
    return false;
}

static bool ESP8266_TCP_ReadRaw(uint8_t * buffer,
                                uint16_t request_length,
                                uint16_t * actual_length,
                                uint32_t timeout_ms)
{
    static const uint8_t response_prefix[] =
        "+CIPRECVDATA:";

    static const uint8_t response_ok[] =
        "\r\nOK\r\n";

    char command[40];

    if ((NULL == buffer) ||
        (NULL == actual_length) ||
        (0U == request_length) ||
        (request_length > ESP8266_TCP_MAX_READ_SIZE))
    {
        return false;
    }

    *actual_length = 0U;

    int command_length =
        snprintf(command,
                 sizeof(command),
                 "AT+CIPRECVDATA=%u\r\n",
                 (unsigned int) request_length);

    if ((command_length <= 0) ||
        ((size_t) command_length >= sizeof(command)))
    {
        return false;
    }

    ESP8266_ClearRxBuffer();

    if (!ESP8266_UART_Write((const uint8_t *) command,
                            (uint16_t) command_length,
                            3000U))
    {
        return false;
    }

    uint32_t elapsed_ms = 0U;
    int32_t prefix_index = -1;
    uint16_t data_index = 0U;
    uint16_t received_length = 0U;
    bool length_parsed = false;

    while (elapsed_ms < timeout_ms)
    {
        uint16_t rx_length = RxLine;

        if (!length_parsed)
        {
            prefix_index =
                ESP8266_FindBytes(
                    wifi_rb,
                    rx_length,
                    response_prefix,
                    (uint16_t) (sizeof(response_prefix) - 1U),
                    0U);

            if (prefix_index >= 0)
            {
                uint16_t number_index =
                    (uint16_t) prefix_index +
                    (uint16_t) (sizeof(response_prefix) - 1U);

                uint32_t parsed_length = 0U;
                bool has_digit = false;
                bool has_comma = false;

                while (number_index < rx_length)
                {
                    uint8_t value = wifi_rb[number_index];

                    if ((value >= (uint8_t) '0') &&
                        (value <= (uint8_t) '9'))
                    {
                        has_digit = true;
                        parsed_length =
                            (parsed_length * 10U) +
                            (uint32_t) (value - (uint8_t) '0');

                        if (parsed_length >
                            ESP8266_TCP_MAX_READ_SIZE)
                        {
                            printf("Invalid TCP receive length.\r\n");
                            return false;
                        }
                    }
                    else if ((uint8_t) ',' == value)
                    {
                        has_comma = true;
                        data_index =
                            (uint16_t) (number_index + 1U);
                        break;
                    }
                    else
                    {
                        printf("Invalid CIPRECVDATA response.\r\n");
                        return false;
                    }

                    number_index++;
                }

                if (has_digit && has_comma)
                {
                    received_length =
                        (uint16_t) parsed_length;
                    length_parsed = true;
                }
            }
            else
            {
                static const uint8_t error_text[] = "ERROR";

                if (ESP8266_FindBytes(
                        wifi_rb,
                        rx_length,
                        error_text,
                        (uint16_t) (sizeof(error_text) - 1U),
                        0U) >= 0)
                {
                    printf("AT+CIPRECVDATA returned ERROR.\r\n");
                    return false;
                }
            }
        }

        if (length_parsed)
        {
            uint32_t data_end =
                (uint32_t) data_index +
                (uint32_t) received_length;

            if (data_end <= rx_length)
            {
                int32_t ok_index =
                    ESP8266_FindBytes(
                        wifi_rb,
                        rx_length,
                        response_ok,
                        (uint16_t) (sizeof(response_ok) - 1U),
                        (uint16_t) data_end);

                if (ok_index >= 0)
                {
                    if (received_length > request_length)
                    {
                        printf("TCP receive length exceeds request.\r\n");
                        return false;
                    }

                    if (received_length > 0U)
                    {
                        memcpy(buffer,
                               &wifi_rb[data_index],
                               received_length);
                    }

                    *actual_length = received_length;
                    return true;
                }
            }
        }

        R_BSP_SoftwareDelay(1U,
                            BSP_DELAY_UNITS_MILLISECONDS);
        elapsed_ms++;
    }

    printf("AT+CIPRECVDATA timeout, rx=%u.\r\n",
           (unsigned int) RxLine);

    return false;
}

bool ESP8266_TCP_Receive(uint8_t * buffer,
                         uint16_t request_length,
                         uint16_t * actual_length,
                         uint32_t timeout_ms)
{
    if ((NULL == buffer) ||
        (NULL == actual_length) ||
        (0U == request_length) ||
        (request_length > ESP8266_TCP_MAX_READ_SIZE))
    {
        return false;
    }

    *actual_length = 0U;

    /*
     * ReceiveLine可能已经读取到换行符后的部分PCM，
     * 先返回内部暂存数据。
     */
    uint16_t pending_length =
        ESP8266_TCP_ReadPending(buffer,
                                request_length);

    if (pending_length > 0U)
    {
        *actual_length = pending_length;
        return true;
    }

    uint32_t available_length = 0U;

    if (!ESP8266_TCP_WaitData(&available_length,
                              timeout_ms))
    {
        return false;
    }

    uint16_t read_length =
        (available_length > request_length) ?
        request_length :
        (uint16_t) available_length;

    if (0U == read_length)
    {
        return false;
    }

    return ESP8266_TCP_ReadRaw(buffer,
                               read_length,
                               actual_length,
                               timeout_ms);
}

bool ESP8266_TCP_ReceiveLine(char * buffer,
                             uint16_t buffer_size,
                             uint32_t timeout_ms)
{
    uint8_t receive_data[ESP8266_TCP_LINE_READ_CHUNK];
    uint16_t line_length = 0U;

    if ((NULL == buffer) ||
        (buffer_size < 2U))
    {
        return false;
    }

    buffer[0] = '\0';

    while (line_length < (uint16_t) (buffer_size - 1U))
    {
        uint16_t actual_length = 0U;

        if (!ESP8266_TCP_Receive(receive_data,
                                 sizeof(receive_data),
                                 &actual_length,
                                 timeout_ms))
        {
            return false;
        }

        for (uint16_t i = 0U;
             i < actual_length;
             i++)
        {
            if ((uint8_t) '\n' == receive_data[i])
            {
                /*
                 * 去掉协议行末尾可选的\r。
                 */
                if ((line_length > 0U) &&
                    ('\r' == buffer[line_length - 1U]))
                {
                    line_length--;
                }

                buffer[line_length] = '\0';

                uint16_t remaining =
                    (uint16_t) (actual_length - i - 1U);

                if (remaining > 0U)
                {
                    if (!ESP8266_TCP_SavePending(
                            &receive_data[i + 1U],
                            remaining))
                    {
                        printf("TCP pending buffer overflow.\r\n");
                        return false;
                    }
                }

                return true;
            }

            if (line_length >=
                (uint16_t) (buffer_size - 1U))
            {
                printf("TCP line buffer is too small.\r\n");
                return false;
            }

            buffer[line_length++] =
                (char) receive_data[i];
        }
    }

    printf("TCP line has no newline.\r\n");
    return false;
}




bool ESP8266_TCP_Open(const char * server_ip,
                      uint16_t server_port)
{
    char command[128];

    if ((NULL == server_ip) || ('\0' == server_ip[0]))
    {
        return false;
    }

    ESP8266_TCP_ResetPending();

    /*
     * 清理上一次可能残留的普通 TCP 连接。
     * 未连接时可能返回 ERROR，因此不检查结果。
     */
    (void) ESP8266_SendCmdWaitResponse("AT+CIPCLOSE\r\n",
                                       "OK",
                                       "CLOSED",
                                       2000U);

    if (!ESP8266_SendCmdWaitResponse("AT+CIPMUX=0\r\n",
                                     "OK",
                                     NULL,
                                     2000U))
    {
        return false;
    }

    if (!ESP8266_SendCmdWaitResponse("AT+CIPMODE=0\r\n",
                                     "OK",
                                     NULL,
                                     2000U))
    {
        return false;
    }

    /*
     * 禁止在CIPRECVDATA响应中附带远端IP和端口，
     * 便于按固定格式解析二进制数据。
     */
    if (!ESP8266_SendCmdWaitResponse("AT+CIPDINFO=0\r\n",
                                     "OK",
                                     NULL,
                                     2000U))
    {
        return false;
    }

    /*
     * 使用被动接收模式。
     * PC下发的数据先保存在ESP8266内部缓存，
     * MCU通过AT+CIPRECVDATA分片读取。
     */
    if (!ESP8266_SendCmdWaitResponse("AT+CIPRECVMODE=1\r\n",
                                     "OK",
                                     NULL,
                                     2000U))
    {
        return false;
    }

    int command_length =
        snprintf(command,
                 sizeof(command),
                 "AT+CIPSTART=\"TCP\",\"%s\",%u\r\n",
                 server_ip,
                 (unsigned int) server_port);

    if ((command_length <= 0) ||
        ((size_t) command_length >= sizeof(command)))
    {
        return false;
    }

    /*
     * 正常响应通常为 CONNECT + OK。
     * 若连接已经存在，部分固件会返回 ALREADY CONNECTED。
     */
    if (!ESP8266_SendCmdWaitResponse(command,
                                     "OK",
                                     "ALREADY CONNECTED",
                                     15000U))
    {
        return false;
    }

    return true;
}

bool ESP8266_TCP_Send(const uint8_t * data,
                      uint16_t length)
{
    char command[32];

    if ((NULL == data) || (0U == length))
    {
        return false;
    }

    /*
     * 本项目固定使用 512 字节分片。
     * 这里再限制为 1460 字节，避免单次数据过大。
     */
    if (length > 1460U)
    {
        printf("TCP packet too large: %u\r\n",
               (unsigned int) length);
        return false;
    }

    int command_length =
        snprintf(command,
                 sizeof(command),
                 "AT+CIPSEND=%u\r\n",
                 (unsigned int) length);

    if ((command_length <= 0) ||
        ((size_t) command_length >= sizeof(command)))
    {
        return false;
    }

    /*
     * 收到 '>' 后，ESP8266 开始等待指定长度的二进制数据。
     */
    if (!ESP8266_SendCmdWaitResponse(command,
                                     ">",
                                     NULL,
                                     3000U))
    {
        return false;
    }

    /*
     * 清除 AT+CIPSEND 的响应，单独等待本包的 SEND OK。
     */
    ESP8266_ClearRxBuffer();

    /*
     * 音频数据可能包含 0x00，必须按 length 发送，
     * 不能使用 strlen()。
     */
    if (!ESP8266_UART_Write(data,
                            length,
                            5000U))
    {
        return false;
    }

    if (!ESP8266_WaitResponse("SEND OK",
                              NULL,
                              8000U))
    {
        return false;
    }

    return true;
}

void ESP8266_TCP_Close(void)
{
    (void) ESP8266_SendCmdWaitResponse("AT+CIPCLOSE\r\n",
                                       "OK",
                                       "CLOSED",
                                       3000U);
}

/**
 * @brief 仅初始化Wi-Fi，不连接MQTT
 */
bool ESP8266_WiFiInit(void)
{
    printf("Initialize ESP8266 WiFi only...\r\n");

    if (!ESP8266_SetWiFiMode())
    {
        printf("ESP8266_SetWiFiMode failed.\r\n");
        return false;
    }

    if (!ESP8266_ConnectWiFi())
    {
        printf("ESP8266_ConnectWiFi failed.\r\n");
        return false;
    }

    printf("ESP8266 WiFi connected.\r\n");

    return true;
}














