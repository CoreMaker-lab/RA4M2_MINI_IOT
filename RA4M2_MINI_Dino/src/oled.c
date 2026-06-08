#include "oled.h"
#include "stdlib.h"
#include "oledfont.h"

#include "hal_data.h"
extern fsp_err_t err;
extern int  timeout_ms;
extern  i2c_master_event_t i2c_event ;

u8 OLED_GRAM[144][8];
//反显函数
void OLED_ColorTurn(u8 i)
{
    if(i==0)
        {
            OLED_WR_Byte(0xA6,OLED_CMD);//正常显示
        }
    if(i==1)
        {
            OLED_WR_Byte(0xA7,OLED_CMD);//反色显示
        }
}

//屏幕旋转180度
void OLED_DisplayTurn(u8 i)
{
    if(i==0)
        {
            OLED_WR_Byte(0xC8,OLED_CMD);//正常显示
            OLED_WR_Byte(0xA0,OLED_CMD);
        }
    if(i==1)
        {
            OLED_WR_Byte(0xC0,OLED_CMD);//反转显示
            OLED_WR_Byte(0xA1,OLED_CMD);
        }
}



//写入一个字节
void Send_Byte(u8 dat)
{
//    u8 i;
//    for(i=0;i<8;i++)
//    {
//        if(dat&0x80)//将dat的8位从最高位依次写入
//        {
//            OLED_SDA_Set();
//    }
//        else
//        {
//            OLED_SDA_Clr();
//    }
//        IIC_delay();
//        OLED_SCL_Set();
//        IIC_delay();
//        OLED_SCL_Clr();//将时钟信号设置为低电平
//        dat<<=1;
//  }

    R_IIC_MASTER_SlaveAddressSet(&g_i2c_master0_ctrl, 0x3C, I2C_MASTER_ADDR_MODE_7BIT);
    assert(FSP_SUCCESS == err);

    err = R_IIC_MASTER_Write(&g_i2c_master0_ctrl, &dat, 1, true);
     assert(FSP_SUCCESS == err);
    /* Since there is nothing else to do, block until Callback triggers*/
    //while ((I2C_MASTER_EVENT_TX_COMPLETE != i2c_event) && timeout_ms)
    while ((I2C_MASTER_EVENT_TX_COMPLETE != i2c_event) && timeout_ms>0)
    {
        R_BSP_SoftwareDelay(100U, BSP_DELAY_UNITS_MICROSECONDS);
        timeout_ms--;
    }
    if (I2C_MASTER_EVENT_ABORTED == i2c_event)
    {
        __BKPT(0);
    }
    /* Read data back from the I2C slave */
    i2c_event = I2C_MASTER_EVENT_ABORTED;
    timeout_ms           = 100000;




}

//发送一个字节
//mode:数据/命令标志 0,表示命令;1,表示数据;
void OLED_WR_Byte(u8 dat,u8 mode)
{
//    I2C_Start();
//    Send_Byte(0x78);
//    I2C_WaitAck();
//    if(mode){Send_Byte(0x40);}
//  else{Send_Byte(0x00);}
//    I2C_WaitAck();
//    Send_Byte(dat);
//    I2C_WaitAck();
//    I2C_Stop();

    R_IIC_MASTER_SlaveAddressSet(&g_i2c_master0_ctrl, 0x3C, I2C_MASTER_ADDR_MODE_7BIT);
    assert(FSP_SUCCESS == err);
    uint8_t ii[2]={0x00,0x00};
    if(mode)
        ii[0] = 0x40;
      else
        ii[0] = 0x00;
    ii[1] = dat;

    err = R_IIC_MASTER_Write(&g_i2c_master0_ctrl, ii, 0x02, true);
    assert(FSP_SUCCESS == err);
    /* Since there is nothing else to do, block until Callback triggers*/
    //while ((I2C_MASTER_EVENT_TX_COMPLETE != i2c_event) && timeout_ms)
    while ((I2C_MASTER_EVENT_TX_COMPLETE != i2c_event) && timeout_ms>0)
    {
     R_BSP_SoftwareDelay(100U, BSP_DELAY_UNITS_MICROSECONDS);
     timeout_ms--;
    }
    if (I2C_MASTER_EVENT_ABORTED == i2c_event)
    {
        __BKPT(0);
    }
    /* Read data back from the I2C slave */
    i2c_event = I2C_MASTER_EVENT_ABORTED;
    timeout_ms           = 100000;




}

