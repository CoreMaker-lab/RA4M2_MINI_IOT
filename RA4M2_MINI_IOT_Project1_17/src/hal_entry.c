#include "hal_data.h"
#include <stdio.h>
#include "ESP8266.h"
#include "dht11.h"
#include "oled.h"
#include "bmp.h"



#define AUDIO_TCP_SERVER_IP       "192.168.43.124"
#define AUDIO_TCP_SERVER_PORT     9000

#define AUDIO_UPLOAD_CHUNK_SIZE   512
#define AUDIO_PCM_BYTES           (AUDIO_SAMPLE_COUNT * 2)


FSP_CPP_HEADER
void R_BSP_WarmStart(bsp_warm_start_event_t event);
FSP_CPP_FOOTER

fsp_err_t err = FSP_SUCCESS;
volatile bool uart_send_complete_flag = false;
void user_uart_callback (uart_callback_args_t * p_args)
{
    if(p_args->event == UART_EVENT_TX_COMPLETE)
    {
        uart_send_complete_flag = true;
    }
}
#ifdef __GNUC__                                 //串口重定向
    #define PUTCHAR_PROTOTYPE int __io_putchar(int ch)
#else
    #define PUTCHAR_PROTOTYPE int fputc(int ch, FILE *f)
#endif


PUTCHAR_PROTOTYPE
{
        err = R_SCI_UART_Write(&g_uart9_ctrl, (uint8_t *)&ch, 1);
        if(FSP_SUCCESS != err) __BKPT();
        while(uart_send_complete_flag == false){}
        uart_send_complete_flag = false;
        return ch;
}

int _write(int fd,char *pBuffer,int size)
{
    for(int i=0;i<size;i++)
    {
        __io_putchar(*pBuffer++);
    }
    return size;
}


/* ---------- UART4 接收：极简环形缓冲 + 回调塞字节 ---------- */
#define WIFI_RX_BUF_SZ   1024
uint8_t  wifi_rb[WIFI_RX_BUF_SZ];
uint16_t RxLine=0;           //接收到的数据长度
uint8_t Rx_flag_finish=0; //接受完成或者时间溢出
bool uart4_tx_flag = false;
void user_uart4_callback(uart_callback_args_t * p_args)
{
    if (UART_EVENT_RX_CHAR == p_args->event)
    {
        /*
         * 不判断 p_args->data 是否为 NULL。
         * data 是接收到的字节值，0x00 也是合法数据。
         */
        if (RxLine < (WIFI_RX_BUF_SZ - 1U))
        {
            wifi_rb[RxLine++] = (uint8_t) p_args->data;
            wifi_rb[RxLine] = '\0';
        }

        /* 每收到一个字节就重置 GPT0，推迟接收超时点。 */
        (void) R_GPT_Reset(&g_timer0_ctrl);
    }
    else if (UART_EVENT_TX_COMPLETE == p_args->event)
    {
        uart4_tx_flag = true;
    }
}

/* 周期到点（溢出）标志：中断里置位，主循环里读+清 */
uint8_t gpt0_flag = 0;

/* GPT0 中断回调：到达设定周期（计数溢出）时会进来 */
void gpt0_callback(timer_callback_args_t *p_args)
{
    if (TIMER_EVENT_CYCLE_END == p_args->event)
    {
        gpt0_flag = 1;  // 到点了，通知主循环
        if (RxLine > 0)
        {
            Rx_flag_finish = 1;       // ★ 通知主循环：有一帧可打印
        }
    }
}

//温湿度变量定义
uint8_t humdity_integer;//湿度整数
uint8_t humdity_decimal;//湿度小数
uint8_t temp_integer ;//温度整数
uint8_t temp_decimal ;//温度小数
uint8_t dht11_check ;//校验值
uint16_t dht11_i=0;//定时上报计数

