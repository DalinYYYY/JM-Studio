"""电机可视化面板: 关节电机数字孪生大屏风格。

布局 (单关节电机视图):
┌────────────────────────────────────────────────────────────┐
│  顶部状态条  状态: RUN  模式: VELOCITY  位置: 1.234 rad     │
├──────────┬──────────────────────────────┬──────────────────┤
│ 运行监测  │      关节电机 3D 视图        │  数据预警 (上)    │
│ (KPI)    │      (电机+减速箱+输出轴)    │  功率曲线 (下)    │
│          │      + 悬浮指标标签          │                  │
├──────────┴──────────────────────────────┴──────────────────┤
│  底部数据表: 电机端/输出端 位置|速度|力矩                    │
└────────────────────────────────────────────────────────────┘

3D 模型用 QPainter 伪 3D (圆柱体透视 + 旋转转子), 不依赖 OpenGL。
KPI 直接取自遥测字段 (电机端/输出端 位置/速度/力矩), 零引擎改动。
归一化阈值从 engine.mp 实时读取, 切换场景/导入参数后自动跟随。
"""
import math
import time
from collections import deque

from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen,
)
from PyQt6.QtWidgets import QWidget

from ui.theme import theme


# ==================== 状态配色 ====================
# (主色, 辉光色, 扫描频率 Hz, 是否闪烁)
_STATE_STYLE = {
    "IDLE":   ("#3a4a5a", "#4a5a6a", 0.0, False),
    "READY":  ("#00d4ff", "#00d4ff", 2.0, False),
    "RUN":    ("#00d4ff", "#00ffff", 6.0, False),
    "FAULT":  ("#ff3030", "#ff6060", 0.0, True),   # 1Hz 闪烁
    "SAFETY": ("#ff6b35", "#ff8b55", 0.0, True),   # 0.5Hz 闪烁
    "CALIB":  ("#aaff00", "#ccff33", 4.0, False),
}

# 三相电流色: 正=青蓝, 负=橙红
_CUR_POS = "#00d4ff"
_CUR_NEG = "#ff6b35"

# 能量流色
_FLOW_POS = "#00d4ff"   # Power > 0: 母线 → 电机
_FLOW_NEG = "#ff6b35"   # Power < 0: 电机 → 母线

# 归一化回退默认值 (仅在 engine 未注入时使用, 与 twin_config.MotorBase 默认一致)
_FALLBACK_PEAK_CURRENT = 15.0
_FALLBACK_MAX_SPEED = 300.0
_FALLBACK_V_RATED = 48.0
_FALLBACK_V_OVER = 58.0
_FALLBACK_V_UNDER = 15.0

# 故障位 → 告警描述映射 (与 twin_fsm.Fault 对齐, 不直接 import 避免循环依赖)
_FAULT_BITS = [
    (1 << 0,  "严重", "过电流保护触发"),
    (1 << 1,  "严重", "过电压保护触发"),
    (1 << 2,  "严重", "欠电压保护触发"),
    (1 << 3,  "警告", "FET 过温保护触发"),
    (1 << 4,  "警告", "电机过温保护触发"),
    (1 << 5,  "警告", "软件位置超限"),
    (1 << 6,  "警告", "跟随误差过大"),
    (1 << 7,  "严重", "通信丢失"),
    (1 << 10, "严重", "超速保护触发"),
    (1 << 15, "提示", "手动注入故障"),
]

# 底部数据表行: (标签, key, 单位, 格式)
_DATA_ROWS = [
    ("电机端位置", "pos",         "rad",   "{:+.4f}"),
    ("输出端位置", "pos_out",     "rad",   "{:+.4f}"),
    ("电机端速度", "vel",         "rad/s", "{:+.4f}"),
    ("输出端速度", "vel_out",     "rad/s", "{:+.4f}"),
    ("电机端力矩", "torque",      "Nm",    "{:+.4f}"),
    ("输出端力矩", "torque_out",  "Nm",    "{:+.4f}"),
    ("Iq 电流",    "iq",          "A",     "{:+.4f}"),
    ("电机温度",   "temp_motor",  "℃",     "{:.1f}"),
]


