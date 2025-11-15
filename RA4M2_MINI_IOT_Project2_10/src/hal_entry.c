#include "hal_data.h"
#include "oled.h"
#include "bmp.h"
#include <stdio.h>
#include "lsm6dsv16x_reg.h"

/* ==== ML includes ==== */
#include "feature.h"
#include "clf.h"
#include "weights.h"


FSP_CPP_HEADER
void R_BSP_WarmStart(bsp_warm_start_event_t event);
FSP_CPP_FOOTER


fsp_err_t err = FSP_SUCCESS;
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

/* Callback function */
i2c_master_event_t i2c2_event = I2C_MASTER_EVENT_ABORTED;
uint32_t  i2c2_timeout_ms = 100000;
void sci_i2c_master_callback(i2c_master_callback_args_t *p_args)
{
    i2c2_event = I2C_MASTER_EVENT_ABORTED;
    if (NULL != p_args)
    {
        /* capture callback event for validating the i2c transfer event*/
        i2c2_event = p_args->event;
    }
}

#define SENSOR_BUS g_i2c2_ctrl

/* Private macro -------------------------------------------------------------*/
#define    BOOT_TIME            10 //ms

/* Private variables ---------------------------------------------------------*/
static int16_t data_raw_acceleration[3];
static int16_t data_raw_angular_rate[3];
static int16_t data_raw_temperature;
static double_t acceleration_mg[3];
static double_t angular_rate_mdps[3];
static double_t temperature_degC;
static uint8_t whoamI;
static uint8_t tx_buffer[1000];

static lsm6dsv16x_filt_settling_mask_t filt_settling_mask;

/* Extern variables ----------------------------------------------------------*/

/* Private functions ---------------------------------------------------------*/

/*
 *   WARNING:
 *   Functions declare in this section are defined at the end of this file
 *   and are strictly related to the hardware platform used.
 *
 */
static int32_t platform_write(void *handle, uint8_t reg, const uint8_t *bufp,
                              uint16_t len);
static int32_t platform_read(void *handle, uint8_t reg, uint8_t *bufp,
                             uint16_t len);
static void tx_com( uint8_t *tx_buffer, uint16_t len );
static void platform_delay(uint32_t ms);
static void platform_init(void *handle);


// ==== Data capture config (60 Hz, 10 s, accel-only) ====
//逻辑上认为你的 IMU 输出数据率=60 Hz。后面用它来计算一共要采多少帧，也用于估算 10 s 的采样数。
#define FS_HZ              60
//一次会话的采集时长：10 秒。
#define CAPTURE_SECONDS    10
//一次会话应该采的样本数上限。这里是 60×10=600 帧。
#define CAPTURE_SAMPLES    (FS_HZ * CAPTURE_SECONDS)

// 标签：按你当下做的动作改成 "idle"/"CW"/"CCW"/"UPDOWN"
//录“静止”前把它改成 "idle"，录“顺时针”前改成 "CW"，以此类推（"CCW"、"UPDOWN"）。
static const char* g_label = "UPDOWN";
//是否正在采集的开关。按键触发后置 1，采集满 600 帧或你主动停止后置 0。
static uint8_t  g_capturing = 0;
//会话起始时间戳（HAL_GetTick() 读到的毫秒）。打印 CSV 的第一列 t_ms 就是 HAL_GetTick() - g_start_ms，方便你回放/对齐。
static uint32_t g_start_ms   = 0;
//当前会话已经采了多少帧。每来一帧加速度就 g_count++，到 CAPTURE_SAMPLES 就结束这次会话。
static int      g_count      = 0;

// 按键去抖（K2 低电平=按下）
//上一时刻的按键状态。你的键 默认高电平，所以初值 1（未按）。
static int      g_btn_prev = 1;
//上一次“状态改变”的时间戳，用来做时间去抖。
static uint32_t g_btn_last_change = 0;
//去抖时间窗口 30 ms。只有当状态稳定超过 30 ms 才认为“真的按下/弹起”。
static const uint32_t BTN_DEBOUNCE_MS = 30;


/* 毫秒计时器，全局Tick */
static volatile uint32_t g_ms_tick = 0;

/* GPT0 溢出/周期中断回调 */
void gpt0_callback(timer_callback_args_t * p_args)
{
    if (p_args == NULL)
    {
        return;
    }

    /* 我们只关心“一个周期结束”这个事件。
       FSP 里通常对应 TIMER_EVENT_CYCLE_END。 */
    if (p_args->event == TIMER_EVENT_CYCLE_END)
    {
        g_ms_tick++;  // 每 1ms 加 1
    }
}