/* Callback function */
i2c_master_event_t i2c_event = I2C_MASTER_EVENT_ABORTED;
uint32_t  timeout_ms = 1000000;
void i2c_master_callback(i2c_master_callback_args_t *p_args)
{
    i2c_event = I2C_MASTER_EVENT_ABORTED;
    if (NULL != p_args)
    {
        /* capture callback event for validating the i2c transfer event*/
        i2c_event = p_args->event;
    }
}

#define AUDIO_SAMPLE_RATE      8000        // 8 kHz
#define AUDIO_DURATION_SEC     5           // 录音时长 5 秒
#define AUDIO_SAMPLE_COUNT     (AUDIO_SAMPLE_RATE * AUDIO_DURATION_SEC)  // 40000

// 录音缓冲：40000 个 16bit 采样点
uint16_t buzzer_num[AUDIO_SAMPLE_COUNT];

// 按键 + 音频状态
typedef enum
{
    AUDIO_STATE_IDLE = 0,
    AUDIO_STATE_RECORDING,
    AUDIO_STATE_PLAYBACK,
    AUDIO_STATE_UPLOAD
} audio_state_t;

volatile audio_state_t g_audio_state = AUDIO_STATE_IDLE;

// 录音/回放各自的索引
volatile uint32_t g_record_index = 0;
volatile uint32_t g_play_index   = 0;

// 由定时器中断置位，主循环看到后去采一帧 ADC
volatile uint8_t g_need_sample = 0;

void gpt6_callback(timer_callback_args_t *p_args)
{
    if (TIMER_EVENT_CYCLE_END == p_args->event)
    {
        switch (g_audio_state)
        {
            case AUDIO_STATE_RECORDING:
                if (g_record_index < AUDIO_SAMPLE_COUNT)
                {
                    g_need_sample = 1;
                }
                else
                {
                    /* 录音完成，进入上传状态 */
                    g_need_sample = 0;
                    g_audio_state = AUDIO_STATE_UPLOAD;
                }
                break;

            case AUDIO_STATE_PLAYBACK:
                if (g_play_index < AUDIO_SAMPLE_COUNT)
                {
                    // 按 8kHz 的节拍，往 DAC 丢一帧
                    err = R_DAC_Write(&g_dac0_ctrl, buzzer_num[g_play_index]);
                    assert(FSP_SUCCESS == err);
                    g_play_index++;
                }
                else
                {
                    // 播放完毕，关掉输出，回到空闲
                    err = R_DAC_Write(&g_dac0_ctrl, 0);
                    assert(FSP_SUCCESS == err);
                    g_audio_state = AUDIO_STATE_IDLE;
                }
                break;

            case AUDIO_STATE_IDLE:
            default:
                // 空闲状态下，这个定时器就啥也不干
                break;
        }
    }
}

volatile bool scan_complete_flag = false;
void adc_callback (adc_callback_args_t * p_args)
{
    //宏将告知编译器回调函数不使用参数 p_args，从而避免编译器发出警告，
    FSP_PARAMETER_NOT_USED(p_args);
    scan_complete_flag = true;
}

static uint16_t Audio_Calculate_DC(void)
{
    uint32_t sum = 0;

    for (uint32_t i = 0; i < AUDIO_SAMPLE_COUNT; i++)
    {
        sum += buzzer_num[i];
    }

    return (uint16_t)(sum / AUDIO_SAMPLE_COUNT);
}

static int16_t Audio_ADC_To_PCM16(uint16_t adc_value,
                                  uint16_t dc_value)
{
    int32_t pcm;

    pcm = ((int32_t)adc_value - (int32_t)dc_value) << 4;

    if (pcm > 32767)
    {
        pcm = 32767;
    }
    else if (pcm < -32768)
    {
        pcm = -32768;
    }

    return (int16_t)pcm;
}

