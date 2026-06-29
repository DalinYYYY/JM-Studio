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
        self._scan_phase = 0.0
        self._rotor_angle = 0.0   # 转子旋转角度 (跟随 vel)
        self._anim_t0 = time.monotonic()

        # 中央视图模式: "face"=端面同轴三环仪表, "side"=侧视玻璃剖面
        self._view_mode = "face"
        self._view_btns = []   # [(QRectF, mode)] 供鼠标命中

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
        # 深蓝背景渐变
        bg = QLinearGradient(0, 0, 0, self.height())
        bg.setColorAt(0, theme.c("twin_bg_top"))
        bg.setColorAt(1, theme.c("twin_bg_bottom"))
        p.fillRect(self.rect(), QBrush(bg))
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

        # 3. 中间模型 (端面仪表 / 侧视剖面 可切换)
        model_rect = QRectF(left_w + margin, top_h + margin,
                            W - left_w - right_w - 2 * margin,
                            H - top_h - bottom_h - 2 * margin)
        if self._view_mode == "side":
            self._paint_motor_3d(p, model_rect)
        else:
            self._paint_motor_face(p, model_rect)
        self._paint_view_toggle(p, model_rect)

        # 4. 右侧面板 (告警 + 功率曲线)
        self._paint_right_panel(p, QRectF(W - right_w + margin, top_h + margin,
                                           right_w - 2 * margin,
                                           H - top_h - bottom_h - 2 * margin))

        # 5. 底部数据表
        self._paint_bottom_table(p, QRectF(margin, H - bottom_h,
                                            W - 2 * margin, bottom_h - margin))

        self._dirty = False

    # ==================== 视图切换 (端面/侧视) ====================
    def _paint_view_toggle(self, p: QPainter, rect: QRectF):
        """在模型区右上角画 端面/侧视 切换按钮, 记录命中矩形。"""
        self._view_btns = []
        labels = [("face", "端面"), ("side", "侧视")]
        bw, bh, gap = 48.0, 22.0, 4.0
        x = rect.right() - (bw * len(labels) + gap * (len(labels) - 1)) - 6
        y = rect.y() + 4
        p.setFont(QFont("Microsoft YaHei", 8, QFont.Weight.Bold))
        for mode, text in labels:
            b = QRectF(x, y, bw, bh)
            active = (self._view_mode == mode)
            p.setBrush(QBrush(theme.c("card_top") if active else theme.c("twin_panel_bg")))
            p.setPen(QPen(theme.c("twin_glass") if active else theme.c("border"),
                          1.4 if active else 1.0))
            p.drawRoundedRect(b, 4, 4)
            p.setPen(theme.c("title") if active else theme.c("muted"))
            p.drawText(b, Qt.AlignmentFlag.AlignCenter, text)
            self._view_btns.append((QRectF(b), mode))
            x += bw + gap

    def mousePressEvent(self, ev):
        """点击视图切换按钮。"""
        pos = ev.position() if hasattr(ev, "position") else QPointF(ev.pos())
        for b, mode in self._view_btns:
            if b.contains(pos):
                if mode != self._view_mode:
                    self._view_mode = mode
                    self._dirty = True
                    self.update()
                return
        super().mousePressEvent(ev)

    # ==================== 顶部状态条 ====================
    def _paint_top_status(self, p: QPainter, rect: QRectF):
        """绘制顶部状态条: 状态/模式/位置/速度/故障。"""
        p.fillRect(rect, theme.c("panel_bg"))
        p.setPen(QPen(theme.c("border"), 1.0))
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
        """绘制左侧运行监测 KPI (卡片瓦片)。"""
        # 面板背景
        p.setBrush(QBrush(theme.c("twin_panel_bg")))
        p.setPen(QPen(theme.c("border"), 1.0))
        p.drawRoundedRect(rect, 6, 6)

        # 标题
        p.setPen(theme.c("title"))
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

        # 卡片瓦片: 单列堆叠, 卡内左上标签 + 大数值 + 右下单位
        card_x = rect.x() + 10
        card_w = rect.width() - 20
        gap = 6
        y0 = rect.y() + 34
        avail = rect.bottom() - 8 - y0
        n = len(rows)
        card_h = max(34.0, min(48.0, (avail - (n - 1) * gap) / n))

        card_top = theme.c("card_top")
        card_bot = theme.c("card_bottom")
        border_c = theme.c("border")
        for i, (label, value, unit, color) in enumerate(rows):
            y = y0 + i * (card_h + gap)
            if y + card_h > rect.bottom() - 4:
                break
            card = QRectF(card_x, y, card_w, card_h)
            # 卡片渐变底
            grad = QLinearGradient(0, y, 0, y + card_h)
            grad.setColorAt(0, card_top)
            grad.setColorAt(1, card_bot)
            p.setBrush(QBrush(grad))
            p.setPen(QPen(border_c, 1.0))
            p.drawRoundedRect(card, 5, 5)
            # 左侧色条
            p.setBrush(QBrush(QColor(color)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(card.x() + 2, card.y() + 4, 3, card_h - 8), 1.5, 1.5)
            # 标签 (左上)
            p.setPen(theme.c("muted"))
            p.setFont(QFont("Microsoft YaHei", 8))
            p.drawText(QRectF(card.x() + 12, card.y() + 4, card_w - 16, 14),
                       Qt.AlignmentFlag.AlignLeft, label)
            # 数值 (左下, 大字)
            p.setPen(QColor(color))
            p.setFont(QFont("Consolas", 13, QFont.Weight.Bold))
            p.drawText(QRectF(card.x() + 12, card.y() + card_h - 22, card_w - 56, 20),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, value)
            # 单位 (右下)
            p.setPen(theme.c("muted"))
            p.setFont(QFont("Microsoft YaHei", 8))
            p.drawText(QRectF(card.right() - 50, card.y() + card_h - 22, 42, 20),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, unit)

    # ==================== 中间: 端面同轴三环仪表 (方案 1A) ====================
    def _paint_motor_face(self, p: QPainter, rect: QRectF):
        """端面视角同轴三环仪表: 内盘转子+力矩, 中环电机端, 外环输出端, 齿环=减速器, 刻度环=编码器。"""
        state = self._data.get("sys_state_name", "IDLE")
        main_color, glow_color, _, blink = _STATE_STYLE.get(
            state, _STATE_STYLE["IDLE"])
        dim_main = main_color
        if blink:
            ph = (time.monotonic() * (1.0 if state == "FAULT" else 0.5)) % 1.0
            if ph > 0.5:
                dim_main = "#3a1a1a" if state == "FAULT" else "#3a2a1a"

        # 标题
        p.setPen(theme.c("title"))
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        p.drawText(QRectF(rect.x() + 10, rect.y() + 4, rect.width() - 20, 22),
                   Qt.AlignmentFlag.AlignLeft, "▎关节电机端面视图")

        area = QRectF(rect.x(), rect.y() + 28, rect.width(), rect.height() - 28)
        cx = area.center().x()
        cy = area.center().y()
        R = min(area.width(), area.height()) * 0.40   # 外环半径

        # 半径分层
        r_out = R                 # 外环 (输出端)
        r_gear = R * 0.74         # 齿啮合带
        r_mid = R * 0.64          # 中环 (电机端)
        r_rotor = R * 0.46        # 转子内盘

        # 数据
        d = self._data
        pos = float(d.get("pos", 0.0) or 0.0)
        pos_out = float(d.get("pos_out", 0.0) or 0.0)
        vel = float(d.get("vel", 0.0) or 0.0)
        vel_out = float(d.get("vel_out", 0.0) or 0.0)
        torque = float(d.get("torque", 0.0) or 0.0)
        iq = float(d.get("iq", 0.0) or 0.0)
        vmax = max(self._max_speed(), 1.0)
        gear = self._gear_ratio()
        tgt = self._read_cmd_target_pos()

        center = QPointF(cx, cy)

        # ---- 背景盘 ----
        p.setBrush(QBrush(theme.c("twin_bg_bottom")))
        p.setPen(QPen(theme.c("border"), 1.0))
        p.drawEllipse(center, r_out + 16, r_out + 16)

        # ========== 外环: 输出端 (减速器之后) ==========
        # 速度环形条 (输出端速度, 归一到电机端量程/减速比)
        vmax_out = max(vmax / max(gear, 1.0), 1e-3)
        self._draw_ring_arc(p, center, r_out, R * 0.085,
                            vel_out / vmax_out, "#00d4ff", track=True)
        # 位置指针 (输出端绝对角)
        self._draw_gauge_needle(p, center, r_out - R * 0.12, r_out + R * 0.02,
                                pos_out, "#00d4ff", width=2.5)
        # 外环标尺刻度
        self._draw_tick_ring(p, center, r_out + R * 0.04, 24, theme.c("muted"))

        # ========== 齿啮合带: 减速器 (随 vel 缓慢滚动) ==========
        self._draw_gear_band(p, center, r_gear, R * 0.05,
                             self._rotor_angle / max(gear, 1.0), dim_main)

        # ========== 中环: 电机端 ==========
        self._draw_ring_arc(p, center, r_mid, R * 0.085,
                            vel / vmax, main_color, track=True)
        # 目标位置虚影扇区 (cmd_target_pos vs pos = 跟随误差)
        if tgt is not None:
            self._draw_follow_shadow(p, center, r_mid, R * 0.085, pos, tgt, main_color)
        # 位置指针 (电机端)
        self._draw_gauge_needle(p, center, r_mid - R * 0.10, r_mid + R * 0.02,
                                pos, main_color, width=2.5)
        # 编码器刻度环 (贴中环外缘, 测电机端角)
        self._draw_tick_ring(p, center, r_mid + R * 0.05, 36, glow_color, minor=True)
        # 编码器当前角标记
        ea = pos
        p.setBrush(QBrush(QColor(glow_color)))
        p.setPen(Qt.PenStyle.NoPen)
        em = QPointF(cx + (r_mid + R * 0.05) * math.cos(ea - math.pi / 2),
                     cy + (r_mid + R * 0.05) * math.sin(ea - math.pi / 2))
        p.drawEllipse(em, 3, 3)

        # ========== 内盘: 转子 + 力矩/电流热度 ==========
        # 力矩填充 (中心向外, 颜色随正负)
        t_norm = max(-1.0, min(1.0, torque / max(self._peak_torque(), 1e-3)))
        tcol = QColor(_CUR_POS if torque >= 0 else _CUR_NEG)
        fill_grad = QLinearGradient(cx, cy - r_rotor, cx, cy + r_rotor)
        c0 = QColor(tcol); c0.setAlpha(int(40 + 150 * abs(t_norm)))
        c1 = QColor(tcol); c1.setAlpha(20)
        fill_grad.setColorAt(0, c0)
        fill_grad.setColorAt(1, c1)
        p.setBrush(QBrush(fill_grad))
        p.setPen(QPen(QColor(glow_color), 1.2))
        p.drawEllipse(center, r_rotor, r_rotor)
        # 旋转转子磁极 (复用侧视的视觉语言, 这里正圆)
        self._draw_face_rotor(p, center, r_rotor * 0.82, self._rotor_angle, glow_color)

        # ========== 中心读数 ==========
        p.setPen(QColor(main_color))
        p.setFont(QFont("Consolas", 15, QFont.Weight.Bold))
        p.drawText(QRectF(cx - r_rotor, cy - 18, r_rotor * 2, 20),
                   Qt.AlignmentFlag.AlignCenter, f"{vel:+.1f}")
        p.setPen(theme.c("muted"))
        p.setFont(QFont("Microsoft YaHei", 8))
        p.drawText(QRectF(cx - r_rotor, cy + 2, r_rotor * 2, 14),
                   Qt.AlignmentFlag.AlignCenter, "rad/s")

        # ========== 环标注 (右上/右下/左下 角注) ==========
        self._draw_face_legend(p, area, gear, pos, pos_out, torque, iq)

    def _draw_ring_arc(self, p: QPainter, c: QPointF, r: float, thick: float,
                        frac: float, color: str, track: bool = False):
        """环形进度条: frac∈[-1,1], 从顶部 12 点起, 正值顺时针。"""
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        # 轨道底
        if track:
            tc = QColor(color); tc.setAlpha(45)
            p.setPen(QPen(tc, thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(rect, 0, 360 * 16)
        frac = max(-1.0, min(1.0, frac))
        span = int(-frac * 270 * 16)   # 最大 270°, 顺时针为负角
        start = 90 * 16                # 12 点方向
        # 辉光 + 主弧
        for w, a in ((thick + 4, 50), (thick, 255)):
            cc = QColor(color); cc.setAlpha(a)
            p.setPen(QPen(cc, w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(rect, start, span)

    def _draw_gauge_needle(self, p: QPainter, c: QPointF, r0: float, r1: float,
                            angle: float, color: str, width: float = 2.0):
        """从 r0 到 r1 的指针, angle=0 指向 12 点, 顺时针。"""
        a = angle - math.pi / 2
        p0 = QPointF(c.x() + r0 * math.cos(a), c.y() + r0 * math.sin(a))
        p1 = QPointF(c.x() + r1 * math.cos(a), c.y() + r1 * math.sin(a))
        for w, alpha in ((width + 3, 60), (width, 255)):
            cc = QColor(color); cc.setAlpha(alpha)
            p.setPen(QPen(cc, w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(p0, p1)

    def _draw_follow_shadow(self, p: QPainter, c: QPointF, r: float, thick: float,
                             pos: float, tgt: float, color: str):
        """pos→tgt 之间的虚影扇区 (跟随误差可视化)。"""
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        a0 = (pos) * 180 / math.pi
        a1 = (tgt) * 180 / math.pi
        start = int((90 - a0) * 16)
        span = int(-(a1 - a0) * 16)
        cc = QColor(color); cc.setAlpha(70)
        p.setPen(QPen(cc, thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(rect, start, span)

    def _draw_tick_ring(self, p: QPainter, c: QPointF, r: float, n: int,
                         color, minor: bool = False):
        """刻度环: n 个刻度线。"""
        col = color if isinstance(color, QColor) else QColor(color)
        col = QColor(col); col.setAlpha(150)
        p.setPen(QPen(col, 1.0))
        ln = r * (0.04 if minor else 0.06)
        for i in range(n):
            a = i * 2 * math.pi / n - math.pi / 2
            p0 = QPointF(c.x() + (r - ln) * math.cos(a), c.y() + (r - ln) * math.sin(a))
            p1 = QPointF(c.x() + r * math.cos(a), c.y() + r * math.sin(a))
            p.drawLine(p0, p1)

    def _draw_gear_band(self, p: QPainter, c: QPointF, r: float, thick: float,
                         angle: float, color: str):
        """齿啮合带: 沿环分布的小齿块, 随 angle 缓慢滚动 (表达减速机构)。"""
        n = 48
        cc = QColor(color); cc.setAlpha(110)
        p.setPen(QPen(QColor(color).darker(140), 1.0))
        for i in range(n):
            a = i * 2 * math.pi / n + angle
            # 交替内外, 形成齿纹
            rr = r + (thick * 0.5 if i % 2 == 0 else -thick * 0.5)
            pt = QPointF(c.x() + rr * math.cos(a), c.y() + rr * math.sin(a))
            p.setBrush(QBrush(cc if i % 2 == 0 else QColor(color).darker(120)))
            p.drawEllipse(pt, thick * 0.32, thick * 0.32)

    def _draw_face_rotor(self, p: QPainter, c: QPointF, r: float,
                          angle: float, glow: str):
        """端面转子: 旋转磁极 (N/S 交替) + 发光毂。"""
        for i in range(4):
            a = angle + i * math.pi / 2
            col = QColor("#00d4ff") if i % 2 == 0 else QColor("#ff6b35")
            # 拖影
            for k in range(3, 0, -1):
                ak = a - k * 0.12
                pk = QPointF(c.x() + r * math.cos(ak), c.y() + r * math.sin(ak))
                tc = QColor(col); tc.setAlpha(36 // k)
                p.setBrush(QBrush(tc)); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(pk, r * 0.16, r * 0.16)
            pt = QPointF(c.x() + r * math.cos(a), c.y() + r * math.sin(a))
            cc = QColor(col); cc.setAlpha(200)
            p.setBrush(QBrush(cc)); p.setPen(QPen(cc.darker(120), 1.0))
            p.drawEllipse(pt, r * 0.18, r * 0.18)
        # 中心毂辉光
        hub = QColor(glow); hub.setAlpha(160)
        p.setBrush(QBrush(hub)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(c, r * 0.18, r * 0.18)

    def _draw_face_legend(self, p: QPainter, area: QRectF, gear: float,
                           pos: float, pos_out: float, torque: float, iq: float):
        """端面视图四角图例/读数。"""
        items = [
            ("left",  area.y() + 30,  "● 电机端位置", f"{pos:+.4f} rad", "#aaccff"),
            ("left",  area.y() + 64,  "● 输出端位置", f"{pos_out:+.4f} rad", "#00d4ff"),
            ("right", area.y() + 30,  "减速比",       f"{gear:.0f} : 1", theme.hex("twin_glass")),
            ("right", area.y() + 64,  "力矩",         f"{torque:+.3f} Nm",
             _CUR_POS if torque >= 0 else _CUR_NEG),
            ("right", area.y() + 98,  "Iq 电流",      f"{iq:+.3f} A",
             _CUR_POS if iq >= 0 else _CUR_NEG),
        ]
        for side, y, label, value, color in items:
            if side == "left":
                lx = area.x() + 8
                p.setPen(theme.c("muted"))
                p.setFont(QFont("Microsoft YaHei", 8))
                p.drawText(QRectF(lx, y, 120, 14), Qt.AlignmentFlag.AlignLeft, label)
                p.setPen(QColor(color))
                p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
                p.drawText(QRectF(lx, y + 13, 140, 16), Qt.AlignmentFlag.AlignLeft, value)
            else:
                rx = area.right() - 148
                p.setPen(theme.c("muted"))
                p.setFont(QFont("Microsoft YaHei", 8))
                p.drawText(QRectF(rx, y, 140, 14), Qt.AlignmentFlag.AlignRight, label)
                p.setPen(QColor(color))
                p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
                p.drawText(QRectF(rx, y + 13, 140, 16), Qt.AlignmentFlag.AlignRight, value)

    def _peak_torque(self) -> float:
        """峰值力矩 (Nm), 用于力矩归一化, 失败回退。"""
        mp = self._engine_mp()
        if mp is not None:
            try:
                v = float(getattr(mp.motor_base, "peak_torque", 0.0))
                if v > 0:
                    return v
            except (AttributeError, TypeError, ValueError):
                pass
        return 1.0

    # ==================== 中间 3D 关节电机模型 ====================
    def _paint_motor_3d(self, p: QPainter, rect: QRectF):
        """绘制玻璃透视关节电机 (透明外壳 + 发光铜绕组 + 亮芯转子 + 金色支架) + 引线标注。"""
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
        p.setPen(theme.c("title"))
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

        # 圆柱半径 (尽量放大填充中央区)
        motor_r = min(96, model_rect.height() * 0.32)
        gear_r = motor_r * 1.12   # 减速箱略粗
        enc_r = motor_r * 0.45    # 编码器较细
        shaft_r = motor_r * 0.20  # 输出轴最细

        # 金色支架底座 (电机两端 + 减速箱下方)
        base_y = cy + gear_r + 6
        self._draw_brass_mount(p, motor_x + motor_w * 0.18, base_y, motor_r * 0.55)
        self._draw_brass_mount(p, motor_x + motor_w * 0.82, base_y, motor_r * 0.55)
        self._draw_brass_mount(p, gear_x + gear_w * 0.5, base_y, gear_r * 0.5)

        # 1. 编码器段 (尾部小圆柱, 不透明金属)
        self._draw_cylinder(p, enc_x, cy, enc_w, enc_r, "#4a5060", "#2a3040")
        p.setPen(QPen(QColor("#6a7080"), 1.0))
        for i in range(3):
            ix = enc_x + (i + 1) * enc_w / 4
            p.drawLine(QPointF(ix, cy - enc_r * 0.7), QPointF(ix, cy + enc_r * 0.7))

        # 2. 电机本体: 先画内部 (铜绕组 + 转子), 再罩玻璃外壳
        self._draw_motor_stator(p, motor_x, cy, motor_w, motor_r, dim_main)
        self._draw_motor_rotor(p, motor_x, cy, motor_w, motor_r, self._rotor_angle, glow_color)
        self._draw_glass_cylinder(p, motor_x, cy, motor_w, motor_r)

        # 3. 减速箱段: 内部行星轮 + 玻璃外壳
        self._draw_gearbox_detail(p, gear_x, cy, gear_w, gear_r, self._rotor_angle, dim_main)
        self._draw_glass_cylinder(p, gear_x, cy, gear_w, gear_r)

        # 4. 输出轴 (细圆柱伸出, 金属)
        self._draw_cylinder(p, shaft_x, cy, shaft_w, shaft_r, "#8a8a90", "#5a5a60")
        p.setBrush(QBrush(QColor(dim_main)))
        p.setPen(QPen(QColor(dim_main).darker(120), 1.0))
        p.drawEllipse(QPointF(shaft_x + shaft_w, cy), 4, 4)

        # 5. 引线标注 (左右分流, 竖直堆叠到两侧, 不交叉)
        vel = float(self._data.get("vel", 0.0) or 0.0)
        torque = float(self._data.get("torque", 0.0) or 0.0)
        temp_motor = float(self._data.get("temp_motor", 35.0) or 35.0)
        # (锚点x, 锚点y, side, text, color)
        left_annots = [
            (enc_x + enc_w * 0.5, cy, "编码器", "#9FB6DD"),
            (motor_x + motor_w * 0.4, cy - motor_r * 0.5,
             f"{vel:+.2f} rad/s", main_color),
            (motor_x + motor_w * 0.5, cy + motor_r * 0.5,
             f"温度 {temp_motor:.1f} ℃", "#ffaa44"),
        ]
        right_annots = [
            (motor_x + motor_w * 0.6, cy - motor_r * 0.4,
             f"{torque:+.3f} Nm", _CUR_POS if torque >= 0 else _CUR_NEG),
            (gear_x + gear_w * 0.5, cy - gear_r * 0.4,
             f"减速比 {self._gear_ratio():.0f}:1", theme.hex("twin_glass")),
            (shaft_x + shaft_w, cy, "输出轴", "#9FB6DD"),
        ]
        self._draw_leader_column(p, left_annots, "left", model_rect)
        self._draw_leader_column(p, right_annots, "right", model_rect)

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

    def _draw_glass_cylinder(self, p: QPainter, x: float, cy: float,
                              w: float, r: float):
        """绘制玻璃透视外壳 (半透明填充 + 青色高光描边 + 顶部反光带)。

        必须在内部元件 (绕组/转子) 之后调用, 罩在外层。
        """
        glass = theme.c("twin_glass")
        ell_w = r * 0.25
        # 半透明筒身填充 (上半略亮, 下半渐隐)
        fill = QColor(theme.c("twin_glass_fill"))
        grad = QLinearGradient(0, cy - r, 0, cy + r)
        g_top = QColor(fill); g_top.setAlpha(min(255, fill.alpha() + 24))
        g_bot = QColor(fill); g_bot.setAlpha(max(0, fill.alpha() - 12))
        grad.setColorAt(0, g_top)
        grad.setColorAt(0.5, g_bot)
        grad.setColorAt(1, g_top)
        p.setBrush(QBrush(grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(QRectF(x, cy - r, w, 2 * r))

        # 外壳描边 (双层: 外淡内亮)
        edge_o = QColor(glass); edge_o.setAlpha(70)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(edge_o, 2.5))
        p.drawLine(QPointF(x, cy - r), QPointF(x + w, cy - r))
        p.drawLine(QPointF(x, cy + r), QPointF(x + w, cy + r))
        edge_i = QColor(glass); edge_i.setAlpha(180)
        p.setPen(QPen(edge_i, 1.0))
        p.drawLine(QPointF(x, cy - r), QPointF(x + w, cy - r))
        p.drawLine(QPointF(x, cy + r), QPointF(x + w, cy + r))

        # 端面椭圆环 (玻璃圈)
        for ex in (x, x + w):
            p.setPen(QPen(edge_i, 1.2))
            p.drawEllipse(QPointF(ex, cy), ell_w, r)

        # 顶部高光反光带
        hl = QColor("#FFFFFF"); hl.setAlpha(40)
        p.setPen(QPen(hl, 2.0))
        p.drawLine(QPointF(x + 6, cy - r * 0.62), QPointF(x + w - 6, cy - r * 0.62))

    def _draw_brass_mount(self, p: QPainter, cx: float, base_y: float, half_w: float):
        """绘制金色支架 (拱形托座: 渐变金 + 暗描边 + 顶部高光)。"""
        brass = theme.c("twin_brass")
        brass_dark = theme.c("twin_brass_dark")
        h = max(14.0, half_w * 0.9)
        # 拱形支架: 上窄下宽梯形 + 底脚
        path = QPainterPath()
        path.moveTo(cx - half_w * 0.55, base_y)            # 上左
        path.lineTo(cx + half_w * 0.55, base_y)            # 上右
        path.lineTo(cx + half_w, base_y + h)               # 下右
        path.lineTo(cx - half_w, base_y + h)               # 下左
        path.closeSubpath()
        grad = QLinearGradient(0, base_y, 0, base_y + h)
        grad.setColorAt(0, QColor(brass).lighter(125))
        grad.setColorAt(0.5, QColor(brass))
        grad.setColorAt(1, QColor(brass_dark))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor(brass_dark).darker(120), 1.0))
        p.drawPath(path)
        # 顶部高光
        hl = QColor(brass).lighter(150)
        p.setPen(QPen(hl, 1.2))
        p.drawLine(QPointF(cx - half_w * 0.5, base_y + 1),
                   QPointF(cx + half_w * 0.5, base_y + 1))
        # 底脚
        p.setBrush(QBrush(QColor(brass_dark)))
        p.setPen(QPen(QColor(brass_dark).darker(130), 1.0))
        p.drawRoundedRect(QRectF(cx - half_w, base_y + h, half_w * 2, 4), 2, 2)
        # 固定螺栓点
        p.setBrush(QBrush(QColor(brass).lighter(130)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, base_y + h * 0.55), 2.2, 2.2)

    def _draw_motor_stator(self, p: QPainter, x: float, cy: float,
                            w: float, r: float, color: str):
        """绘制电机定子: 沿轴向排列的发光铜绕组环 (透过玻璃可见)。"""
        p.save()
        p.setClipRect(QRectF(x, cy - r, w, 2 * r))
        # 亮度脉动: 随 iq 幅值 (0~1)
        iq = abs(float(self._data.get("iq", 0.0) or 0.0))
        load = max(0.0, min(1.0, iq / max(self._peak_current(), 1.0)))
        copper = theme.c("twin_copper")
        glow_rgba = QColor(theme.c("twin_copper_glow"))
        # 一排铜绕组环 (沿轴向 6 组, 上下两弧)
        n_coil = 7
        ring_r = r * 0.78
        for i in range(n_coil):
            rx = x + w * (i + 0.5) / n_coil
            # 辉光底 (亮度随负载)
            ga = int(glow_rgba.alpha() * (0.4 + 0.6 * load))
            gc = QColor(glow_rgba); gc.setAlpha(max(20, min(255, ga)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(gc, 4.0))
            p.drawArc(QRectF(rx - r * 0.16, cy - ring_r, r * 0.32, ring_r * 2),
                      30 * 16, 120 * 16)     # 顶部弧
            p.drawArc(QRectF(rx - r * 0.16, cy - ring_r, r * 0.32, ring_r * 2),
                      210 * 16, 120 * 16)    # 底部弧
            # 铜环主体
            cc = QColor(copper).lighter(int(105 + 35 * load))
            p.setPen(QPen(cc, 2.0))
            p.drawArc(QRectF(rx - r * 0.16, cy - ring_r, r * 0.32, ring_r * 2),
                      30 * 16, 120 * 16)
            p.drawArc(QRectF(rx - r * 0.16, cy - ring_r, r * 0.32, ring_r * 2),
                      210 * 16, 120 * 16)
        p.restore()

    def _draw_motor_rotor(self, p: QPainter, x: float, cy: float,
                           w: float, r: float, angle: float, glow: str):
        """绘制电机内部旋转转子 (发光中轴 + 旋转磁极拖影)。"""
        p.save()
        p.setClipRect(QRectF(x, cy - r, w, 2 * r))
        # 发光中轴 (多层辉光)
        for pen in self._glow_pen(glow, 2.0, glow=3):
            p.setPen(pen)
            p.drawLine(QPointF(x, cy), QPointF(x + w, cy))
        # 旋转磁极 (4 极, 跟随 angle, 带拖影)
        inner_r = r * 0.45
        cx = x + w * 0.5
        for i in range(4):
            a = angle + i * math.pi / 2
            c = QColor("#00d4ff") if i % 2 == 0 else QColor("#ff6b35")
            # 拖影 (3 段递减)
            for k in range(3, 0, -1):
                ak = a - k * 0.12
                px = cx + inner_r * math.cos(ak) * 0.3
                py = cy + inner_r * math.sin(ak)
                tc = QColor(c); tc.setAlpha(40 // k)
                p.setBrush(QBrush(tc))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(px, py), 5, 5)
            # 本体
            px = cx + inner_r * math.cos(a) * 0.3
            py = cy + inner_r * math.sin(a)
            cc = QColor(c); cc.setAlpha(190)
            p.setBrush(QBrush(cc))
            p.setPen(QPen(cc.darker(120), 1.0))
            p.drawEllipse(QPointF(px, py), 5, 5)
        # 中心发光毂
        hub = QColor(glow); hub.setAlpha(150)
        p.setBrush(QBrush(hub))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), 4, 4)
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

    def _draw_leader_column(self, p: QPainter, annots: list, side: str,
                             bounds: QRectF):
        """把一组标注竖直堆叠到模型一侧, 引线 锚点→水平→标签, 互不交叉。

        annots: [(锚点x, 锚点y, text, color), ...] 按锚点 y 排序后均匀分布。
        side='left' 标签贴左缘, 'right' 贴右缘。
        """
        if not annots:
            return
        p.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        fm = p.fontMetrics()
        th = fm.height()
        pad = 6
        box_h = th + 6

        # 标签 x 列位置 (贴边)
        if side == "left":
            col_w = max(fm.horizontalAdvance(a[2]) for a in annots) + 2 * pad
            col_x = bounds.x() + 6
        else:
            col_w = max(fm.horizontalAdvance(a[2]) for a in annots) + 2 * pad
            col_x = bounds.right() - 6 - col_w

        # 竖直均匀分布标签 y (按锚点 y 排序)
        ordered = sorted(annots, key=lambda a: a[1])
        n = len(ordered)
        gap = 10
        total_h = n * box_h + (n - 1) * gap
        y0 = bounds.center().y() - total_h / 2
        y0 = max(bounds.y() + 30, y0)

        leader = QColor(theme.c("twin_leader"))
        for i, (ax, ay, text, color) in enumerate(ordered):
            box_y = y0 + i * (box_h + gap)
            box = QRectF(col_x, box_y, col_w, box_h)
            anchor = QPointF(ax, ay)
            # 引线连接点 (标签内缘中点)
            if side == "left":
                join = QPointF(box.right(), box.center().y())
                mid_x = (join.x() + anchor.x()) / 2
            else:
                join = QPointF(box.left(), box.center().y())
                mid_x = (join.x() + anchor.x()) / 2
            # 折线: 锚点 → 水平中段 → 标签 (台阶式)
            path = QPainterPath()
            path.moveTo(anchor)
            path.lineTo(QPointF(mid_x, anchor.y()))
            path.lineTo(QPointF(mid_x, join.y()))
            path.lineTo(join)
            p.setPen(QPen(leader, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            # 锚点小圆
            p.setBrush(QBrush(QColor(color)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(anchor, 2.2, 2.2)
            # 标签胶囊
            p.setBrush(QBrush(theme.c("twin_panel_bg")))
            p.setPen(QPen(QColor(color), 1.0))
            p.drawRoundedRect(box, 3, 3)
            p.setPen(QColor(color))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    # ==================== 右侧面板 (告警 + 功率曲线) ====================
    def _paint_right_panel(self, p: QPainter, rect: QRectF):
        """绘制右侧: 上半告警列表 + 下半功率曲线。"""
        # 面板背景
        p.setBrush(QBrush(theme.c("twin_panel_bg")))
        p.setPen(QPen(theme.c("border"), 1.0))
        p.drawRoundedRect(rect, 6, 6)

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
        p.setPen(theme.c("title"))
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
        p.setPen(theme.c("title"))
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        p.drawText(QRectF(rect.x(), rect.y(), rect.width(), 22),
                   Qt.AlignmentFlag.AlignLeft, "▎功率趋势")

        # 绘图区
        plot_rect = QRectF(rect.x() + 8, rect.y() + 28,
                            rect.width() - 16, rect.height() - 40)
        # 背景
        p.setBrush(QBrush(theme.c("twin_bg_bottom")))
        p.setPen(QPen(theme.c("border"), 1.0))
        p.drawRect(plot_rect)
        # 网格
        p.setPen(QPen(theme.c("grid"), 1.0))
        for i in range(1, 4):
            gx = plot_rect.x() + plot_rect.width() * i / 4
            p.drawLine(QPointF(gx, plot_rect.y()), QPointF(gx, plot_rect.bottom()))
            gy = plot_rect.y() + plot_rect.height() * i / 4
            p.drawLine(QPointF(plot_rect.x(), gy), QPointF(plot_rect.right(), gy))
        # 零线
        mid_y = plot_rect.y() + plot_rect.height() / 2
        p.setPen(QPen(theme.c("muted"), 1.0))
        p.drawLine(QPointF(plot_rect.x(), mid_y), QPointF(plot_rect.right(), mid_y))

        if len(self._power_hist) < 2:
            p.setPen(theme.c("muted"))
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

        color = _FLOW_POS if self._power_hist[-1][1] >= 0 else _FLOW_NEG

        # 线下填充 (到零线, 柔和渐变)
        p.save()
        p.setClipRect(plot_rect)
        fill_path = QPainterPath()
        fill_path.moveTo(QPointF(pts[0].x(), mid_y))
        for pt in pts:
            fill_path.lineTo(pt)
        fill_path.lineTo(QPointF(pts[-1].x(), mid_y))
        fill_path.closeSubpath()
        fg = QLinearGradient(0, plot_rect.y(), 0, plot_rect.bottom())
        c_top = QColor(color); c_top.setAlpha(70)
        c_bot = QColor(color); c_bot.setAlpha(0)
        fg.setColorAt(0, c_top)
        fg.setColorAt(0.5, c_bot)
        fg.setColorAt(1, c_top)
        p.setBrush(QBrush(fg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(fill_path)
        p.restore()

        # 辉光
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
        p.setBrush(QBrush(theme.c("twin_panel_bg")))
        p.setPen(QPen(theme.c("border"), 1.0))
        p.drawRoundedRect(rect, 6, 6)

        # 标题
        p.setPen(theme.c("title"))
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
                p.setPen(QPen(theme.c("border"), 1.0))
                p.drawLine(QPointF(x, table_rect.y() + 4),
                           QPointF(x, table_rect.bottom() - 4))

    # ==================== 辅助: 网格底纹 ====================
    def _paint_grid(self, p: QPainter):
        """科幻感网格底纹 (透明度走主题)。"""
        grid_c = theme.c("grid")
        p.setPen(QPen(grid_c, 1.0))
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