static inline uint32_t hal_millis(void)
{
    return g_ms_tick;
}

/* 采样率、窗口、步长——必须与训练时一致 */
#define FS                     60          // Hz
#define WIN                    (2*FS)      // 120 samples (约2秒)
#define HOP                    (FS/5)      // 12 samples (~0.2秒)


/* 实时推理相关 */
#define DEBOUNCE_N             4           // 连续多少次同类才稳输出
#define IDLE_ENERGY_THR        (5e-4f)     // 静止能量阈值 (g^2)

static const char* NAMES[] = { "idle", "CW", "UPDOWN" };

/* 环形缓冲：保存最近 WIN 帧 (ax,ay,az)，用于提特征 / 推理 */
static float ring[WIN * 3];
static int   wpos   = 0;   // 写指针 (0..WIN-1)
static int   hopc   = 0;   // 计数到 HOP 触发一次推理



/* 去抖输出状态 */
static int last_cls = -1;
static int stable_n = 0;

/* OLED 当前已经显示的动作，用来避免重复刷新屏幕 */
static int oled_shown_cls = -1;
/****************************************************************
 * TinyML 推理关键函数：
 *  - on_acc(): 每帧加速度进 ring，并按步长 HOP 触发 infer_once()
 *  - infer_once(): 从 ring 提特征，能量过低直接 idle，否则 int8 线性分类器
 *                  然后做去抖并 printf 输出稳定标签
 ****************************************************************/

/* infer_once(): 对最近2秒窗口做一次分类 */
static void infer_once(void)
{
    float feat[FEATURE_DIM];

    /* 零拷贝特征提取：直接用环形缓冲 ring[WIN*3] */
    feat_extract_from_circ3(ring, WIN, wpos, feat);

    /* 计算静止能量：三个轴的方差 (feat[1],feat[6],feat[11]) 相加 */
    float energy = feat[1] + feat[6] + feat[11];

    int cls;
    if (energy < IDLE_ENERGY_THR)
    {
        /* 非常安静，强制认 idle */
        cls = 0;
    }
    else
    {
        /* 用量化逻辑回归模型 (dense_i8_t) 做分类 */
        const dense_i8_t* model = get_trained_model();
        cls = infer_linear_i8(model, feat);  // 0=idle,1=CW,2=UPDOWN
    }

    /* 连续 DEBOUNCE_N 次同一类后才稳定输出 */
    if (cls == last_cls)
    {
        if (++stable_n >= DEBOUNCE_N)
        {
            printf("[%lu ms] %s\r\n",
                   (unsigned long)hal_millis(),
                   NAMES[cls]);

            /* 如果OLED上显示的状态变化了，就刷新OLED */
            if (cls != oled_shown_cls)
            {
                oled_shown_cls = cls;

                OLED_Clear();  // 清屏
                // 居中显示的话你可以自己调坐标，
                // 这里用你给的参考：x=32, y=20, 字号24, 亮显(1)
                OLED_ShowString(32, 20, (char*)NAMES[cls], 24, 1);
                OLED_Refresh();
            }

        }
    }
    else
    {
        last_cls = cls;
        stable_n = 1;
        /* 如果你想一换类就立刻也打印一次，可以加：
           printf("[%lu ms] %s(edge)\r\n", (unsigned long)hal_millis(), NAMES[cls]);
        */
    }
}