//开启OLED显示
void OLED_DisPlay_On(void)
{
    OLED_WR_Byte(0x8D,OLED_CMD);//电荷泵使能
    OLED_WR_Byte(0x14,OLED_CMD);//开启电荷泵
    OLED_WR_Byte(0xAF,OLED_CMD);//点亮屏幕
}

//关闭OLED显示
void OLED_DisPlay_Off(void)
{
    OLED_WR_Byte(0x8D,OLED_CMD);//电荷泵使能
    OLED_WR_Byte(0x10,OLED_CMD);//关闭电荷泵
    OLED_WR_Byte(0xAE,OLED_CMD);//关闭屏幕
}

//更新显存到OLED
void OLED_Refresh(void)
{
//    u8 i,n;
//    for(i=0;i<8;i++)
//    {
//        OLED_WR_Byte(0xb0+i,OLED_CMD); //设置行起始地址
//        OLED_WR_Byte(0x00,OLED_CMD);   //设置低列起始地址
//        OLED_WR_Byte(0x10,OLED_CMD);   //设置高列起始地址
//        I2C_Start();
//        Send_Byte(0x78);
//        I2C_WaitAck();
//        Send_Byte(0x40);
//        I2C_WaitAck();
//        for(n=0;n<128;n++)
//        {
//            Send_Byte(OLED_GRAM[n][i]);
//            I2C_WaitAck();
//        }
//        I2C_Stop();
//  }

    uint8_t i, n;
    fsp_err_t err;
    uint8_t buf[129]; // 控制字节 + 128 字节数据
    buf[0] = 0x40;     // 控制字节（0x40 表示后续为数据）
    for(i = 0; i < oled_y; i++)
    {
        OLED_WR_Byte(0xB0 + i, OLED_CMD); // 设置页地址
        OLED_WR_Byte(0x00 , OLED_CMD);     // 设置低列地址
        OLED_WR_Byte(0x10, OLED_CMD);     // 设置高列地址

        for(n = 0; n < oled_x; n++)
        {
            buf[n + 1] = OLED_GRAM[n][i]; // 填充数据部分
        }

        // 设置 OLED I2C 地址（建议只设置一次，若已设置可略过）
        R_IIC_MASTER_SlaveAddressSet(&g_i2c_master0_ctrl, 0x3C, I2C_MASTER_ADDR_MODE_7BIT);

        // 发送整帧显示数据（129字节）
        err = R_IIC_MASTER_Write(&g_i2c_master0_ctrl, buf, sizeof(buf), true);
        assert(FSP_SUCCESS == err);

        // 等待写入完成
        while ((I2C_MASTER_EVENT_TX_COMPLETE != i2c_event) && timeout_ms > 0)
        {
            R_BSP_SoftwareDelay(100U, BSP_DELAY_UNITS_MICROSECONDS);
            timeout_ms--;
        }

        if (I2C_MASTER_EVENT_ABORTED == i2c_event)
        {
            __BKPT(); // 出错断点
        }

        i2c_event = I2C_MASTER_EVENT_ABORTED;
        timeout_ms = 100000;
    }


    //    u8 i,n;
    //    for(i=0;i<oled_y;i++)
    //    {
    //        OLED_WR_Byte(0xb0+i,OLED_CMD); //设置行起始地址
    //        OLED_WR_Byte(0x00,OLED_CMD);   //设置低列起始地址
    //        OLED_WR_Byte(0x10,OLED_CMD);   //设置高列起始地址
    //        I2C_Start();
    //        Send_Byte(0x78);
    //        I2C_WaitAck();
    //        Send_Byte(0x40);
    //        I2C_WaitAck();
    //       for(n=0;n<oled_x;n++)
    //        {
    //            Send_Byte(OLED_GRAM[n][i]);
    //            I2C_WaitAck();
    //        }
    //        I2C_Stop();


//        /* ---- 1. 一次性设置 7-bit 从地址（如果你已在 BSP 中配置过，可删掉这行） ---- */
//        R_IIC_MASTER_SlaveAddressSet(&g_i2c_master0_ctrl,
//                                     0x3C,        /* SSD1312 默认地址：0x3C */
//                                     I2C_MASTER_ADDR_MODE_7BIT);
//        uint8_t buf[1 + oled_x];
//        buf[0] = 0x40;               /* 0x40 = “接下来是数据” 的控制字节 */
//
//
//        /* ---- 3. 逐页刷新 ---- */
//         for (uint8_t page = 0; page < oled_y; page++)
//         {
//             /* 3-1 设置页地址 & 列起始地址（仍用原有写命令函数） */
//             OLED_WR_Byte(0xB0 | page, OLED_CMD);      /* Page Address */
//             OLED_WR_Byte(0x00,         OLED_CMD);     /* Low Column  = 0 */
//             OLED_WR_Byte(0x10,         OLED_CMD);     /* High Column = 0 */
//
//             /* 3-2 把本页像素复制到发送缓冲（buf[1] ~ buf[oled_x]） */
//             for (uint8_t col = 0; col < oled_x; col++)
//             {
//                 buf[col + 1] = OLED_GRAM[col][page];
//             }
//
//
//             fsp_err_t err = R_IIC_MASTER_Write(&g_i2c_master0_ctrl,
//                                                buf,
//                                                (uint32_t)(oled_x + 1),  /* 总字节数 */
//                                                false);                  /* 发送 Stop 条件 */
//                     // 等待写入完成
//                     while ((I2C_MASTER_EVENT_TX_COMPLETE != i2c_event) && timeout_ms > 0)
//                     {
//                         R_BSP_SoftwareDelay(100U, BSP_DELAY_UNITS_MICROSECONDS);
//                         timeout_ms--;
//                     }
//
//                     if (I2C_MASTER_EVENT_ABORTED == i2c_event)
//                     {
//                         __BKPT(); // 出错断点
//                     }
//
//                     i2c_event = I2C_MASTER_EVENT_ABORTED;
//                     timeout_ms = 100000;
//         }









}
//清屏函数
void OLED_Clear(void)
{
    u8 i,n;
    for(i=0;i<8;i++)
    {
       for(n=0;n<128;n++)
            {
             OLED_GRAM[n][i]=0;//清除所有数据
            }
  }
    OLED_Refresh();//更新显示
}


