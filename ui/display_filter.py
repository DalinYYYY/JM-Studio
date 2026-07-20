"""显示层数据滤波工具。

针对编码器噪声导致的显示抖动, 提供三种可复用滤波器:
  - AngleDeadZone:       静止死区 + 动态跟随, 适用于转子示意图目标角度
  - MovingAverageAngle:  圆形均值移动平均, 适用于波形图 angle 曲线
  - PrecisionRounder:    精度截断, 适用于数值卡片

所有角度滤波器均考虑周期性, 用 atan2(sin, cos) 处理 0/2π 跨边界跳变。
仅作用于 UI 显示层, 不影响数据链路的原始值(日志/导出仍为原始数据)。
"""

import math

import numpy as np


class AngleDeadZone:
    """位置变化死区。

    位置变化小于阈值时锁定角度不动; 位置变化超过阈值时正常跟随。
    仅用位置变化判断(位置是原始量), 不用速度(速度是位置的微分, 噪声更大:
    0.01rad 噪声 @ 50Hz 会产生 0.5rad/s 瞬时速度, 若用速度做前置判断会偶发误解锁,
    旁路位置死区, 导致 single/pos/multiturn/示意图全部用原始噪声值抖动)。

    锁定状态可通过 is_locked 查询: 调用方据此把噪声速度也置零,
    避免 60fps 航位推算用噪声速度持续推进显示角度, 形成"漂移-拉回"的低频摆动。
    """

    def __init__(self, pos_threshold=0.05):
        """
        :param pos_threshold: 位置变化死区(rad), 小于此值锁定。
                              0.05 rad ≈ 2.86°, 覆盖编码器噪声波动
        """
        self._pos_thr = float(pos_threshold)
        self._locked_th = 0.0          # 锁定的角度
        self.is_locked = True          # 最后一次 update 是否锁定(静止)

    def reset(self, th=0.0):
        """重置锁定角度(切回面板/重连时调用)。"""
        self._locked_th = float(th)
        self.is_locked = True

    def update(self, th_rad: float, vel_rad_s: float = 0.0) -> float:
        """返回滤波后的角度(rad)。位置变化小于阈值时锁定, 否则跟随。

        vel_rad_s 参数保留(调用方传入), 但不参与判断 —— 速度是位置的微分,
        噪声更大, 用速度做前置判断会偶发误解锁旁路位置死区。

        锁定时 is_locked=True, 调用方应把噪声速度也置零,
        避免航位推算用噪声速度积分放大成低频摆动。
        """
        th = float(th_rad)
        # 仅用位置变化判断(atan2 处理 0/2π 跨边界)
        err = math.atan2(math.sin(th - self._locked_th),
                         math.cos(th - self._locked_th))
        if abs(err) < self._pos_thr:
            self.is_locked = True       # 锁定: 调用方应把速度置零
            return self._locked_th
        else:
            self._locked_th = th        # 位置变化大, 更新锁定值
            self.is_locked = False
            return th


class MovingAverageAngle:
    """环形缓冲移动平均, 跨批次连续。

    适用于波形图 angle 曲线, 平滑静止段毛刺。
    用 atan2(sin_avg, cos_avg) 计算圆形均值, 避免 359°↔1° 跳变。
    """

    def __init__(self, window=5):
        self._n = max(1, int(window))
        self._buf = np.zeros(self._n, dtype=np.float32)
        self._idx = 0
        self._filled = 0

    def reset(self):
        """清空缓冲区状态。"""
        self._idx = 0
        self._filled = 0

    def update_batch(self, th_arr: np.ndarray) -> np.ndarray:
        """批量更新, 返回与输入等长的滤波后数组。跨批次状态连续。"""
        out = np.empty_like(th_arr)
        n = self._n
        for i, th in enumerate(th_arr):
            self._buf[self._idx] = th
            self._idx = (self._idx + 1) % n
            if self._filled < n:
                self._filled += 1
            # 圆形均值: atan2(mean(sin), mean(cos))
            cnt = self._filled
            sin_sum = float(np.sin(self._buf[:cnt]).sum())
            cos_sum = float(np.cos(self._buf[:cnt]).sum())
            out[i] = math.atan2(sin_sum / cnt, cos_sum / cnt)
        return out


class PrecisionRounder:
    """精度截断器。

    四舍五入到指定小数位, 等效于显示死区。
    适用于数值卡片, 简单可靠。
    """

    def __init__(self, decimals=1):
        self._dec = int(decimals)

    def round(self, value: float) -> float:
        return round(float(value), self._dec)


class MovingAverage:
    """环形缓冲移动平均(线性, 非圆形), 跨批次连续。

    适用于连续量(非角度)的曲线平滑, 如 vel/pos/iq 等。
    """

    def __init__(self, window=8):
        self._n = max(1, int(window))
        self._buf = np.zeros(self._n, dtype=np.float32)
        self._idx = 0
        self._filled = 0

    def reset(self):
        self._buf[:] = 0.0
        self._idx = 0
        self._filled = 0

    def update_batch(self, arr: np.ndarray) -> np.ndarray:
        """批量更新, 返回与输入等长滤波后数组。跨批次状态连续。"""
        out = np.empty_like(arr, dtype=np.float32)
        n = self._n
        for i, v in enumerate(arr):
            self._buf[self._idx] = v
            self._idx = (self._idx + 1) % n
            if self._filled < n:
                self._filled += 1
            out[i] = float(self._buf[:self._filled].sum()) / self._filled
        return out
