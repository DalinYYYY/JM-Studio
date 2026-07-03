# MotorInfo配置参数

**控制参数 存放在 EEPROM \+ FLASH（备份）**

配套配置表：`motor_info.csv`（每行一个参数，脚本据此生成 C 结构体 / 校验 / Flash 读写代码）。

## 一、1024B空间精准分配

```Plain Text
总参数区（1024B = 0x400）
┌───────────────────────────
│ 全局头部（64B）                              0x0000
│ 子块1：系统级参数 SystemParam（64B）          0x0040  含运维数据
│ 子块2：电机标定参数 MotorCalibParam（128B）   0x0080  含减速比/编码器/功率级/电流采样
│ 子块3：设备参数 DeviceParam（64B）            0x0100
│ 子块4：控制参数 ControlParam（320B）          0x0140  三环PID+前馈+滤波+预留
│ 子块5：保护与通信参数 ProtectCommParam（128B）0x0280
│ 子块6：高级算法参数 AdvancedAlgoParam（64B）  0x0300
│ 预留区（192B）                                0x0340
└────────────────────────────
```

### 1.1 子块偏移速查

| 子块 | 结构体 | 起始偏移 | 大小 | 已用 | 预留 |
|------|--------|----------|------|------|------|
| 头部 | ParamHeader_t | 0x0000 | 64B | 64B | 0 |
| 1 | SystemParam_t | 0x0040 | 64B | 20B | 44B |
| 2 | MotorCalibParam_t | 0x0080 | 128B | 108B | 20B |
| 3 | DeviceParam_t | 0x0100 | 64B | 32B | 32B |
| 4 | ControlParam_t | 0x0140 | 320B | 88B | 232B |
| 5 | ProtectCommParam_t | 0x0280 | 128B | 44B | 84B |
| 6 | AdvancedAlgoParam_t | 0x0300 | 64B | 44B | 20B |
| — | reserved | 0x0340 | 192B | — | 192B |

> CSV 中 `BlockOffset` 列即对应每个参数所属子块的起始偏移（块级偏移）。
> 单个参数的绝对偏移 = `BlockOffset` + 该参数在块内按 `DataType` 顺序累加的字节数（4字节对齐），由生成脚本计算。

## 二、CSV 字段说明

| 列名 | 含义 | 用途 |
|------|------|------|
| VariableName | C 变量名 | 生成结构体字段、get/set 接口名 |
| NameZh | 中文说明 | 注释 |
| Category | 所属子块名 | 分组到对应结构体；决定 BlockOffset |
| Access | 读写属性 | RW 生成 get/set；RO 只生成 get |
| DataType | C 数据类型 | float / uint32_t / int32_t / uint16_t ... |
| DefaultValue | 默认值 | 生成 `motor_param_init` 默认配置 |
| Min / Max | 取值范围 | 生成 `motor_param_validate` 范围校验 |
| Unit | 单位 | 注释 |
| BlockOffset | 子块起始偏移(hex) | Flash 寻址、生成偏移表 |
| Index | 参数全局序号(1起) | 协议 param_id、偏移表索引 |
| Remarks | 补充说明 | 注释 |

## 三、完整数据结构定义

### 3.1 全局头部与索引表

```C
#define __ALIGNED_4 __attribute__((aligned(4)))

// 魔数："SERVO_V2"
#define PARAM_MAGIC 0x53455256
// 总参数区大小（固定1024B）
#define PARAM_AREA_SIZE 1024
// 最大子块数量（头部 64B 限制：12B 基础字段 + 6*8B 索引 + 4B pad = 64B）
#define MAX_BLOCK_COUNT 6

// 子块索引项（8B）
typedef struct __ALIGNED_4 {
    uint32_t offset;    // 子块偏移（字节）
    uint32_t size;      // 子块大小（字节，4的倍数）
} BlockIndex_t;

// 全局头部（64B）
typedef struct __ALIGNED_4 {
    uint32_t magic;            // 4B
    uint16_t version_major;    // 2B
    uint16_t version_minor;    // 2B
    uint32_t crc32;            // 4B
    BlockIndex_t blocks[MAX_BLOCK_COUNT]; // 6*8 = 48B
    uint32_t reserved;         // 4B pad → 共 64B
} ParamHeader_t;
```

### 3.2 各子块数据结构

> 字段顺序与 `motor_info.csv` 中同一 Category 下的行顺序完全一致；所有字段 4 字节对齐。

#### 子块1：系统级参数 SystemParam_t（64B，偏移0x0040）