//画点
//x:0~127
//y:0~63
//t:1 填充 0,清空
void OLED_DrawPoint(u8 x,u8 y,u8 t)
{
    u8 i,m,n;
    i=y/8;
    m=y%8;
    n=1<<m;
    if(t){OLED_GRAM[x][i]|=n;}
    else
    {
        OLED_GRAM[x][i]=~OLED_GRAM[x][i];
        OLED_GRAM[x][i]|=n;
        OLED_GRAM[x][i]=~OLED_GRAM[x][i];
    }
}

//画线
//x1,y1:起点坐标
//x2,y2:结束坐标
void OLED_DrawLine(u8 x1,u8 y1,u8 x2,u8 y2,u8 mode)
{
    u16 t;
    int xerr=0,yerr=0,delta_x,delta_y,distance;
    int incx,incy,uRow,uCol;
    delta_x=x2-x1; //计算坐标增量
    delta_y=y2-y1;
    uRow=x1;//画线起点坐标
    uCol=y1;
    if(delta_x>0)incx=1; //设置单步方向
    else if (delta_x==0)incx=0;//垂直线
    else {incx=-1;delta_x=-delta_x;}
    if(delta_y>0)incy=1;
    else if (delta_y==0)incy=0;//水平线
    else {incy=-1;delta_y=-delta_x;}
    if(delta_x>delta_y)distance=delta_x; //选取基本增量坐标轴
    else distance=delta_y;
    for(t=0;t<distance+1;t++)
    {
        OLED_DrawPoint(uRow,uCol,mode);//画点
        xerr+=delta_x;
        yerr+=delta_y;
        if(xerr>distance)
        {
            xerr-=distance;
            uRow+=incx;
        }
        if(yerr>distance)
        {
            yerr-=distance;
            uCol+=incy;
        }
    }
}
//x,y:圆心坐标
//r:圆的半径
void OLED_DrawCircle(u8 x,u8 y,u8 r)
{
    int a, b,num;
    a = 0;
    b = r;
    while(2 * b * b >= r * r)      
    {
        OLED_DrawPoint(x + a, y - b,1);
        OLED_DrawPoint(x - a, y - b,1);
        OLED_DrawPoint(x - a, y + b,1);
        OLED_DrawPoint(x + a, y + b,1);
 
        OLED_DrawPoint(x + b, y + a,1);
        OLED_DrawPoint(x + b, y - a,1);
        OLED_DrawPoint(x - b, y - a,1);
        OLED_DrawPoint(x - b, y + a,1);
        
        a++;
        num = (a * a + b * b) - r*r;//计算画的点离圆心的距离
        if(num > 0)
        {
            b--;
            a--;
        }
    }
}