class MotorViewPanel(QWidget):
    """关节电机数字孪生大屏可视化面板。"""

    # 粒子数量 (能量流通道)
    N_PARTICLES = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(900, 600)
        self.setAutoFillBackground(False)

        # 数据缓存
        self._data = {}
        self._engine = None
        self._prev_vel = 0.0
        self._prev_t = 0.0
        self._acc = 0.0
        self._dirty = False

        # 动画相位
        self._particle_phase = 0.0
        self._scan_phase = 0.0
        self._rotor_angle = 0.0   # 转子旋转角度 (跟随 vel)
        self._anim_t0 = time.monotonic()

        # 预生成辉光 QPen 缓存: (color, width) -> [QPen 外层淡, QPen 内层亮]
        self._glow_pens = {}

        # 功率历史 (t_rel, power) 用于右侧曲线, 限 240 点
        self._power_hist = deque(maxlen=240)
        self._t0 = None

        # 告警列表: [(t_rel, level, msg)], 限 20 条
        self._alarms = deque(maxlen=20)
        self._last_fault = 0

        # 重绘定时器: 30Hz
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    # ==================== 公共 API ====================
    def set_engine(self, engine):
        """注入引擎句柄, 用于读 cmd_target_pos / 位置限位 / 归一化参数。"""
        self._engine = engine

    def feed(self, t: dict):
        """接收一帧遥测, 更新缓存 + 置 dirty。"""
        vel = float(t.get("vel", 0.0))
        ts = float(t.get("t_sim", 0.0))
        # 加速度差分 (dt 取自 t_sim)
        dt = ts - self._prev_t
        if dt > 1e-6:
            self._acc = (vel - self._prev_vel) / dt
        self._prev_vel = vel
        self._prev_t = ts
        self._data = t
        # 功率历史
        if self._t0 is None:
            self._t0 = ts
        vbus = float(t.get("vbus", 0.0) or 0.0)
        ibus = float(t.get("ibus", 0.0) or 0.0)
        power = float(t.get("power", 0.0) or (vbus * ibus))
        self._power_hist.append((ts - self._t0, power))
        # 告警上升沿检测
        self._update_alarms(ts - self._t0, int(t.get("fault_flags", 0) or 0))
        self._dirty = True

    def apply_theme(self):
        """主题切换: 清除辉光缓存, 下次重绘重建。"""
        self._glow_pens.clear()
        self.update()

    # ==================== 内部: 引擎参数读取 ====================
    def _engine_mp(self):
        """安全获取 engine.mp, 失败返回 None。"""
        if self._engine is None:
            return None
        return getattr(self._engine, "mp", None)

    def _peak_current(self) -> float:
        """峰值电流 (A), 用于三相电流/母线电流归一化。"""
        mp = self._engine_mp()
        if mp is not None:
            try:
                v = float(getattr(mp.motor_base, "peak_current", _FALLBACK_PEAK_CURRENT))
                if v > 0:
                    return v
            except (AttributeError, TypeError, ValueError):
                pass
        return _FALLBACK_PEAK_CURRENT

    def _max_speed(self) -> float:
        """最大速度 (rad/s, 电机端), 用于速度归一化。"""
        mp = self._engine_mp()
        if mp is not None:
            try:
                v = float(getattr(mp.motor_base, "max_speed", _FALLBACK_MAX_SPEED))
                if v > 0:
                    return v
            except (AttributeError, TypeError, ValueError):
                pass
        return _FALLBACK_MAX_SPEED

    def _vbus_thresholds(self):
        """返回 (v_under, v_over, v_rated) 用于母线电压仪表配色与归一化。"""
        mp = self._engine_mp()
        if mp is not None:
            try:
                pp = mp.protection_param
                v_under = float(getattr(pp, "protect_under_voltage", _FALLBACK_V_UNDER))
                v_over = float(getattr(pp, "protect_over_voltage", _FALLBACK_V_OVER))
                v_rated = float(getattr(mp.motor_base, "rated_voltage", _FALLBACK_V_RATED))
                if v_over > v_under and v_rated > 0:
                    return v_under, v_over, v_rated
            except (AttributeError, TypeError, ValueError):
                pass
        return _FALLBACK_V_UNDER, _FALLBACK_V_OVER, _FALLBACK_V_RATED

    def _power_scale(self) -> float:
        """功率归一化分母 (W), 取 rated_voltage * peak_current。"""
        mp = self._engine_mp()
        if mp is not None:
            try:
                v_rated = float(getattr(mp.motor_base, "rated_voltage", _FALLBACK_V_RATED))
                i_peak = float(getattr(mp.motor_base, "peak_current", _FALLBACK_PEAK_CURRENT))
                if v_rated > 0 and i_peak > 0:
                    return v_rated * i_peak
            except (AttributeError, TypeError, ValueError):
                pass
        return _FALLBACK_V_RATED * _FALLBACK_PEAK_CURRENT

    # ==================== 内部: 告警生成 ====================
    def _update_alarms(self, t_rel: float, fault_flags: int):
        """故障上升沿 → 新增告警条目。"""
        new_bits = fault_flags & (~self._last_fault)
        if new_bits:
            for bit, level, msg in _FAULT_BITS:
                if new_bits & bit:
                    self._alarms.append((t_rel, level, msg))
        # 故障清除 → 提示
        cleared = self._last_fault & (~fault_flags)
        if cleared and self._last_fault != 0:
            self._alarms.append((t_rel, "提示", "故障已清除"))
        self._last_fault = fault_flags

    # ==================== 内部: 动画 tick ====================
    def _tick(self):
        """30Hz 定时器: 仅在可见 + dirty 时推进动画 + 触发重绘。"""
        if not self.isVisible():
            return
        now = time.monotonic()
        elapsed = now - self._anim_t0
        self._anim_t0 = now

        state = self._data.get("sys_state_name", "IDLE")
        _, _, scan_hz, blink = _STATE_STYLE.get(state, _STATE_STYLE["IDLE"])

        # 扫描弧相位
        if scan_hz > 0:
            self._scan_phase += scan_hz * elapsed
            self._dirty = True

        # 粒子相位 (跟随 Power)
        power = float(self._data.get("power", 0.0) or
                      (float(self._data.get("vbus", 0.0)) *
                       float(self._data.get("ibus", 0.0))))
        p_scale = max(self._power_scale(), 1.0)
        speed = max(-1.0, min(1.0, power / p_scale))
        self._particle_phase += speed * elapsed * 0.5
        if abs(speed) > 0.01:
            self._dirty = True

        # 转子旋转角度 (跟随 vel)
        vel = float(self._data.get("vel", 0.0) or 0.0)
        self._rotor_angle += vel * elapsed
        if abs(vel) > 0.01:
            self._dirty = True

        # FAULT/SAFETY 闪烁
        if blink:
            self._dirty = True

        if self._dirty:
            self.update()

    # ==================== 绘制入口 ====================
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 深色背景
        p.fillRect(self.rect(), QColor("#0a0e1a"))
        self._paint_grid(p)

        W = self.width()
        H = self.height()

        # 布局分区
        top_h = 36
        left_w = 260
        right_w = 320
        bottom_h = 120
        margin = 10

        # 1. 顶部状态条
        self._paint_top_status(p, QRectF(0, 0, W, top_h))

        # 2. 左侧 KPI 面板
        self._paint_left_kpi(p, QRectF(margin, top_h + margin,
                                        left_w - margin, H - top_h - bottom_h - 2 * margin))

        # 3. 中间 3D 模型
        model_rect = QRectF(left_w + margin, top_h + margin,
                            W - left_w - right_w - 2 * margin,
                            H - top_h - bottom_h - 2 * margin)
        self._paint_motor_3d(p, model_rect)

        # 4. 右侧面板 (告警 + 功率曲线)
        self._paint_right_panel(p, QRectF(W - right_w + margin, top_h + margin,
                                           right_w - 2 * margin,
                                           H - top_h - bottom_h - 2 * margin))

        # 5. 底部数据表
        self._paint_bottom_table(p, QRectF(margin, H - bottom_h,
                                            W - 2 * margin, bottom_h - margin))

        self._dirty = False

    # ==================== 顶部状态条 ====================
    def _paint_top_status(self, p: QPainter, rect: QRectF):
        """绘制顶部状态条: 状态/模式/位置/速度/故障。"""
        p.fillRect(rect, QColor("#0f1419"))
        p.setPen(QPen(QColor("#1a2535"), 1.0))
        p.drawLine(rect.bottomLeft(), rect.bottomRight())

        state = self._data.get("sys_state_name", "-")
        mode = self._data.get("control_mode_name", "-")
        pos = float(self._data.get("pos", 0.0) or 0.0)
        vel = float(self._data.get("vel", 0.0) or 0.0)
        fault = int(self._data.get("fault_flags", 0) or 0)

        main_color = _STATE_STYLE.get(state, _STATE_STYLE["IDLE"])[0]
        font = QFont("Microsoft YaHei", 10, QFont.Weight.Bold)
        p.setFont(font)

        # 状态指示灯
        p.setBrush(QBrush(QColor(main_color)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(rect.x() + 16, rect.center().y()), 5, 5)

        # 文本项: (标签, 值, 颜色)
        items = [
            ("状态",   state,                 main_color),
            ("模式",   mode,                  "#7FD4FF"),
            ("位置",   f"{pos:+.4f} rad",     "#9FB6DD"),
            ("速度",   f"{vel:+.4f} rad/s",   "#9FB6DD"),
            ("故障",   f"0x{fault:04X}" if fault else "无", "#ff3030" if fault else "#5a6a7a"),
        ]
        x = rect.x() + 32
        for label, value, color in items:
            # 标签
            p.setPen(QColor("#5a6a7a"))
            p.setFont(QFont("Microsoft YaHei", 8))
            p.drawText(QRectF(x, rect.y() + 4, 40, 14),
                       Qt.AlignmentFlag.AlignLeft, label)
            # 值
            p.setPen(QColor(color))
            p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            p.drawText(QRectF(x, rect.y() + 16, 120, 16),
                       Qt.AlignmentFlag.AlignLeft, value)
            x += 150

        # 右侧标题
        p.setPen(QColor("#7FD4FF"))
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        p.drawText(QRectF(rect.right() - 260, rect.y(), 250, rect.height()),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   "▎关节电机数字孪生")

    # ==================== 左侧 KPI 面板 ====================
    def _paint_left_kpi(self, p: QPainter, rect: QRectF):
        """绘制左侧运行监测 KPI 列表。"""
        # 面板背景
        p.setBrush(QBrush(QColor("#0f1419")))
        p.setPen(QPen(QColor("#1a2535"), 1.0))
        p.drawRoundedRect(rect, 4, 4)

        # 标题
        p.setPen(QColor("#7FD4FF"))
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        p.drawText(QRectF(rect.x() + 10, rect.y() + 6, rect.width() - 20, 22),
                   Qt.AlignmentFlag.AlignLeft, "▎运行监测")

        d = self._data
        vbus = float(d.get("vbus", 0.0) or 0.0)
        ibus = float(d.get("ibus", 0.0) or 0.0)
        power = float(d.get("power", 0.0) or (vbus * ibus))
        state = d.get("sys_state_name", "IDLE")
        main_color = _STATE_STYLE.get(state, _STATE_STYLE["IDLE"])[0]

        # KPI 行: (标签, 数值, 单位, 颜色)
        rows = [
            ("电机端速度", f"{float(d.get('vel', 0) or 0):+.3f}",   "rad/s", main_color),
            ("输出端速度", f"{float(d.get('vel_out', 0) or 0):+.3f}", "rad/s", "#00d4ff"),
            ("电机端力矩", f"{float(d.get('torque', 0) or 0):+.3f}",  "Nm",    _CUR_POS if float(d.get('torque', 0) or 0) >= 0 else _CUR_NEG),
            ("输出端力矩", f"{float(d.get('torque_out', 0) or 0):+.3f}", "Nm",  _CUR_POS if float(d.get('torque_out', 0) or 0) >= 0 else _CUR_NEG),
            ("电机端位置", f"{float(d.get('pos', 0) or 0):+.4f}",     "rad",   "#aaccff"),
            ("输出端位置", f"{float(d.get('pos_out', 0) or 0):+.4f}", "rad",   "#aaccff"),
            ("Iq 电流",    f"{float(d.get('iq', 0) or 0):+.3f}",      "A",     _CUR_POS if float(d.get('iq', 0) or 0) >= 0 else _CUR_NEG),
            ("母线电压",   f"{vbus:.2f}",                              "V",     "#00d4ff"),
            ("母线电流",   f"{ibus:+.3f}",                             "A",     _CUR_POS if ibus >= 0 else _CUR_NEG),
            ("功率",       f"{power/1000:+.3f}",                       "kW",    _FLOW_POS if power >= 0 else _FLOW_NEG),
            ("加速度",     f"{self._acc:+.0f}",                        "rad/s²", "#9FB6DD"),
            ("电机温度",   f"{float(d.get('temp_motor', 0) or 0):.1f}", "℃",    "#ffaa44"),
        ]

        row_h = 30
        y0 = rect.y() + 32
        for i, (label, value, unit, color) in enumerate(rows):
            y = y0 + i * row_h
            if y + row_h > rect.bottom() - 4:
                break
            # 分隔线
            if i > 0:
                p.setPen(QPen(QColor("#161d2a"), 1.0))
                p.drawLine(QPointF(rect.x() + 10, y), QPointF(rect.right() - 10, y))
            # 标签
            p.setPen(QColor("#7a8a9a"))
            p.setFont(QFont("Microsoft YaHei", 9))
            p.drawText(QRectF(rect.x() + 14, y + 4, 80, 14),
                       Qt.AlignmentFlag.AlignLeft, label)
            # 数值
            p.setPen(QColor(color))
            p.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
            p.drawText(QRectF(rect.x() + 14, y + 14, rect.width() - 60, 16),
                       Qt.AlignmentFlag.AlignLeft, value)
            # 单位
            p.setPen(QColor("#5a6a7a"))
            p.setFont(QFont("Microsoft YaHei", 8))
            p.drawText(QRectF(rect.right() - 56, y + 14, 46, 16),
                       Qt.AlignmentFlag.AlignRight, unit)

    # ==================== 中间 3D 关节电机模型 ====================
    def _paint_motor_3d(self, p: QPainter, rect: QRectF):
        """绘制 3D 透视关节电机模型 (电机本体 + 减速箱 + 输出轴) + 悬浮标签。"""
        state = self._data.get("sys_state_name", "IDLE")
        main_color, glow_color, _, blink = _STATE_STYLE.get(
            state, _STATE_STYLE["IDLE"])

        # FAULT/SAFETY 闪烁半周期变暗
        dim_main = main_color
        if blink:
            phase = (time.monotonic() * (1.0 if state == "FAULT" else 0.5)) % 1.0
            if phase > 0.5:
                dim_main = "#3a1a1a" if state == "FAULT" else "#3a2a1a"

        # 标题
        p.setPen(QColor("#7FD4FF"))
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        p.drawText(QRectF(rect.x() + 10, rect.y() + 4, rect.width() - 20, 22),
                   Qt.AlignmentFlag.AlignLeft, "▎关节电机 3D 视图")

        # 模型区域 (去掉标题)
        model_rect = QRectF(rect.x(), rect.y() + 28,
                             rect.width(), rect.height() - 28)

        # 电机水平横置: 编码器(左) + 电机本体(中) + 减速箱(右) + 输出轴(最右)
        cy = model_rect.center().y()
        total_w = model_rect.width() - 80
        x0 = model_rect.x() + 40

        # 段长分配: 编码器 12%, 电机本体 50%, 减速箱 30%, 输出轴 8%
        enc_w = total_w * 0.12
        motor_w = total_w * 0.50
        gear_w = total_w * 0.30
        shaft_w = total_w * 0.08
        enc_x = x0
        motor_x = enc_x + enc_w
        gear_x = motor_x + motor_w
        shaft_x = gear_x + gear_w

        # 圆柱半径
        motor_r = min(60, model_rect.height() * 0.28)
        gear_r = motor_r * 1.15   # 减速箱略粗
        enc_r = motor_r * 0.45    # 编码器较细
        shaft_r = motor_r * 0.20  # 输出轴最细

        # 底座 (工字钢)
        base_y = cy + gear_r + 8
        base_h = 10
        p.setBrush(QBrush(QColor("#2a3040")))
        p.setPen(QPen(QColor("#3a4a5a"), 1.0))
        p.drawRoundedRect(QRectF(enc_x - 6, base_y, total_w + 12, base_h), 2, 2)

        # 1. 编码器段 (尾部小圆柱)
        self._draw_cylinder(p, enc_x, cy, enc_w, enc_r, "#4a5060", "#2a3040")
        # 编码器端盖纹理
        p.setPen(QPen(QColor("#6a7080"), 1.0))
        for i in range(3):
            ix = enc_x + (i + 1) * enc_w / 4
            p.drawLine(QPointF(ix, cy - enc_r * 0.7), QPointF(ix, cy + enc_r * 0.7))

        # 2. 电机本体 (粗圆柱 + 散热鳍片 + 定子绕组剖面)
        self._draw_cylinder(p, motor_x, cy, motor_w, motor_r, "#5a6070", "#2a3040")
        self._draw_motor_stator(p, motor_x, cy, motor_w, motor_r, dim_main)
        self._draw_motor_rotor(p, motor_x, cy, motor_w, motor_r, self._rotor_angle, glow_color)

        # 3. 减速箱段 (略粗圆柱, 行星齿轮纹理)
        self._draw_cylinder(p, gear_x, cy, gear_w, gear_r, "#3a4050", "#1a2030")
        self._draw_gearbox_detail(p, gear_x, cy, gear_w, gear_r, self._rotor_angle, dim_main)

        # 4. 输出轴 (细圆柱伸出)
        self._draw_cylinder(p, shaft_x, cy, shaft_w, shaft_r, "#8a8a90", "#5a5a60")
        # 输出轴端面 (旋转标志)
        p.setBrush(QBrush(QColor(dim_main)))
        p.setPen(QPen(QColor(dim_main).darker(120), 1.0))
        p.drawEllipse(QPointF(shaft_x + shaft_w, cy), 4, 4)

        # 5. 轴承座 (电机与减速箱之间)
        p.setBrush(QBrush(QColor("#1a1a22")))
        p.setPen(QPen(QColor("#3a3a44"), 1.0))
        p.drawRoundedRect(QRectF(gear_x - 6, cy - gear_r - 4, 12, (gear_r + 4) * 2), 2, 2)

        # 6. 能量流粒子 (母线侧 → 电机本体)
        self._paint_energy_flow(p,
                                model_rect.x() + 20, cy - motor_r - 30,
                                motor_x + motor_w * 0.5, cy - motor_r)

        # 7. 悬浮指标标签
        vel = float(self._data.get("vel", 0.0) or 0.0)
        torque = float(self._data.get("torque", 0.0) or 0.0)
        temp_motor = float(self._data.get("temp_motor", 35.0) or 35.0)
        labels = [
            (motor_x + motor_w * 0.3, cy - motor_r - 26, f"{vel:+.2f} rad/s", main_color),
            (motor_x + motor_w * 0.7, cy - motor_r - 26, f"{torque:+.3f} Nm", _CUR_POS if torque >= 0 else _CUR_NEG),
            (gear_x + gear_w * 0.5, cy + gear_r + 14, f"减速比 {self._gear_ratio():.0f}:1", "#7FD4FF"),
            (motor_x + motor_w * 0.5, cy + motor_r + 14, f"温度 {temp_motor:.1f} ℃", "#ffaa44"),
        ]
        for lx, ly, text, color in labels:
            self._draw_float_label(p, lx, ly, text, color)

    def _gear_ratio(self) -> float:
        """减速比。"""
        mp = self._engine_mp()
        if mp is not None:
            try:
                return float(getattr(mp.gearbox_param, "gear_ratio", 100.0))
            except (AttributeError, TypeError, ValueError):
                pass
        return 100.0

    def _draw_cylinder(self, p: QPainter, x: float, cy: float,
                        w: float, r: float, light: str, dark: str):
        """绘制水平圆柱体 (伪 3D: 矩形 + 渐变 + 两端椭圆)。"""
        # 主体渐变 (上亮下暗, 模拟圆柱反光)
        grad = QLinearGradient(0, cy - r, 0, cy + r)
        grad.setColorAt(0, QColor(light))
        grad.setColorAt(0.5, QColor(light).lighter(110))
        grad.setColorAt(1, QColor(dark))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor(dark).darker(120), 1.0))
        p.drawRect(QRectF(x, cy - r, w, 2 * r))
        # 左端面椭圆 (暗)
        p.setBrush(QBrush(QColor(dark).darker(110)))
        p.drawEllipse(QPointF(x, cy), r * 0.25, r)
        # 右端面椭圆 (亮)
        p.setBrush(QBrush(QColor(light).darker(110)))
        p.drawEllipse(QPointF(x + w, cy), r * 0.25, r)

    def _draw_motor_stator(self, p: QPainter, x: float, cy: float,
                            w: float, r: float, color: str):
        """绘制电机定子 (散热鳍片 + 绕组剖面)。"""
        p.save()
        p.setClipRect(QRectF(x, cy - r, w, 2 * r))
        # 散热鳍片 (顶部/底部水平细线)
        p.setPen(QPen(QColor(color).darker(160), 1.0))
        for off in (-r * 0.75, -r * 0.55, -r * 0.35, r * 0.35, r * 0.55, r * 0.75):
            p.drawLine(QPointF(x + 4, cy + off), QPointF(x + w - 4, cy + off))
        # 定子绕组剖面 (内部红/黄线圈, 6 槽)
        slot_r = r * 0.16
        for i in range(6):
            ang = i * 60.0 + 30.0
            rad = math.radians(ang - 90)
            sx = x + w * 0.5 + (r * 0.6) * math.cos(rad) * 0.3  # 压扁, 透视
            sy = cy + (r * 0.78) * math.sin(rad)
            # 交替红/黄
            c = "#ff6b35" if i % 2 == 0 else "#ffdd44"
            p.setBrush(QBrush(QColor(c).darker(140)))
            p.setPen(QPen(QColor(c), 1.0))
            p.drawEllipse(QPointF(sx, sy), slot_r, slot_r)
        p.restore()

    def _draw_motor_rotor(self, p: QPainter, x: float, cy: float,
                           w: float, r: float, angle: float, glow: str):
        """绘制电机内部旋转转子 (中心轴 + 磁极)。"""
        p.save()
        p.setClipRect(QRectF(x, cy - r, w, 2 * r))
        # 中心轴 (水平线)
        p.setPen(QPen(QColor(glow).darker(120), 2.0))
        p.drawLine(QPointF(x, cy), QPointF(x + w, cy))
        # 旋转磁极 (4 极, 跟随 angle)
        inner_r = r * 0.45
        cx = x + w * 0.5
        for i in range(4):
            a = angle + i * math.pi / 2
            px = cx + inner_r * math.cos(a) * 0.3
            py = cy + inner_r * math.sin(a)
            c = QColor("#00d4ff") if i % 2 == 0 else QColor("#ff6b35")
            c.setAlpha(160)
            p.setBrush(QBrush(c))
            p.setPen(QPen(c.darker(120), 1.0))
            p.drawEllipse(QPointF(px, py), 5, 5)
        p.restore()

    def _draw_gearbox_detail(self, p: QPainter, x: float, cy: float,
                              w: float, r: float, angle: float, color: str):
        """绘制减速箱内部 (行星齿轮纹理, 跟随转子旋转)。"""
        p.save()
        p.setClipRect(QRectF(x, cy - r, w, 2 * r))
        # 中心太阳轮
        cx = x + w * 0.5
        sun_r = r * 0.25
        c = QColor(color); c.setAlpha(120)
        p.setBrush(QBrush(c))
        p.setPen(QPen(QColor(color).darker(140), 1.0))
        p.drawEllipse(QPointF(cx, cy), sun_r, sun_r)
        # 3 个行星齿轮 (120° 分布, 反向旋转)
        planet_r = r * 0.20
        planet_orbit = r * 0.55
        for i in range(3):
            a = -angle / 3 + i * 2 * math.pi / 3   # 减速比体现: 反向 + 减速
            px = cx + planet_orbit * math.cos(a) * 0.3
            py = cy + planet_orbit * math.sin(a)
            p.setBrush(QBrush(QColor(color).darker(130)))
            p.setPen(QPen(QColor(color).darker(110), 1.0))
            p.drawEllipse(QPointF(px, py), planet_r, planet_r)
            # 齿轮中心点
            p.setBrush(QBrush(QColor(color)))
            p.drawEllipse(QPointF(px, py), 2, 2)
        # 齿圈 (外环)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(color).darker(160), 1.5))
        p.drawEllipse(QPointF(cx, cy), r * 0.78, r * 0.78)
        p.restore()

    def _draw_float_label(self, p: QPainter, x: float, y: float,
                           text: str, color: str):
        """绘制带背景的悬浮标签。"""
        p.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        pad = 4
        rect = QRectF(x - tw / 2 - pad, y, tw + 2 * pad, th + 2)
        # 背景
        p.setBrush(QBrush(QColor(10, 14, 26, 200)))
        p.setPen(QPen(QColor(color), 1.0))
        p.drawRoundedRect(rect, 3, 3)
        # 文字
        p.setPen(QColor(color))
        p.drawText(QRectF(x - tw / 2, y + 1, tw, th),
                   Qt.AlignmentFlag.AlignCenter, text)
        # 引线 (向下到模型)
        p.setPen(QPen(QColor(color).darker(150), 1.0))
        p.drawLine(QPointF(x, rect.bottom()), QPointF(x, rect.bottom() + 6))

    # ==================== 能量流粒子 ====================
    def _paint_energy_flow(self, p: QPainter,
                            x1: float, y1: float, x2: float, y2: float):
        """绘制能量流粒子通道 (母线 → 电机)。

        path 起点 (x1,y1) = 母线侧, 终点 (x2,y2) = 电机侧。
        Power > 0 (母线→电机): 粒子沿 path 正向流动。
        Power < 0 (电机→母线, 回馈): 粒子反向流动。
        """
        power = float(self._data.get("power", 0.0) or
                      (float(self._data.get("vbus", 0.0)) *
                       float(self._data.get("ibus", 0.0))))
        # 通道底色 (贝塞尔曲线)
        path = QPainterPath()
        path.moveTo(x1, y1)
        path.cubicTo(x1 + (x2 - x1) * 0.33, y1 - 20,
                     x1 + (x2 - x1) * 0.66, y2 - 20,
                     x2, y2)
        p.setPen(QPen(QColor("#1a2030"), 1.5))
        p.drawPath(path)

        # 粒子
        color = _FLOW_POS if power >= 0 else _FLOW_NEG
        n = self.N_PARTICLES
        for i in range(n):
            t = (self._particle_phase + i / n) % 1.0
            if abs(power) < 1.0:
                t = 0.5 + (i / n - 0.5) * 0.3
            pt = path.pointAtPercent(t)
            for j in range(2, 0, -1):
                c = QColor(color); c.setAlpha(60 // j)
                p.setPen(QPen(c, 1 + j))
                p.setBrush(QBrush(c))
                p.drawEllipse(pt, 2 + j, 2 + j)
            p.setPen(QPen(QColor(color), 1.0))
            p.setBrush(QBrush(QColor(color)))
            p.drawEllipse(pt, 2, 2)

    # ==================== 右侧面板 (告警 + 功率曲线) ====================
    def _paint_right_panel(self, p: QPainter, rect: QRectF):
        """绘制右侧: 上半告警列表 + 下半功率曲线。"""
        # 面板背景
        p.setBrush(QBrush(QColor("#0f1419")))
        p.setPen(QPen(QColor("#1a2535"), 1.0))
        p.drawRoundedRect(rect, 4, 4)

        half_h = rect.height() / 2
        # 上半: 数据预警
        alarm_rect = QRectF(rect.x() + 4, rect.y() + 4,
                             rect.width() - 8, half_h - 8)
        self._paint_alarm_list(p, alarm_rect)
        # 下半: 功率曲线
        curve_rect = QRectF(rect.x() + 4, rect.y() + half_h + 4,
                             rect.width() - 8, half_h - 8)
        self._paint_power_curve(p, curve_rect)

    def _paint_alarm_list(self, p: QPainter, rect: QRectF):
        """绘制数据预警列表。"""
        p.setPen(QColor("#7FD4FF"))
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        p.drawText(QRectF(rect.x(), rect.y(), rect.width(), 22),
                   Qt.AlignmentFlag.AlignLeft, "▎数据预警")

        # 告警条目 (最多 8 条, 倒序)
        items = list(self._alarms)[-8:]
        if not items:
            p.setPen(QColor("#5a6a7a"))
            p.setFont(QFont("Microsoft YaHei", 9))
            p.drawText(QRectF(rect.x(), rect.y() + 30, rect.width(), 20),
                       Qt.AlignmentFlag.AlignCenter, "无告警")
            return

        level_color = {"严重": "#ff3030", "警告": "#ff6b35", "提示": "#00d4ff"}
        row_h = 22
        y0 = rect.y() + 26
        for i, (t_rel, level, msg) in enumerate(items):
            y = y0 + i * row_h
            if y + row_h > rect.bottom():
                break
            # 等级色条
            c = level_color.get(level, "#5a6a7a")
            p.setBrush(QBrush(QColor(c)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(rect.x(), y + 2, 3, row_h - 4), 1, 1)
            # 时间
            p.setPen(QColor("#5a6a7a"))
            p.setFont(QFont("Consolas", 8))
            p.drawText(QRectF(rect.x() + 8, y, 50, row_h),
                       Qt.AlignmentFlag.AlignVCenter, f"{t_rel:.1f}s")
            # 等级
            p.setPen(QColor(c))
            p.setFont(QFont("Microsoft YaHei", 8, QFont.Weight.Bold))
            p.drawText(QRectF(rect.x() + 58, y, 36, row_h),
                       Qt.AlignmentFlag.AlignVCenter, level)
            # 描述
            p.setPen(QColor("#9FB6DD"))
            p.setFont(QFont("Microsoft YaHei", 9))
            p.drawText(QRectF(rect.x() + 96, y, rect.width() - 100, row_h),
                       Qt.AlignmentFlag.AlignVCenter, msg)

    def _paint_power_curve(self, p: QPainter, rect: QRectF):
        """绘制实时功率曲线 (QPainter 自绘, 不依赖 pyqtgraph)。"""
        p.setPen(QColor("#7FD4FF"))
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        p.drawText(QRectF(rect.x(), rect.y(), rect.width(), 22),
                   Qt.AlignmentFlag.AlignLeft, "▎功率趋势")

        # 绘图区
        plot_rect = QRectF(rect.x() + 8, rect.y() + 28,
                            rect.width() - 16, rect.height() - 40)
        # 背景
        p.setBrush(QBrush(QColor("#080c14")))
        p.setPen(QPen(QColor("#1a2535"), 1.0))
        p.drawRect(plot_rect)
        # 网格
        p.setPen(QPen(QColor(255, 255, 255, 15), 1.0))
        for i in range(1, 4):
            gx = plot_rect.x() + plot_rect.width() * i / 4
            p.drawLine(QPointF(gx, plot_rect.y()), QPointF(gx, plot_rect.bottom()))
            gy = plot_rect.y() + plot_rect.height() * i / 4
            p.drawLine(QPointF(plot_rect.x(), gy), QPointF(plot_rect.right(), gy))
        # 零线
        mid_y = plot_rect.y() + plot_rect.height() / 2
        p.setPen(QPen(QColor("#3a4a5a"), 1.0))
        p.drawLine(QPointF(plot_rect.x(), mid_y), QPointF(plot_rect.right(), mid_y))

        if len(self._power_hist) < 2:
            p.setPen(QColor("#5a6a7a"))
            p.setFont(QFont("Microsoft YaHei", 9))
            p.drawText(plot_rect, Qt.AlignmentFlag.AlignCenter, "等待数据...")
            return

        # 数据范围
        p_scale = max(self._power_scale(), 1.0)
        t_min = self._power_hist[0][0]
        t_max = self._power_hist[-1][0]
        t_span = max(t_max - t_min, 0.1)

        # 折线
        pts = []
        for t_rel, power in self._power_hist:
            px = plot_rect.x() + (t_rel - t_min) / t_span * plot_rect.width()
            norm = max(-1.0, min(1.0, power / p_scale))
            py = mid_y - norm * plot_rect.height() / 2
            pts.append(QPointF(px, py))

        # 辉光
        color = _FLOW_POS if self._power_hist[-1][1] >= 0 else _FLOW_NEG
        for w, a in ((4, 40), (2, 80)):
            c = QColor(color); c.setAlpha(a)
            p.setPen(QPen(c, w))
            p.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath()
            path.moveTo(pts[0])
            for pt in pts[1:]:
                path.lineTo(pt)
            p.drawPath(path)
        # 主线
        p.setPen(QPen(QColor(color), 1.5))
        path = QPainterPath()
        path.moveTo(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        p.drawPath(path)

        # 当前值标签
        cur_power = self._power_hist[-1][1]
        p.setPen(QColor(color))
        p.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        p.drawText(QRectF(plot_rect.x(), plot_rect.bottom() + 2,
                           plot_rect.width(), 14),
                   Qt.AlignmentFlag.AlignRight, f"{cur_power/1000:+.3f} kW")

    # ==================== 底部数据表 ====================
    def _paint_bottom_table(self, p: QPainter, rect: QRectF):
        """绘制底部数据表 (8 列关节电机数据)。"""
        # 背景
        p.setBrush(QBrush(QColor("#0f1419")))
        p.setPen(QPen(QColor("#1a2535"), 1.0))
        p.drawRoundedRect(rect, 4, 4)

        # 标题
        p.setPen(QColor("#7FD4FF"))
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        p.drawText(QRectF(rect.x() + 10, rect.y() + 4, rect.width() - 20, 22),
                   Qt.AlignmentFlag.AlignLeft, "▎实时数据明细")

        # 表格区
        table_rect = QRectF(rect.x() + 10, rect.y() + 28,
                             rect.width() - 20, rect.height() - 32)
        col_w = table_rect.width() / len(_DATA_ROWS)
        # 表头
        p.setPen(QColor("#5a6a7a"))
        p.setFont(QFont("Microsoft YaHei", 8))
        for i, (name, _, _, _) in enumerate(_DATA_ROWS):
            p.drawText(QRectF(table_rect.x() + i * col_w, table_rect.y(),
                               col_w, 16),
                       Qt.AlignmentFlag.AlignCenter, name)

        # 数据行
        for i, (name, key, unit, fmt) in enumerate(_DATA_ROWS):
            val = self._data.get(key, 0.0)
            try:
                val = float(val or 0.0)
            except (ValueError, TypeError):
                val = 0.0
            x = table_rect.x() + i * col_w
            y = table_rect.y() + 20

            # 数值颜色: 温度超阈值标橙/红
            color = "#9FB6DD"
            if key == "temp_motor":
                if val >= 85:
                    color = "#ff3030"
                elif val >= 70:
                    color = "#ff6b35"
                else:
                    color = "#ffaa44"
            elif key in ("vel", "vel_out", "torque", "torque_out", "iq"):
                color = _CUR_POS if val >= 0 else _CUR_NEG

            p.setPen(QColor(color))
            p.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
            p.drawText(QRectF(x, y, col_w, 20),
                       Qt.AlignmentFlag.AlignCenter, fmt.format(val))
            # 单位
            p.setPen(QColor("#5a6a7a"))
            p.setFont(QFont("Microsoft YaHei", 7))
            p.drawText(QRectF(x, y + 22, col_w, 12),
                       Qt.AlignmentFlag.AlignCenter, unit)
            # 列分隔线
            if i > 0:
                p.setPen(QPen(QColor("#161d2a"), 1.0))
                p.drawLine(QPointF(x, table_rect.y() + 4),
                           QPointF(x, table_rect.bottom() - 4))

    # ==================== 辅助: 网格底纹 ====================
    def _paint_grid(self, p: QPainter):
        """科幻感网格底纹。"""
        p.setPen(QPen(QColor(255, 255, 255, 8), 1.0))
        step = 40
        for x in range(0, self.width(), step):
            p.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), step):
            p.drawLine(0, y, self.width(), y)

    # ==================== 辅助: 辉光 Pen 缓存 ====================
    def _glow_pen(self, color: str, width: float, glow: int = 3):
        """多层辉光 Pen: 外层粗淡 + 内层细亮。结果缓存。"""
        key = (color, width, glow)
        if key in self._glow_pens:
            return self._glow_pens[key]
        pens = []
        for i in range(glow, 0, -1):
            c = QColor(color)
            c.setAlpha(max(20, 80 // i))
            pens.append(QPen(c, width + i * 2))
        pens.append(QPen(QColor(color), width))
        self._glow_pens[key] = pens
        return pens

    # ==================== 辅助: 读 cmd_target_pos ====================
    def _read_cmd_target_pos(self):
        """从 engine.fsm 读 cmd_target_pos, 失败返回 None。"""
        if self._engine is None:
            return None
        fsm = getattr(self._engine, "fsm", None)
        if fsm is None:
            return None
        val = getattr(fsm, "cmd_target_pos", None)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None