/* on_acc(): 每拿到一帧加速度(ax,ay,az)就进环形缓冲+尝试推理 */
static void on_acc(float ax_g, float ay_g, float az_g)
{
    ring[wpos*3 + 0] = ax_g;
    ring[wpos*3 + 1] = ay_g;
    ring[wpos*3 + 2] = az_g;

    wpos = (wpos + 1) % WIN;

    if (++hopc >= HOP)
    {
        hopc = 0;
        infer_once();
    }
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
    OLED_ShowString(0, 0, "RA4M2", 16, 1);//在坐标 (0,0) 显示RA4M2，字体16，正显
    OLED_ShowString(0, 16, "hello world!", 16, 0);//在坐标 (0,16) 显示hello world!，字体16，反显
    OLED_Refresh();// 将 OLED_GRAM 显存内容刷新到 OLED 屏幕上，显示数字和字符串

    R_BSP_SoftwareDelay(200, BSP_DELAY_UNITS_MILLISECONDS);  // 延时 200 毫秒

    /* Open the transfer instance with initial configuration. */
    err = R_SCI_UART_Open(&g_uart9_ctrl, &g_uart9_cfg);
    assert(FSP_SUCCESS == err);

    printf("hello!\n");

    /* Initialize the I2C module */
    err = R_SCI_I2C_Open(&g_i2c2_ctrl, &g_i2c2_cfg);
    /* Handle any errors. This function should be defined by the user. */
    assert(FSP_SUCCESS == err);

    //LSM6DSV16X CS->1
    R_IOPORT_PinWrite(&g_ioport_ctrl, BSP_IO_PORT_05_PIN_00, BSP_IO_LEVEL_HIGH);

    lsm6dsv16x_reset_t rst;
    stmdev_ctx_t dev_ctx;
    /* Initialize mems driver interface */
    dev_ctx.write_reg = platform_write;
    dev_ctx.read_reg = platform_read;
    dev_ctx.mdelay = platform_delay;
    dev_ctx.handle = &SENSOR_BUS;

    /* Init test platform */
  //  platform_init(dev_ctx.handle);
    /* Wait sensor boot time */
    platform_delay(BOOT_TIME);

    /* Check device ID */
    lsm6dsv16x_device_id_get(&dev_ctx, &whoamI);
      printf("LSM6DSV16X_ID=0x%x,whoamI=0x%x\n",LSM6DSV16X_ID,whoamI);
    if (whoamI != LSM6DSV16X_ID)
      while (1);

    /* Restore default configuration */
    lsm6dsv16x_reset_set(&dev_ctx, LSM6DSV16X_RESTORE_CTRL_REGS);
    do {
      lsm6dsv16x_reset_get(&dev_ctx, &rst);
    } while (rst != LSM6DSV16X_READY);

    /* Enable Block Data Update */
    lsm6dsv16x_block_data_update_set(&dev_ctx, PROPERTY_ENABLE);
    /* Set Output Data Rate.
     * Selected data rate have to be equal or greater with respect
     * with MLC data rate.
     */
    lsm6dsv16x_xl_data_rate_set(&dev_ctx, LSM6DSV16X_ODR_AT_60Hz);
    lsm6dsv16x_gy_data_rate_set(&dev_ctx, LSM6DSV16X_ODR_AT_60Hz);
    /* Set full scale */
    lsm6dsv16x_xl_full_scale_set(&dev_ctx, LSM6DSV16X_2g);
    lsm6dsv16x_gy_full_scale_set(&dev_ctx, LSM6DSV16X_2000dps);


    /* Initializes the module. */
    err = R_GPT_Open(&g_timer0_ctrl, &g_timer0_cfg);
    /* Handle any errors. This function should be defined by the user. */
    assert(FSP_SUCCESS == err);

    /* Start the timer. */
    (void) R_GPT_Start(&g_timer0_ctrl);

    while (1)
    {

        lsm6dsv16x_data_ready_t drdy;

        /* Read output only if new xl value is available */
        lsm6dsv16x_flag_data_ready_get(&dev_ctx, &drdy);

        if (drdy.drdy_xl) {
          /* Read acceleration, convert to mg */
          memset(data_raw_acceleration, 0x00, 3 * sizeof(int16_t));
          lsm6dsv16x_acceleration_raw_get(&dev_ctx, data_raw_acceleration);
          acceleration_mg[0] = lsm6dsv16x_from_fs2_to_mg(data_raw_acceleration[0]);
          acceleration_mg[1] = lsm6dsv16x_from_fs2_to_mg(data_raw_acceleration[1]);
          acceleration_mg[2] = lsm6dsv16x_from_fs2_to_mg(data_raw_acceleration[2]);

          /* 转锟斤拷 g锟斤拷喂锟斤拷锟斤拷锟斤拷锟斤拷锟斤拷 */
          float ax = (float)acceleration_mg[0] / 1000.0f;
          float ay = (float)acceleration_mg[1] / 1000.0f;
          float az = (float)acceleration_mg[2] / 1000.0f;
          on_acc(ax, ay, az);


        }

                {
                    bsp_io_level_t lvl;
                    R_IOPORT_PinRead(&g_ioport_ctrl,BSP_IO_PORT_00_PIN_00,&lvl);
                    /* 我们把 '按下' 映射成 1，'松开' 映射成 0 */
                    int btn = (lvl == BSP_IO_LEVEL_LOW) ? 1 : 0;
                    uint32_t now_ms = hal_millis();
                    if ( (btn != g_btn_prev) &&((now_ms - g_btn_last_change) > BTN_DEBOUNCE_MS) )
                    {
                        g_btn_prev = btn;
                        g_btn_last_change = now_ms;
                        /* 按键从松开->按下，并且当前不在采集，才开始一段新的采集 */
                        if (btn && !g_capturing)
                        {
                            g_capturing = 1;
                            g_start_ms = now_ms;
                            g_count = 0;
                            // 采集前先打表头，后续可直接复制到 CSV 文件
                            printf("t_ms\tax_g\tay_g\taz_g\tlabel\t\r\n");
                        }
                    }

                }
        /* 适当小延时，避免while(1)完全满速占用CPU。
           注意：不要延太久，否则可能丢样本（60Hz=每16.7ms一帧）。
           这里 1~2ms 就够了。 */
        R_BSP_SoftwareDelay(1, BSP_DELAY_UNITS_MILLISECONDS);

    }

#if BSP_TZ_SECURE_BUILD
    /* Enter non-secure code */
    R_BSP_NonSecureEnter();
#endif
}