//在指定位置显示一个字符,包括部分字符
//x:0~127
//y:0~63
//size1:选择字体 6x8/6x12/8x16/12x24
//mode:0,反色显示;1,正常显示
void OLED_ShowChar(u8 x,u8 y,u8 chr,u8 size1,u8 mode)
{
    u8 i,m,temp,size2,chr1;
    u8 x0=x,y0=y;
    if(size1==8)size2=6;
    else size2=(size1/8+((size1%8)?1:0))*(size1/2);  //得到字体一个字符对应点阵集所占的字节数
    chr1=chr-' ';  //计算偏移后的值
    for(i=0;i<size2;i++)
    {
        if(size1==8)
              {temp=asc2_0806[chr1][i];} //调用0806字体
        else if(size1==12)
        {temp=asc2_1206[chr1][i];} //调用1206字体
        else if(size1==16)
        {temp=asc2_1608[chr1][i];} //调用1608字体
        else if(size1==24)
        {temp=asc2_2412[chr1][i];} //调用2412字体
        else return;
        for(m=0;m<8;m++)
        {
            if(temp&0x01)OLED_DrawPoint(x,y,mode);
            else OLED_DrawPoint(x,y,!mode);
            temp>>=1;
            y++;
        }
        x++;
        if((size1!=8)&&((x-x0)==size1/2))
        {x=x0;y0=y0+8;}
        y=y0;
  }
}


//显示字符串
//x,y:起点坐标
//size1:字体大小
//*chr:字符串起始地址
//mode:0,反色显示;1,正常显示
void OLED_ShowString(u8 x,u8 y,u8 *chr,u8 size1,u8 mode)
{
    while((*chr>=' ')&&(*chr<='~'))//判断是不是非法字符!
    {
        OLED_ShowChar(x,y,*chr,size1,mode);
        if(size1==8)x+=6;
        else x+=size1/2;
        chr++;
  }
}

//m^n
u32 OLED_Pow(u8 m,u8 n)
{
    u32 result=1;
    while(n--)
    {
      result*=m;
    }
    return result;
}

//显示数字
//x,y :起点坐标
//num :要显示的数字
//len :数字的位数
//size:字体大小
//mode:0,反色显示;1,正常显示
void OLED_ShowNum(u8 x,u8 y,u32 num,u8 len,u8 size1,u8 mode)
{
    u8 t,temp,m=0;
    if(size1==8)m=2;
    for(t=0;t<len;t++)
    {
        temp=(num/OLED_Pow(10,len-t-1))%10;
            if(temp==0)
            {
                OLED_ShowChar(x+(size1/2+m)*t,y,'0',size1,mode);
      }
            else
            {
              OLED_ShowChar(x+(size1/2+m)*t,y,temp+'0',size1,mode);
            }
  }
}

//显示汉字
//x,y:起点坐标
//num:汉字对应的序号
//mode:0,反色显示;1,正常显示
void OLED_ShowChinese(u8 x,u8 y,u8 num,u8 size1,u8 mode)
{
    u8 m,temp;
    u8 x0=x,y0=y;
    u16 i,size3=(size1/8+((size1%8)?1:0))*size1;  //得到字体一个字符对应点阵集所占的字节数
    for(i=0;i<size3;i++)
    {
        if(size1==16)
                {temp=Hzk1[num][i];}//调用16*16字体
        else if(size1==24)
                {temp=Hzk2[num][i];}//调用24*24字体
        else if(size1==32)
                {temp=Hzk3[num][i];}//调用32*32字体
        else if(size1==64)
                {temp=Hzk4[num][i];}//调用64*64字体
        else return;
        for(m=0;m<8;m++)
        {
            if(temp&0x01)OLED_DrawPoint(x,y,mode);
            else OLED_DrawPoint(x,y,!mode);
            temp>>=1;
            y++;
        }
        x++;
        if((x-x0)==size1)
        {x=x0;y0=y0+8;}
        y=y0;
    }
}

//num 显示汉字的个数
//space 每一遍显示的间隔
//mode:0,反色显示;1,正常显示
void OLED_ScrollDisplay(u8 num,u8 space,u8 mode)
{
    u8 i,n,t=0,m=0,r;
    while(1)
    {
        if(m==0)
        {
        OLED_ShowChinese(128,24,t,16,mode); //写入一个汉字保存在OLED_GRAM[][]数组中
            t++;
        }
        if(t==num)
            {
                for(r=0;r<16*space;r++)      //显示间隔
                 {
                    for(i=1;i<144;i++)
                        {
                            for(n=0;n<8;n++)
                            {
                                OLED_GRAM[i-1][n]=OLED_GRAM[i][n];
                            }
                        }
           OLED_Refresh();
                 }
        t=0;
      }
        m++;
        if(m==16){m=0;}
        for(i=1;i<144;i++)   //实现左移
        {
            for(n=0;n<8;n++)
            {
                OLED_GRAM[i-1][n]=OLED_GRAM[i][n];
            }
        }
        OLED_Refresh();
    }
}

