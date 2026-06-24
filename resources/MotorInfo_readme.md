# MotorInfo配置参数

**控制参数 存放在EEPROM \+ FLASH（备份）**

## 一、1024B空间精准分配

```Plain Text
总参数区（1024B = 0x400）
┌───────────────────────────
│ 全局头部（64B）                        
│ 子块1：系统级参数（64B）                
│ 子块2：电机标定参数（128B）                  
│ 子块3：设备参数（64B）                  
│ 子块4：控制参数（320B）（三环PID+6个陷波+输入整形+扰动观测器+预留）        │
│ 子块5：保护与通信参数（128B）      
│ 子块6：高级算法参数（64B）       
│ 预留区（192B）   
└────────────────────────────
```

## 二、完整数据结构定义

### 2\.1 全局头部与索引表

```C
#define __ALIGNED_4 __attribute__((aligned(4)))

// 魔数："SERVO_V2"
#define PARAM_MAGIC 0x53455256
// 总参数区大小（固定1024B）
#define PARAM_AREA_SIZE 1024
// 最大子块数量
#define MAX_BLOCK_COUNT 8

// 子块索引项（8B）
typedef struct __ALIGNED_4 {
    uint32_t offset;    // 子块偏移（字节）
    uint32_t size;      // 子块大小（字节，4的倍数）
} BlockIndex_t;

// 全局头部（64B）
typedef struct __ALIGNED_4 {
    uint32_t magic;
    uint16_t version_major;
    uint16_t version_minor;
    uint32_t crc32;
    BlockIndex_t blocks[MAX_BLOCK_COUNT];
} ParamHeader_t;
```

### 2\.2 各子块数据结构

#### 子块1：系统级参数（64B，偏移0x0040）

```C
typedef struct __ALIGNED_4 {
    uint32_t config_version;
    uint32_t enable_uart;       // bit0:UART, bit1:CAN, bit2:CANFD, bit3:USB
    uint32_t system_flag;
    uint32_t enable_bus_sensor;
    uint32_t safety_limit;
    uint32_t reserved[11];       // 剩余36B预留
} SystemParam_t;
```

#### 子块2：电机标定参数（128B，偏移0x0080）

```C
typedef struct __ALIGNED_4 {
    uint32_t is_calibrated;
    uint32_t pole_pairs;
    float calibration_current;
    float resistance_calib_max_voltage;
    float phase_inductance_d;    // d轴电感
    float phase_inductance_q;    // q轴电感
    float phase_resistance;      // 相电阻
    uint32_t direction;          // 编码器方向：0正向，1反向
    uint32_t motor_type;
    float current_lim;
    float current_control_bandwidth;
    float flux_linkage;          // 永磁体磁链
    float torque_constant;       // 转矩常数
    float rotor_inertia;         // 转子惯量
    float friction_coulomb;      // 库仑摩擦
    float friction_viscous;      // 粘滞摩擦
    uint32_t reserved[16];       // 剩余64B预留（齿槽转矩/磁链饱和补偿）
} MotorCalibParam_t;
```

#### 子块3：设备参数（64B，偏移0x0100）

```C
typedef struct __ALIGNED_4 {
    float device_zero;
    float device_time;
    
    // CAN通信
    uint32_t can_id;
    uint32_t can_baudrate;
    float can_timeout_s;
    uint32_t can_fd_enable;
    uint32_t can_fd_baudrate;

    // UART通信
    uint32_t uart_baudrate;
    uint32_t uart_parity;
    uint32_t uart_stop_bits;
    uint32_t reserved[14];        
} DeviceParam_t;
```

#### 子块4：控制参数（320B，偏移0x0140，核心优化）

**320B超大控制参数区**，足够容纳：

- 三环PID\+前馈\+滤波

- 6个独立陷波滤波器

- 输入整形

- 扰动观测器

- 摩擦补偿

- 60B预留空间（可加滑模/自适应控制等）

```C
typedef struct __ALIGNED_4 {
    // -------------------------- 电流环（32B） --------------------------
    float kp_ld;
    float ki_ld;
    float kp_lq;
    float ki_lq;
    float integral_limit;
    float decoupling_gain;
    float comp_du_V;
    float pwm_duty_max;

    // -------------------------- 速度环（32B） --------------------------
    float kp_s;
    float ki_s;
    float speed_integral_limit;
    float vff;              // 速度前馈
    float aff;              // 加速度前馈
    float jerk_ff;          // 加加速度前馈
    float speed_filter_alpha;
    uint32_t speed_filter_enable;

    // -------------------------- 位置环（32B） --------------------------
    float kp_p;
    float ki_p;             // 位置环积分
    float position_integral_limit;
    float position_filter_alpha;
    uint32_t position_filter_enable;
    float following_error_limit;
    uint32_t reserved_pos[2];

    // -------------------------- 预留（60B） --------------------------
    uint32_t reserved[15];
} ControlParam_t;
```

#### 子块5：保护与通信参数（128B，偏移0x0280）

```C
typedef struct __ALIGNED_4 {
    // 电气保护
    float over_current_A;
    float over_voltage_V;
    float under_voltage_V;
    float over_temp_drive;
    float over_temp_motor;
    float under_temp_d;

    // 运动保护
    float over_speed_rad_s;
    int32_t position_following_error_p;
    int32_t pos_limit_min;
    int32_t pos_limit_max;
    uint32_t error_enable_mask;

    // 预留（64B）
    uint32_t reserved[24];
} ProtectCommParam_t;
```

#### 子块6：高级算法参数（64B，偏移0x0300）

```C
typedef struct __ALIGNED_4 {
    // MIT阻抗控制
    float mit_kp;
    float mit_kd;
    float mit_max_current;
    float mit_feedforward_torque;

    // 基础力控
    float force_kp;
    float force_ki;
    float force_limit;
    uint32_t force_control_enable;

    // 回零参数
    uint32_t homing_method;
    float homing_speed;
    float homing_offset;

    // 预留（28B）
    uint32_t reserved[7];
} AdvancedAlgoParam_t;
```

### 2\.3 总参数区联合体

```C
typedef union __ALIGNED_4 {
    uint8_t raw[PARAM_AREA_SIZE];      // 原始字节数组
    struct {
        ParamHeader_t header;
        SystemParam_t system;          // 0x0040
        MotorCalibParam_t motor_calib; // 0x0080
        DeviceParam_t device;          // 0x0100
        ControlParam_t control;        // 0x0140
        ProtectCommParam_t protect_comm; // 0x0280
        AdvancedAlgoParam_t advanced;  // 0x0300
        uint8_t reserved[192];         // 0x0340-0x03FF，192B预留
    } blocks;
} MotorInfoParam_t;
```