static bool Audio_Upload_TCP(void)
{
    char header[80];
    uint8_t tx_buffer[AUDIO_UPLOAD_CHUNK_SIZE];

    uint16_t dc_value = Audio_Calculate_DC();
    uint32_t sample_index = 0U;
    uint32_t packet_index = 0U;

    printf("\r\nAudio upload start...\r\n");
    printf("Server: %s:%u\r\n",
           AUDIO_TCP_SERVER_IP,
           AUDIO_TCP_SERVER_PORT);
    printf("DC=%u, samples=%lu, bytes=%lu\r\n",
           dc_value,
           (unsigned long) AUDIO_SAMPLE_COUNT,
           (unsigned long) AUDIO_PCM_BYTES);

    if (!ESP8266_TCP_Open(AUDIO_TCP_SERVER_IP,
                          AUDIO_TCP_SERVER_PORT))
    {
        printf("TCP connect failed.\r\n");
        return false;
    }

    /*
     * 协议头：
     * RA4A,版本,采样率,位宽,声道数,采样点数,PCM字节数\n
     */
    int header_length =
        snprintf(header,
                 sizeof(header),
                 "RA4A,1,%u,16,1,%lu,%lu\n",
                 AUDIO_SAMPLE_RATE,
                 (unsigned long) AUDIO_SAMPLE_COUNT,
                 (unsigned long) AUDIO_PCM_BYTES);

    if ((header_length <= 0) ||
        ((size_t) header_length >= sizeof(header)))
    {
        printf("Build audio header failed.\r\n");
        ESP8266_TCP_Close();
        return false;
    }

    if (!ESP8266_TCP_Send((const uint8_t *) header,
                          (uint16_t) header_length))
    {
        printf("Send audio header failed.\r\n");
        ESP8266_TCP_Close();
        return false;
    }

    while (sample_index < AUDIO_SAMPLE_COUNT)
    {
        uint16_t offset = 0U;

        while (((uint32_t) offset + 2U <= AUDIO_UPLOAD_CHUNK_SIZE) &&
               (sample_index < AUDIO_SAMPLE_COUNT))
        {
            int16_t pcm =
                Audio_ADC_To_PCM16(buzzer_num[sample_index],
                                   dc_value);

            /* PCM16 little-endian */
            tx_buffer[offset++] =
                (uint8_t) ((uint16_t) pcm & 0xFFU);
            tx_buffer[offset++] =
                (uint8_t) (((uint16_t) pcm >> 8U) & 0xFFU);

            sample_index++;
        }

        if (!ESP8266_TCP_Send(tx_buffer, offset))
        {
            printf("Send packet %lu failed.\r\n",
                   (unsigned long) packet_index);
            ESP8266_TCP_Close();
            return false;
        }

        packet_index++;

        if (((packet_index % 16U) == 0U) ||
            (sample_index == AUDIO_SAMPLE_COUNT))
        {
            printf("Upload %lu/%lu bytes, packet=%lu\r\n",
                   (unsigned long) (sample_index * 2U),
                   (unsigned long) AUDIO_PCM_BYTES,
                   (unsigned long) packet_index);
        }
    }

    R_BSP_SoftwareDelay(100U, BSP_DELAY_UNITS_MILLISECONDS);
    ESP8266_TCP_Close();

    printf("Audio upload complete, packets=%lu.\r\n",
           (unsigned long) packet_index);

    return true;
}


/*******************************************************************************************************************//**
 * main() is generated by the RA Configuration editor and is used to generate threads if an RTOS is used.  This function
 * is called by main() when no RTOS is used.
 **********************************************************************************************************************/