//x,y：起点坐标
//sizex,sizey,图片长宽
//BMP[]：要写入的图片数组
//mode:0,反色显示;1,正常显示
void OLED_ShowPicture(u8 x,u8 y,u8 sizex,u8 sizey,u8 BMP[],u8 mode)
{
    u16 j=0;
    u8 i,n,temp,m;
    u8 x0=x,y0=y;
    sizey=sizey/8+((sizey%8)?1:0);
    for(n=0;n<sizey;n++)
    {
         for(i=0;i<sizex;i++)
         {
                temp=BMP[j];
                j++;
                for(m=0;m<8;m++)
                {
                    if(temp&0x01)OLED_DrawPoint(x,y,mode);
                    else OLED_DrawPoint(x,y,!mode);
                    temp>>=1;
                    y++;
                }
                x++;
                if((x-x0)==sizex)
                {
                    x=x0;
                    y0=y0+8;
                }
                y=y0;
     }
     }
}
//OLED的初始化
void OLED_Init(void)
{
//    GPIO_InitTypeDef  GPIO_InitStructure;
//    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA, ENABLE);    //使能A端口时钟
//    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_0|GPIO_Pin_1;
//    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_Out_OD;         //推挽输出
//    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;//速度50MHz
//    GPIO_Init(GPIOA, &GPIO_InitStructure);    //初始化PA0,1
//    GPIO_SetBits(GPIOA,GPIO_Pin_0|GPIO_Pin_1);
//
//    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_2;
//    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_Out_PP;         //推挽输出
//    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;//速度50MHz
//    GPIO_Init(GPIOA, &GPIO_InitStructure);    //初始化PA2
//    GPIO_SetBits(GPIOA,GPIO_Pin_2);
//
//    OLED_RES_Clr();
//    delay_ms(200);
//    OLED_RES_Set();


    OLED_WR_Byte(0xAE,OLED_CMD);//--turn off oled panel

    OLED_WR_Byte(0x00,OLED_CMD);    /*set lower column address*/
    OLED_WR_Byte(0x10,OLED_CMD);    /*set higher column address*/

    OLED_WR_Byte(0xB0,OLED_CMD);    /*set page address*/

    OLED_WR_Byte(0x81,OLED_CMD);    /*contract control*/
    OLED_WR_Byte(0x5f,OLED_CMD);    /*128*/

    if(USE_HORIZONTAL==0)
    {
        OLED_WR_Byte(0x20,OLED_CMD);    /* Set Memory addressing mode (0x20/0x21) */
        OLED_WR_Byte(0x09,OLED_CMD);     /* 0x09 */

        OLED_WR_Byte(0xA1,OLED_CMD);    /*set segment remap  0XA1 */
        OLED_WR_Byte(0xC8,OLED_CMD);    /*Com scan direction   0Xc8  */
    }
    else if(USE_HORIZONTAL==90)
    {
        OLED_WR_Byte(0x20,OLED_CMD);    /* Set Memory addressing mode (0x20/0x21) */
        OLED_WR_Byte(0x02,OLED_CMD);     /* 0x02 */

        OLED_WR_Byte(0xA1,OLED_CMD);    /*set segment remap  0XA1 */
        OLED_WR_Byte(0xC0,OLED_CMD);    /*Com scan direction   0Xc0  */
    }
    else if(USE_HORIZONTAL==180)
    {
        OLED_WR_Byte(0x20,OLED_CMD);    /* Set Memory addressing mode (0x20/0x21) */
        OLED_WR_Byte(0x09,OLED_CMD);     /* 0x09 */

        OLED_WR_Byte(0xA0,OLED_CMD);    /*set segment remap  0XA0 */
        OLED_WR_Byte(0xC0,OLED_CMD);    /*Com scan direction   0Xc0  */
    }
    else if(USE_HORIZONTAL==270)
    {
        OLED_WR_Byte(0x20,OLED_CMD);    /* Set Memory addressing mode (0x20/0x21) */
        OLED_WR_Byte(0x02,OLED_CMD);     /* 0x02 */

        OLED_WR_Byte(0xA0,OLED_CMD);    /*set segment remap  0XA0 */
        OLED_WR_Byte(0xC8,OLED_CMD);    /*Com scan direction   0Xc8  */
    }

    OLED_WR_Byte(0xA4,OLED_CMD);    /*Disable Entire Display On (0xA4/0xA5)*/

    OLED_WR_Byte(0xA6,OLED_CMD);    /*normal / reverse*/

    OLED_WR_Byte(0xA8,OLED_CMD);    /*multiplex ratio*/
    OLED_WR_Byte(0x3F,OLED_CMD);    /*duty = 1/64*/

    OLED_WR_Byte(0xD3,OLED_CMD);    /*set display offset*/
    OLED_WR_Byte(0x00,OLED_CMD);    /*   0x20   */

    OLED_WR_Byte(0xD5,OLED_CMD);    /*set osc division*/
    OLED_WR_Byte(0x80,OLED_CMD);

    OLED_WR_Byte(0xD9,OLED_CMD);    /*set pre-charge period*/
    OLED_WR_Byte(0x22,OLED_CMD);

    OLED_WR_Byte(0xDA,OLED_CMD);    /* Set SEG Pins Hardware Configuration */
    OLED_WR_Byte(0x10,OLED_CMD);

    OLED_WR_Byte(0xdb,OLED_CMD);    /*set vcomh*/
    OLED_WR_Byte(0x30,OLED_CMD);

    OLED_WR_Byte(0x8d,OLED_CMD);    /*set charge pump enable*/
    OLED_WR_Byte(0x72,OLED_CMD);    /* 0x12:7.5V; 0x52:8V;  0x72:9V;  0x92:10V */
    OLED_Clear();
    OLED_WR_Byte(0xAF,OLED_CMD);
}

