```C
typedef struct __ALIGNED_4 {
    uint32_t config_version;     // 主版本:次版本(高16位.低16位)
    uint32_t enable_uart;        // bit0:UART bit1:CAN bit2:CANFD bit3:USB
    uint32_t enable_bus_sensor;  // 0:禁用 1:启用
    uint32_t safety_limit;       // 0:禁用 1:启用
    uint32_t total_runtime_s;    // 累计运行时间(s) 掉电保存 定期写入避免频繁擦写
    uint32_t reserved[11];       // 44B 预留
} SystemParam_t;
```

#### 子块2：电机标定参数 MotorCalibParam_t（128B，偏移0x0080）

```C
typedef struct __ALIGNED_4 {
    uint32_t is_calibrated;              // 0:未校准 1:已校准
    uint32_t pole_pairs;                 // 极对数
    uint32_t motor_type;                 // 0:SPMSM 1:IPMSM 2:BLDC
    uint32_t direction;                  // 0:正向 1:反向
    float phase_resistance;              // 相电阻 (ohm)
    float phase_inductance_d;            // d轴相电感 (H)
    float phase_inductance_q;            // q轴相电感 (H)
    float flux_linkage;                  // 永磁体磁链 (Wb)
    float torque_constant;               // 转矩常数 (Nm/A)
    float rotor_inertia;                 // 转子惯量 (kg·m²)
    float friction_coulomb;              // 库仑摩擦力矩 (Nm)
    float friction_viscous;              // 粘滞摩擦系数 (Nm/(rad/s))
    float gear_ratio;                    // 减速比 = 电机转速/输出转速
    float gear_efficiency;               // 减速器效率 0~1
    float calibration_current;           // 电阻电感校准电流 (A)
    float resistance_calib_max_voltage;  // 电阻校准最大电压 (V)
    float current_lim;                   // 峰值电流限制 (A)
    float current_control_bandwidth;     // 电流环带宽 (Hz)
    // ---- 编码器参数（校准后保存，FOC换相必需）----
    uint32_t enc_type;                   // 1:MT6701 2:MT6835 0:ABZ增量 3:霍尔
    uint32_t enc_lines;                  // SPI绝对值为分辨率 增量式为CPR
    int32_t enc_direction;               // 1:正向 -1:反向
    float enc_offset;                    // 编码器初始位置偏移 (deg)
    float elec_angle_bias;               // 电角度偏移 (rad) 校准后保存 最关键
    // ---- 功率级硬件（同板不同电机/不同功率器件）----
    uint32_t pwm_freq_hz;                // PWM载波频率 (Hz)
    float dead_time_ns;                  // PWM死区时间 (ns)
    // ---- 电流采样硬件（同板不同电机/不同板）----
    float shunt_resistance;              // 电流采样电阻 (ohm)
    float current_amp_gain;              // 电流放大增益（运放增益）
    uint32_t reserved[5];                // 20B 预留（齿槽转矩/磁链饱和补偿）
} MotorCalibParam_t;
```

#### 子块3：设备参数 DeviceParam_t（64B，偏移0x0100）

```C
typedef struct __ALIGNED_4 {
    float device_zero;           // 机械零点位置 (rad)
    uint32_t device_time;        // 生产日期 YYYYMMDD
    uint32_t can_id;             // 11位标准ID
    uint32_t can_baudrate;       // (bps)
    float can_timeout_s;         // 0=禁用超时 (s)
    uint32_t can_fd_enable;      // 0:传统CAN 1:CAN FD
    uint32_t can_fd_baudrate;    // (bps)
    uint32_t uart_baudrate;      // (bps)
    uint32_t reserved[8];        // 32B 预留
} DeviceParam_t;
```

#### 子块4：控制参数 ControlParam_t（320B，偏移0x0140）

320B 超大控制参数区，足够容纳：三环PID + 前馈 + 滤波 + 6个独立陷波 + 输入整形 + 扰动观测器 + 摩擦补偿（预留 232B）。