void hal_entry(void)
{
    /* TODO: add your own code here */

    /* Initialize the I2C module */
    err = R_IIC_MASTER_Open(&g_i2c_master0_ctrl, &g_i2c_master0_cfg);
    /* Handle any errors. This function should be defined by the user. */
    assert(FSP_SUCCESS == err);

    OLED_Init();              // 初始化 OLED 屏幕（发送初始化命令序列，设置工作模式）
    OLED_Clear();             // 清空显存（OLED_GRAM），并刷新，使屏幕全黑
    OLED_ColorTurn(0);        // 设置显示颜色模式：0 为正常显示，1 为反色显示（黑白反转）
    OLED_DisplayTurn(0);      // 设置显示方向：0 为正常方向，1 为上下翻转显示
    OLED_Clear();//清空 OLED 显存
    OLED_ShowString(0, 0,  "RA4M2_MINI_IOT", 16, 1);//在坐标 (0,0) 显示RA4M2_MINI_IOT，字体16，正显
    OLED_ShowString(0, 16, "Temp:", 16, 1);//在坐标 (0,16) 显示Temp，字体16，正显
    OLED_ShowString(0, 32, "Humi:", 16, 1);//在坐标 (0,32) 显示Humi，字体16，反显
    OLED_Refresh();// 将 OLED_GRAM 显存内容刷新到 OLED 屏幕上，显示数字和字符串

    /**********************DHT11初始化***************************************/
    R_IOPORT_PinWrite(&g_ioport_ctrl, BSP_IO_PORT_01_PIN_12, BSP_IO_LEVEL_HIGH);
    R_BSP_SoftwareDelay(1000U, BSP_DELAY_UNITS_MILLISECONDS);

    /* Open the transfer instance with initial configuration. */
    err = R_SCI_UART_Open(&g_uart9_ctrl, &g_uart9_cfg);
    assert(FSP_SUCCESS == err);
    printf("hello world!\n");


    /* Initializes the module. */
    err = R_GPT_Open(&g_timer0_ctrl, &g_timer0_cfg);
    /* Handle any errors. This function should be defined by the user. */
    assert(FSP_SUCCESS == err);

    /* Start the timer. */
    (void) R_GPT_Start(&g_timer0_ctrl);

    err = R_GPT_PeriodSet(&g_timer0_ctrl, 9000000);//频率
    assert(FSP_SUCCESS == err);
    R_BSP_SoftwareDelay(1, BSP_DELAY_UNITS_MILLISECONDS);

    /* 打开 UART4：与 ESP8266 相连（回调需在 FSP 配置为 user_uart4_callback） */
    err = R_SCI_UART_Open(&g_uart4_ctrl, &g_uart4_cfg);
    assert(FSP_SUCCESS == err);

    static uint8_t sec_count = 0;      // 计数秒数
    static float fake_temperature = 25.0f;  // 虚拟温度值，初始25.0摄氏度
    /* 步骤1：使用静态标志确保 ESP8266 初始化流程仅执行一次 */
    static bool esp8266_inited = false;

    if (!esp8266_inited)
    {
        if (!ESP8266_WiFiInit())
        {
            printf("ESP8266 WiFi initialization failed.\r\n");

            while (1)
            {
                R_BSP_SoftwareDelay(
                    1000U,
                    BSP_DELAY_UNITS_MILLISECONDS);
            }
        }

        esp8266_inited = true;
    }

    /* Initialize the DAC channel */
    err = R_DAC_Open(&g_dac0_ctrl, &g_dac0_cfg);
    /* Handle any errors. This function should be defined by the user. */
    assert(FSP_SUCCESS == err);



    err = R_DAC_Start(&g_dac0_ctrl);
    assert(FSP_SUCCESS == err);
//    err = R_DAC_Write(&g_dac0_ctrl, 1024);
//    assert(FSP_SUCCESS == err);

//    length = sizeof(data) / sizeof(unsigned char); //计算长度


    /* Initializes the module. */
    err = R_GPT_Open(&g_timer6_ctrl, &g_timer6_cfg);
    /* Handle any errors. This function should be defined by the user. */
    assert(FSP_SUCCESS == err);
    /* Start the timer. */
    (void) R_GPT_Start(&g_timer6_ctrl);

    (void) R_GPT_Enable(&g_timer6_ctrl);
    R_BSP_SoftwareDelay (20, BSP_DELAY_UNITS_MILLISECONDS);

    err = R_ADC_Open(&g_adc0_ctrl, &g_adc0_cfg);
    /* Handle any errors. This function should be defined by the user. */
    assert(FSP_SUCCESS == err);
    /* Enable channels. */
    err = R_ADC_ScanCfg(&g_adc0_ctrl, &g_adc0_channel_cfg);
    assert(FSP_SUCCESS == err);
    /* Enable scan triggering from ELC events. */
    (void) R_ADC_ScanStart(&g_adc0_ctrl);
    printf("111111111\n");
    /* Read samples in polling mode (no int) */

    while(1)
    {
        /* 1. 读取按键：假设按下为低电平（跟你原来一样） */
        bsp_io_level_t key_level;
        R_IOPORT_PinRead(&g_ioport_ctrl, BSP_IO_PORT_00_PIN_00, &key_level);

        if (key_level == BSP_IO_LEVEL_LOW)
        {
            // 只在空闲状态下响应按键，避免录音/播放过程中误触
            if (g_audio_state == AUDIO_STATE_IDLE)
            {
                // 简单防抖（按下保持一会儿再认定），可以按需加
                R_BSP_SoftwareDelay(20, BSP_DELAY_UNITS_MILLISECONDS);
                R_IOPORT_PinRead(&g_ioport_ctrl, BSP_IO_PORT_00_PIN_00, &key_level);
                if (key_level == BSP_IO_LEVEL_LOW)
                {
                    /* 清空状态和录音缓冲 */
                    g_record_index = 0U;
                    g_play_index = 0U;
                    g_need_sample = 0U;
                    scan_complete_flag = false;
                    memset(buzzer_num, 0, sizeof(buzzer_num));

                    printf("Start recording 5s audio...\r\n");

                    /* 先切换状态，再复位并启动 8 kHz GPT6 */
                    g_audio_state = AUDIO_STATE_RECORDING;

                    err = R_GPT_Reset(&g_timer6_ctrl);
                    assert(FSP_SUCCESS == err);

                    err = R_GPT_Start(&g_timer6_ctrl);
                    assert(FSP_SUCCESS == err);
                }
            }
        }

        /* 2. 如果定时器要求采样，就采一帧 ADC 填到缓冲区 */
        if (g_need_sample)
        {
            g_need_sample = 0;

            if (g_record_index < AUDIO_SAMPLE_COUNT)
            {
                uint16_t adc_data = 0;

                // 启动一次 ADC 转换（单次软件触发）
                scan_complete_flag = false;
                (void) R_ADC_ScanStart(&g_adc0_ctrl);

                while (!scan_complete_flag)
                {
                    // 等待转换完成标志（adc_callback 置位）
                }

                err = R_ADC_Read(&g_adc0_ctrl, ADC_CHANNEL_11, &adc_data);
                assert(FSP_SUCCESS == err);

                buzzer_num[g_record_index] = adc_data;
                g_record_index++;
            }
            // 超过的话就不再写，状态机会在下一次中断里切到上传
        }

        /* 3. 录音完成后，在主循环中停止采样定时器并上传 */
        if (g_audio_state == AUDIO_STATE_UPLOAD)
        {
            err = R_GPT_Stop(&g_timer6_ctrl);
            assert(FSP_SUCCESS == err);

            printf("Recording complete: %lu samples.\r\n",
                   (unsigned long) g_record_index);

            bool upload_ok = Audio_Upload_TCP();

            if (upload_ok)
            {
                printf("Audio upload success.\r\n");
            }
            else
            {
                printf("Audio upload failed.\r\n");
            }

            g_audio_state = AUDIO_STATE_IDLE;
            printf("Press the key to record again.\r\n");
        }

//        if(dht11_i<1000)
//            dht11_i++;
//        else
//        {
//            dht11_i=0;
//            DHT11_Read();
////            printf("hum=%d.%d temp=%d.%d\n",humdity_integer,humdity_decimal,temp_integer,temp_decimal);
//            // 温度整数部分
//            OLED_ShowNum(48, 16, temp_integer, 2, 16, 1);
//            // 小数点
//            OLED_ShowString(64, 16, ".", 16, 1);
//            // 温度小数部分
//            OLED_ShowNum(72, 16, temp_decimal, 1, 16, 1);
//
//            // 湿度整数部分
//            OLED_ShowNum(48, 32, humdity_integer, 2, 16, 1);
//            // 小数点
//            OLED_ShowString(64, 32, ".", 16, 1);
//            // 湿度小数部分
//            OLED_ShowNum(72, 32, humdity_decimal, 1, 16, 1);
//
//            // 把显存刷到屏幕上
//            OLED_Refresh();
//        }
//
//
//        /* ★ 帧完成：在主循环里打印（非中断环境，安全可靠） */
//        if (Rx_flag_finish)
//        {
//            uint8_t  print_buf[WIFI_RX_BUF_SZ];
//            uint16_t len = RxLine;              // 拷贝长度
//            printf("ESP8266 TX->\n");
//            err = R_SCI_UART_Write(&g_uart9_ctrl, wifi_rb, RxLine);
//            if(FSP_SUCCESS != err) __BKPT();
//            while(uart_send_complete_flag == false){}
//            uart_send_complete_flag = false;
//
//            printf("\r\n");
//            RxLine = 0;                         // 清空，准备下一帧
//            Rx_flag_finish = 0;
//            memset(wifi_rb,0,sizeof(wifi_rb));  //清空缓存数组
//
//        }
//
//        /* 步骤2：每10秒调用 ESP8266_LOOP() 上报一次虚拟温度值 */
//        if (gpt0_flag)
//        {
//
//            if(esp8266_inited)
//            {
//                sec_count++;        // 累计秒计数
//                if (sec_count >= 100)    // 满10秒 100ms*100=10s
//                {
//                    sec_count = 0;
////                    // 简单变化虚拟温度值，模拟传感器读数变化
////                    fake_temperature += 0.5f;
////                    if (fake_temperature > 30.0f) {
////                        fake_temperature = 25.0f;
////                    }
//                    /* 关键：在这里把 temp_integer / temp_decimal 融合成 float */
//                    float temperature = temp_integer + temp_decimal / 10.0f;
//                    // 每10秒发布一次温度
//                    ESP8266_MQTTPublish(temperature);
//
//                    float humidity    = humdity_integer  + humdity_decimal  / 10.0f;
//                    // 简化上报湿度
//                    ESP8266_MQTTPublish_Humidity(humidity);
//                }
//            }
//            gpt0_flag = 0;      // 清除GPT标志
//        }
//        R_BSP_SoftwareDelay(1, BSP_DELAY_UNITS_MILLISECONDS);
    }


#if BSP_TZ_SECURE_BUILD
    /* Enter non-secure code */
    R_BSP_NonSecureEnter();
#endif
}