//// 绘制重启
//void OLED_DrawRestart()
//{
//    unsigned int j=0;
//    unsigned char x, y;
//    unsigned char byte;
//    //OLED_SetPos(0, 0);
//    for (y = 2; y < 5; y++)
//    {
//        OLED_SetPos(52, y);
//        IIC_Start();
//        IIC_WriteByte(0x78);
//        IIC_WaitAck();
//        IIC_WriteByte(0x40);
//        IIC_WaitAck();
//        for (x = 0; x < 24; x++)
//        {
//            byte = RESTART[j++];
//            IIC_WriteByte(byte);
//            IIC_WaitAck();
//        }
//        IIC_Stop();
//    }
//    OLED_ShowString(10, 3, "GAME", 16);
//    OLED_ShowString(86, 3, "OVER", 16);
//}



// 绘制封面
void OLED_DrawCover()
{
//    OLED_DrawBMPFast(COVER);
    OLED_ShowPicture(0, 0, 128, 64, COVER, 1);
}

// 显示地面
void OLED_DrawGround()
{


    OLED_ShowPicture(0, 7*8, 128, 7, GROUND, 1);
}


// 绘制云朵（RA4M2 + OLED_ShowPicture 版）
void OLED_DrawCloud(void)
{
    static int  pos    = 128;     // 云左边缘相对屏幕的 x
    static char height = 0;       // 0,1,2 -> 画到第 1/2/3 页（从 0 开始）
    const uint8_t speed = 3;      // 每帧左移 3 像素
    const int length    = sizeof(CLOUD);   // 云原始宽度（24）
    int start_x = 0;

    // 到最左侧完全离屏后，重置到右侧并随机高度（3 档）
    if (pos + length <= -speed)
    {
        pos    = 128;
        height = rand() % 3;
    }

    // 计算绘制起点和可见宽度（做左右裁剪）
    if (pos < 0) start_x = -pos;                    // 左侧裁掉
    int x_draw  = (pos < 0) ? 0 : pos;              // 屏幕上的实际起点
    int visible = length - start_x;                 // 理论可见宽
    if (visible < 0) visible = 0;
    if (x_draw + visible > 128) visible = 128 - x_draw;   // 右侧裁掉
    if (visible < 0) visible = 0;

    // 追加 speed 个 0x00，模拟原 51 代码写入末尾空列以抹掉“尾巴”
    int trailing = speed;
    if (x_draw + visible + trailing > 128) trailing = 128 - x_draw - visible;
    if (trailing < 0) trailing = 0;

    int total = visible + trailing;
    if (total > 0)
    {
        // 24 + speed <= 27，开个 32 字节的缓冲足够
        static uint8_t line[32];
        for (int i = 0; i < total; i++)
        {
            if (i < visible)
                line[i] = CLOUD[start_x + i];
            else
                line[i] = 0x00;  // 尾部清空列
        }

        // y 放在第 1~3 页（每页 8px），与原来 51 的 page = 1 + height 对齐
        uint8_t y = (uint8_t)((1 + (height % 3)) * 8);

        // 把这一“页条”写进显存；实显在主循环里统一 OLED_Refresh()
        OLED_ShowPicture((uint8_t)x_draw, y, (uint8_t)total, 8, line, 1);
    }

    // 下一帧左移
    pos -= speed;
}