/*
 * @brief  Write generic device register (platform dependent)
 *
 * @param  handle    customizable argument. In this examples is used in
 *                   order to select the correct sensor bus handler.
 * @param  reg       register to write
 * @param  bufp      pointer to data to write in register reg
 * @param  len       number of consecutive register to write
 *
 */
static int32_t platform_write(void *handle, uint8_t reg, const uint8_t *bufp,uint16_t len)
{
    // 创建一个足够大的缓冲区来包含寄存器地址和数据
    uint8_t data[len + 1];
    data[0] = reg; // 将寄存器地址放在数据的开始
    memcpy(&data[1], bufp, len); // 复制数据到缓冲区

    err = R_SCI_I2C_Write(&g_i2c2_ctrl, data, len+1, true);
    assert(FSP_SUCCESS == err);
    /* Since there is nothing else to do, block until Callback triggers*/
    //while ((I2C_MASTER_EVENT_TX_COMPLETE != i2c_event) && timeout_ms)
    while ((I2C_MASTER_EVENT_TX_COMPLETE != i2c2_event) && i2c2_timeout_ms>0)
    {
        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MICROSECONDS);
        i2c2_timeout_ms--;
        }
    if (I2C_MASTER_EVENT_ABORTED == i2c2_event)
    {
        __BKPT(0);
    }
    /* Read data back from the I2C slave */
    i2c2_event = I2C_MASTER_EVENT_ABORTED;
    i2c2_timeout_ms           = 100000;
    return 0;
}


/*
 * @brief  Read generic device register (platform dependent)
 *
 * @param  handle    customizable argument. In this examples is used in
 *                   order to select the correct sensor bus handler.
 * @param  reg       register to read
 * @param  bufp      pointer to buffer that store the data read
 * @param  len       number of consecutive register to read
 *
 */
static int32_t platform_read(void *handle, uint8_t reg, uint8_t *bufp,uint16_t len)
{
    err = R_SCI_I2C_Write(&g_i2c2_ctrl, &reg, 1, true);
    assert(FSP_SUCCESS == err);
    /* Since there is nothing else to do, block until Callback triggers*/
    //while ((I2C_MASTER_EVENT_TX_COMPLETE != i2c_event) && timeout_ms)
    while ((I2C_MASTER_EVENT_TX_COMPLETE != i2c2_event) && i2c2_timeout_ms>0)
    {
        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MICROSECONDS);
        i2c2_timeout_ms--;
        }
    if (I2C_MASTER_EVENT_ABORTED == i2c2_event)
    {
        __BKPT(0);
        }
    /* Read data back from the I2C slave */
    i2c2_event = I2C_MASTER_EVENT_ABORTED;
    i2c2_timeout_ms           = 100000;

    /* Read data from I2C slave */
    err = R_SCI_I2C_Read(&g_i2c2_ctrl, bufp, len, false);
    assert(FSP_SUCCESS == err);
    while ((I2C_MASTER_EVENT_RX_COMPLETE != i2c2_event) && i2c2_timeout_ms)
    {
        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
        i2c2_timeout_ms--;
    }
    if (I2C_MASTER_EVENT_ABORTED == i2c2_event)
    {
        __BKPT(0);
    }

    i2c2_event = I2C_MASTER_EVENT_ABORTED;
    i2c2_timeout_ms           = 100000;
  return 0;
}


/*
 * @brief  platform specific delay (platform dependent)
 *
 * @param  ms        delay in ms
 *
 */
static void platform_delay(uint32_t ms)
{
    R_BSP_SoftwareDelay(ms, BSP_DELAY_UNITS_MILLISECONDS);
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