/*******************************************************************************************************************//**
 * This function is called at various points during the startup process.  This implementation uses the event that is
 * called right before main() to set up the pins.
 *
 * @param[in]  event    Where at in the start up process the code is currently at
 **********************************************************************************************************************/
void R_BSP_WarmStart(bsp_warm_start_event_t event)
{
    if (BSP_WARM_START_RESET == event)
    {
#if BSP_FEATURE_FLASH_LP_VERSION != 0

        /* Enable reading from data flash. */
        R_FACI_LP->DFLCTL = 1U;

        /* Would normally have to wait tDSTOP(6us) for data flash recovery. Placing the enable here, before clock and
         * C runtime initialization, should negate the need for a delay since the initialization will typically take more than 6us. */
#endif
    }

    if (BSP_WARM_START_POST_C == event)
    {
        /* C runtime environment and system clocks are setup. */

        /* Configure pins. */
        R_IOPORT_Open (&IOPORT_CFG_CTRL, &IOPORT_CFG_NAME);

#if BSP_CFG_SDRAM_ENABLED

        /* Setup SDRAM and initialize it. Must configure pins first. */
        R_BSP_SdramInit(true);
#endif
    }
}

#if BSP_TZ_SECURE_BUILD

FSP_CPP_HEADER
BSP_CMSE_NONSECURE_ENTRY void template_nonsecure_callable ();

/* Trustzone Secure Projects require at least one nonsecure callable function in order to build (Remove this if it is not required to build). */
BSP_CMSE_NONSECURE_ENTRY void template_nonsecure_callable ()
{

}
FSP_CPP_FOOTER

#endif