// 绘制小恐龙（RA4M2 + OLED_ShowPicture 版，16x16 / 两帧交替）
// 依赖位图：extern const unsigned char DINO[2][32]; // 每帧16x16=32字节（2页×16列）
// 若你的数组名或尺寸不同，把下面的 DINO[...] 和 16x16 改成你的实际值即可。
void OLED_DrawDino(void)
{
    static unsigned char dino_dir = 0;     // 0/1 两帧切换
    const  unsigned char x = 16;           // 与原代码一致
    const  unsigned char y = 5 * 8;        // page=6 => 像素 y=48

    // 先把上一帧覆盖区域清空（避免残影）
    static const unsigned char ZERO_16x16[16 * 2] = {0}; // 32字节
    OLED_ShowPicture(x, y, 16, 16, (unsigned char*)ZERO_16x16, 1);

    // 切换到下一帧并绘制
    dino_dir ^= 1; // 原来的 dino_dir=(dino_dir+1)%2
    OLED_ShowPicture(x, y, 16, 16, (unsigned char*)DINO[dino_dir], 1);

    // 注意：真正显示到屏幕需要在调用处统一 OLED_Refresh()
}

// 透明绘制：位图1->置白，位图0->不改原背景
static void OLED_ShowPicture_Transparent(u8 x, u8 y, u8 sizex, u8 sizey, const unsigned char *BMP)
{
    u16 j = 0;
    u8 i, n, temp, m;
    u8 x0 = x, y0 = y;
    sizey = sizey/8 + ((sizey%8) ? 1 : 0);
    for (n = 0; n < sizey; n++)
    {
        for (i = 0; i < sizex; i++)
        {
            temp = BMP[j++];
            for (m = 0; m < 8; m++)
            {
                if (temp & 0x01) OLED_DrawPoint(x, y, 1); // 只在位图为1时置像素
                // else: 背景保持不变
                temp >>= 1;
                y++;
            }
            x++;
            if ((x - x0) == sizex) { x = x0; y0 += 8; }
            y = y0;
        }
    }
}

// 8帧，每帧16x24=48字节
int OLED_DrawDinoJump(char reset)
{
    static const unsigned char speed_arr[] = {1,1,3,3,4,4,5,6,7};
    static int  speed_idx = (int)(sizeof(speed_arr) - 1);
    static int  height    = 0;      // 0~31
    static char dir       = 0;      // 0=上升, 1=下落
    static unsigned char prev_y_px = 5 * 8; // ★ 落地时顶边在 page=5 (y=40)

    const unsigned char GROUND_Y_TOP = 7 * 8;         // 地面顶边 y=56
    static const unsigned char ZERO_16x24[16*3] = {0};// 16x24 全零块

    if (reset)
    {
        // 仅清天空部分的旧恐龙，不动第7页的地面
        if (prev_y_px < GROUND_Y_TOP) {
            unsigned char erase_h = (unsigned char)(GROUND_Y_TOP - prev_y_px);
            if (erase_h > 24) erase_h = 24;
            if (erase_h) OLED_ShowPicture(16, prev_y_px, 16, erase_h, (unsigned char*)ZERO_16x24, 1);
        }
        height = 0; dir = 0; speed_idx = (int)(sizeof(speed_arr) - 1);
        prev_y_px = 5 * 8;
        return 0;
    }

    // 高度/速度（与你原来一致）
    if (dir == 0) { height += speed_arr[speed_idx]; if (--speed_idx < 0) speed_idx = 0; }
    else          { height -= speed_arr[speed_idx]; if (++speed_idx > (int)(sizeof(speed_arr)-1)) speed_idx = (int)(sizeof(speed_arr)-1); }
    if (height >= 31) { dir = 1; height = 31; }
    if (height <= 0)  { dir = 0; height = 0;  }

    // 0~7->0, 8~15->1, 16~23->2, 24~31->3
    unsigned char offset = (height<=7)?0 : (height<=15)?1 : (height<=23)?2 : 3;
    unsigned char y_top_px = (unsigned char)((5 - offset) * 8); // 本帧顶边像素

    // ① 清除上一帧：同样只清天空部分（不动地面页）
    if (prev_y_px < GROUND_Y_TOP) {
        unsigned char erase_h = (unsigned char)(GROUND_Y_TOP - prev_y_px);
        if (erase_h > 24) erase_h = 24;
        if (erase_h) OLED_ShowPicture(16, prev_y_px, 16, erase_h, (unsigned char*)ZERO_16x24, 1);
    }

    // ② 绘制本帧：把绘制高度裁剪在地面以上，避免“伸进地面”
    unsigned char draw_h = 0;
    if (y_top_px < GROUND_Y_TOP) {
        draw_h = (unsigned char)(GROUND_Y_TOP - y_top_px);
        if (draw_h > 24) draw_h = 24;        // 通常是 8、16 或 24
    }
    if (draw_h) {
        unsigned char frame = (unsigned char)(height & 0x07);
        OLED_ShowPicture(16, y_top_px, 16, draw_h, (unsigned char*)DINO_JUMP[frame], 1);
    }

    prev_y_px = y_top_px;
    return height;
}