```C
typedef struct __ALIGNED_4 {
    // ---------------- 电流环（32B） ----------------
    float kp_ld;                    // d轴比例增益 (V/A)
    float ki_ld;                    // d轴积分增益 (V/(A·s))
    float kp_lq;                    // q轴比例增益 (V/A)
    float ki_lq;                    // q轴积分增益 (V/(A·s))
    float integral_limit;           // 积分限幅 (V)
    float decoupling_gain;          // dq轴解耦增益 0~1
    float comp_du_V;                // 死区补偿电压 (V)
    float pwm_duty_max;             // PWM最大占空比 0~1

    // ---------------- 速度环（32B） ----------------
    float kp_s;                     // 速度环比例增益 (A/(rad/s))
    float ki_s;                     // 速度环积分增益 (A/rad)
    float speed_integral_limit;     // 速度环积分限幅 (A)
    float vff;                      // 速度前馈系数 0~1
    float aff;                      // 加速度前馈系数 0~1
    float jerk_ff;                  // 加加速度前馈系数 0~1
    float speed_filter_alpha;       // 一阶低通滤波系数
    uint32_t speed_filter_enable;   // 0:禁用 1:启用

    // ---------------- 位置环（24B） ----------------
    float kp_p;                     // 位置环比例增益 (Hz)
    float ki_p;                     // 位置环积分增益 (1/s)
    float position_integral_limit;  // 位置环积分限幅 (rad)
    float position_filter_alpha;    // 一阶低通滤波系数
    uint32_t position_filter_enable;// 0:禁用 1:启用
    float following_error_limit;    // 位置跟随误差保护阈值 (P)

    // ---------------- 预留（232B） ----------------
    uint32_t reserved[58];          // 陷波/输入整形/扰动观测器等
} ControlParam_t;
```

#### 子块5：保护与通信参数 ProtectCommParam_t（128B，偏移0x0280）

```C
typedef struct __ALIGNED_4 {
    // 电气保护
    float over_current_A;                // 过流保护阈值 (A)
    float over_voltage_V;                // 过压保护阈值 (V)
    float under_voltage_V;               // 欠压保护阈值 (V)
    float over_temp_drive;               // 驱动器过温阈值 (℃)
    float over_temp_motor;               // 电机过温阈值 (℃)
    float under_temp_d;                  // 欠温保护阈值 (℃)

    // 运动保护
    float over_speed_rad_s;              // 过速保护阈值 (rad/s)
    int32_t position_following_error_p;  // 位置跟随误差保护 (P)
    int32_t pos_limit_min;               // 硬件位置下限 (P)
    int32_t pos_limit_max;               // 硬件位置上限 (P)
    uint32_t error_enable_mask;          // bit0:过流 bit1:过压 bit2:欠压 ...

    // 预留（84B）
    uint32_t reserved[21];
} ProtectCommParam_t;
```

#### 子块6：高级算法参数 AdvancedAlgoParam_t（64B，偏移0x0300）

```C
typedef struct __ALIGNED_4 {
    // MIT阻抗控制
    float mit_kp;                   // 位置刚度 (Nm/rad)
    float mit_kd;                   // 速度阻尼 (Nm/(rad/s))
    float mit_max_current;          // 最大电流 (A)
    float mit_feedforward_torque;   // 前馈力矩 (Nm)

    // 基础力控
    float force_kp;                 // 力控比例增益 (A/Nm)
    float force_ki;                 // 力控积分增益 (A/(Nm·s))
    float force_limit;              // 力控力矩限制 (Nm)
    uint32_t force_control_enable;  // 0:禁用 1:启用

    // 回零参数
    uint32_t homing_method;         // 0:当前位置回零 1:限位回零
    float homing_speed;             // 回零速度 (rad/s)
    float homing_offset;            // 回零偏移 (rad)

    // 预留（20B）
    uint32_t reserved[5];
} AdvancedAlgoParam_t;
```

### 3.3 总参数区联合体

```C
typedef union __ALIGNED_4 {
    uint8_t raw[PARAM_AREA_SIZE];      // 原始字节数组
    struct {
        ParamHeader_t header;            // 0x0000
        SystemParam_t system;            // 0x0040
        MotorCalibParam_t motor_calib;   // 0x0080
        DeviceParam_t device;            // 0x0100
        ControlParam_t control;          // 0x0140
        ProtectCommParam_t protect_comm; // 0x0280
        AdvancedAlgoParam_t advanced;    // 0x0300
        uint8_t reserved[192];           // 0x0340-0x03FF
    } blocks;
} MotorInfoParam_t;
```

## 四、生成脚本约定（后续实现）

脚本读取 `motor_info.csv` 后应产出：

1. **结构体定义**：按 `Category` 分组，字段顺序与 CSV 行顺序一致，类型取自 `DataType`。
2. **默认值初始化**：`xxx_init()` 用 `DefaultValue` 填充，浮点补 `f` 后缀。
3. **范围校验**：`xxx_validate()` 用 `Min`/`Max` 逐字段检查，越界返回首个 `Index`。
4. **偏移表**：以 `BlockOffset` 为块基址，按字段顺序累加 4 字节对齐偏移，生成 `param_offset_tbl[]`（与协议层 `jm_proto_ops.c` 的 `s_param_tbl` 对应）。
5. **Flash 读写**：按子块 `BlockOffset` + 块大小整块读写，头部校验 `magic`/`crc32`。
