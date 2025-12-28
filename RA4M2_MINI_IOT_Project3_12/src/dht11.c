/*
 * dht11.c
 *
 *  Created on: 2025年11月09日
 *      Author: a8456
 */
#include "dht11.h"




extern fsp_err_t err ;



uint8_t DHT11_ReadByte(void)
{
    bsp_io_level_t state;
    uint8_t ans=0,ind;
    for (ind=0;ind<8;ind++)
    {
        ans<<=1;
        do
        {
            R_IOPORT_PinRead(&g_ioport_ctrl, BSP_IO_PORT_01_PIN_12, &state);
        }while(state==BSP_IO_LEVEL_LOW);//直至识别低电平，开始接受数据
        R_BSP_SoftwareDelay(30, BSP_DELAY_UNITS_MICROSECONDS); //延时54us
        R_IOPORT_PinRead(&g_ioport_ctrl, BSP_IO_PORT_01_PIN_12, &state);
        if (state==BSP_IO_LEVEL_HIGH)
            ans |= 1;
        do{
            R_IOPORT_PinRead(&g_ioport_ctrl, BSP_IO_PORT_01_PIN_12, &state);
        }while(state==BSP_IO_LEVEL_HIGH);
    }
    return ans;
}


extern uint8_t humdity_integer;//湿度整数
extern uint8_t humdity_decimal;//湿度小数
extern uint8_t temp_integer ;//温度整数
extern uint8_t temp_decimal ;//温度小数
extern uint8_t dht11_check ;//校验值
uint8_t DHT11_Read(void)
{
    uint8_t dht11_check=0;
    bsp_io_level_t state;
    uint16_t delay_dht=0xffff;

    //DHT启动时序DATA设为输出状态
    R_IOPORT_PinCfg(&g_ioport_ctrl,BSP_IO_PORT_01_PIN_12,((uint32_t) IOPORT_CFG_PORT_DIRECTION_OUTPUT | (uint32_t) IOPORT_CFG_PORT_OUTPUT_HIGH));
    //发送18ms低电平
    R_IOPORT_PinWrite(&g_ioport_ctrl, BSP_IO_PORT_01_PIN_12, BSP_IO_LEVEL_LOW);
    R_BSP_SoftwareDelay(18, BSP_DELAY_UNITS_MILLISECONDS); //延时20ms
    //在输出20-40us高电平
    R_IOPORT_PinWrite(&g_ioport_ctrl, BSP_IO_PORT_01_PIN_12, BSP_IO_LEVEL_HIGH);
//    R_BSP_SoftwareDelay(30, BSP_DELAY_UNITS_MICROSECONDS); //延时30us
    //DATA设为输入状态
    R_IOPORT_PinCfg(&g_ioport_ctrl,BSP_IO_PORT_01_PIN_12,((uint32_t) IOPORT_CFG_PORT_DIRECTION_INPUT));
    //等待DATA响应低电平,后变为高电平
    do{
        delay_dht--;
        if (!delay_dht)
            return 0;
        R_IOPORT_PinRead(&g_ioport_ctrl, BSP_IO_PORT_01_PIN_12, &state);
    }while(state==BSP_IO_LEVEL_HIGH);
    do{
        delay_dht--;
        if (!delay_dht)
            return 0;
        R_IOPORT_PinRead(&g_ioport_ctrl, BSP_IO_PORT_01_PIN_12, &state);
    }while(state==BSP_IO_LEVEL_LOW);
    do{
        delay_dht--;
        if (!delay_dht)
            return 0;
        R_IOPORT_PinRead(&g_ioport_ctrl, BSP_IO_PORT_01_PIN_12, &state);
    }while(state==BSP_IO_LEVEL_HIGH);

    humdity_integer = DHT11_ReadByte();
    humdity_decimal = DHT11_ReadByte();
    temp_integer = DHT11_ReadByte();
    temp_decimal = DHT11_ReadByte();
    dht11_check = DHT11_ReadByte();
    if (((humdity_integer+humdity_decimal+temp_integer+temp_decimal)&0xff)==dht11_check)
        return 1;
    else
        return 0;
}