// 绘制随机出现的仙人掌障碍物（RA4M2 / OLED_ShowPicture 版）
// 依赖位图：
//   CACTUS_1: 8x16  （长度=8，2页×8列=16字节）
//   CACTUS_2: 16x16 （长度=16，2页×16列=32字节）
//   CACTUS_3: 24x16 （长度=24，2页×24列=48字节）
//   CACTUS_4: 24x16 （长度=24，2页×24列=48字节）
// 数据布局需与旧代码一致：先上半页（8px高）length字节，再下半页length字节。
int OLED_DrawCactusRandom(unsigned char ver, unsigned char reset)
{
    const unsigned char speed = 5;
    static int pos = 128;                 // 障碍物左边缘相对屏幕的 X
    int start_x = 0;
    int length  = 0;

    // 位图源指针
    const unsigned char *src = NULL;

    // 复位：回到最右侧 & 清掉屏幕左侧一个 speed 宽度的旧痕
    if (reset == 1)
    {
        static const unsigned char ZERO_24x16[24 * (16/8)] = {0};
        OLED_ShowPicture(0, 5 * 8, speed, 16, (unsigned char*)ZERO_24x16, 1);
        pos = 128;
        return 128;
    }

    // 选择不同形态与宽度
    switch (ver)
    {
        case 0: length = 8;  src = CACTUS_1; break;
        case 1: length = 16; src = CACTUS_2; break;
        case 2: length = 24; src = CACTUS_3; break;
        default:             length = 24; src = CACTUS_4; break;
    }

    // ===== 计算裁剪与绘制区 =====
    // 左越界时从 -pos 开始截取，屏幕起点从 0 开始画；否则就从 pos 画
    start_x        = (pos < 0) ? -pos : 0;
    int x_draw     = (pos < 0) ? 0     : pos;
    int visible    = length - start_x;                  // 理论可见宽
    if (visible < 0) visible = 0;
    if (x_draw + visible > 128) visible = 128 - x_draw; // 右侧裁掉
    if (visible < 0) visible = 0;

    // 先画当前可见区域
    if (visible > 0)
    {
        // 最大 24 宽 × 2 页 = 48 字节，开个足够的缓冲
        static unsigned char buf[24 * (16/8)];

        // 上半页（第0页）
        for (int i = 0; i < visible; ++i)
            buf[i] = src[start_x + i];

        // 下半页（第1页）
        for (int i = 0; i < visible; ++i)
            buf[visible + i] = src[length + start_x + i];

        OLED_ShowPicture((unsigned char)x_draw, 5 * 8, (unsigned char)visible, 16, buf, 1);
    }

    // 再清“尾巴”speed列，避免上一帧右侧残影
    int trailing = speed;
    if (x_draw + visible + trailing > 128) trailing = 128 - x_draw - visible;
    if (trailing < 0) trailing = 0;
    if (trailing > 0)
    {
        static const unsigned char ZERO_24x16[24 * (16/8)] = {0};
        OLED_ShowPicture((unsigned char)(x_draw + visible), 5 * 8,
                         (unsigned char)trailing, 16, (unsigned char*)ZERO_24x16, 1);
    }

    // 向左移动
    pos -= speed;

    // 返回旧接口保持一致：返回“更新前”的pos，即 pos + speed
    return pos + speed;
}



// 绘制重启（RA4M2 + OLED_ShowPicture/ShowString 版）
void OLED_DrawRestart(void)
{
    // 24x24 的 RESTART 图标，原来从 page=2 开始 => y = 2*8 = 16 像素
    OLED_ShowPicture(52, 2 * 8, 24, 24, RESTART, 1);

    // 文本：“GAME” 与 “OVER”，原来在 page=3 => y = 3*8 = 24 像素
    OLED_ShowString(10, 3 * 8, (u8*)"GAME", 16, 1);
    OLED_ShowString(86, 3 * 8, (u8*)"OVER", 16, 1);

    // 记得在调用处统一 OLED_Refresh() 把 GRAM 刷到屏幕上
}
