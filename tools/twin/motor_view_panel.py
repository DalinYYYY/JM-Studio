"""电机可视化面板: 2.5D 全息电机 + 能量流 + 状态反馈。

以科幻全息风格实时呈现电机运行状态:
  - 中央 2.5D 电机本体 (定子环/转子/N-S磁极/A-B-C绕组/位置指针/扫描弧)
  - 右侧 HUD: 三相电流柱状条 + 母线圆环仪表 + 加速度数字
  - 能量流粒子通道 (母线 ↔ 电机, 方向跟随 Power 符号)
  - 状态反馈 (IDLE/READY/RUN/FAULT/SAFETY/CALIB 颜色+动画)

绘制: PyQt6 QWidget + QPainter 自绘, 不依赖 OpenGL。
性能: feed() 仅更新缓存+置 dirty, paintEvent 仅 dirty 时重绘。
"""
import math
import time

from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap,
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


class MotorViewPanel(QWidget):
    """2.5D 全息电机可视化面板。"""

    # 粒子数量
    N_PARTICLES = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(900, 600)
        # 透明背景, 由 paintEvent 自绘
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
        self._anim_t0 = time.monotonic()

        # 预生成辉光 QPen 缓存: (color, width) -> [QPen 外层淡, QPen 内层亮]
        self._glow_pens = {}
        # 预生成粒子 QPixmap
        self._particle_pixmaps = self._build_particle_pixmaps()

        # 重绘定时器: 30Hz
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    # ==================== 公共 API ====================
    def set_engine(self, engine):
        """注入引擎句柄, 用于读 cmd_target_pos / 位置限位。"""
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
        self._dirty = True

    def apply_theme(self):
        """主题切换: 清除辉光缓存, 下次重绘重建。"""
        self._glow_pens.clear()
        self.update()

    # ==================== 内部: 动画 tick ====================
    def _tick(self):
        """30Hz 定时器: 仅在可见 + dirty 时推进动画 + 触发重绘。"""
        if not self.isVisible():
            return
        # 推进动画相位 (即使无新数据也推进, 让粒子/扫描动起来)
        now = time.monotonic()
        elapsed = now - self._anim_t0
        self._anim_t0 = now
        # 扫描弧相位
        state = self._data.get("sys_state_name", "IDLE")
        _, _, scan_hz, _ = _STATE_STYLE.get(state, _STATE_STYLE["IDLE"])
        if scan_hz > 0:
            self._scan_phase += scan_hz * elapsed
            self._dirty = True
        # 粒子相位 (跟随 Power)
        power = float(self._data.get("power", 0.0) or
                      (float(self._data.get("vbus", 0.0)) *
                       float(self._data.get("ibus", 0.0))))
        speed = max(-1.0, min(1.0, power / 500.0))
        self._particle_phase += speed * elapsed * 0.5
        # FAULT/SAFETY 闪烁需要持续重绘
        if _STATE_STYLE.get(state, (None, None, 0, False))[3]:
            self._dirty = True
        if self._dirty:
            self.update()

    # ==================== 绘制入口 ====================
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 深色背景
        p.fillRect(self.rect(), QColor("#0a0e1a"))
        # 网格底纹 (科幻感)
        self._paint_grid(p)

        W = self.width()
        H = self.height()

        # 电机本体区 (居左)
        motor_cx = 300
        motor_cy = int(H * 0.45)
        motor_R = min(180, int(H * 0.32))
        self._paint_motor(p, motor_cx, motor_cy, motor_R)

        # 右侧 HUD 区
        hud_x = max(620, W - 280)
        self._paint_current_bars(p, hud_x, 20, 260, 180)
        self._paint_bus_gauges(p, hud_x, 220, 260, 100)
        self._paint_acc_gauge(p, hud_x, 340, 260, 80)

        # 能量流通道 (母线 HUD → 电机本体)
        self._paint_energy_flow(p, motor_cx + motor_R, motor_cy,
                                hud_x, 270)

        # 底部状态条
        self._paint_status_bar(p, QRectF(0, H - 40, W, 40))

        self._dirty = False

    # ==================== 电机本体 ====================
    def _paint_motor(self, p: QPainter, cx: int, cy: int, R: int):
        """绘制 2.5D 全息电机本体。"""
        state = self._data.get("sys_state_name", "IDLE")
        main_color, glow_color, _, blink = _STATE_STYLE.get(
            state, _STATE_STYLE["IDLE"])

        # FAULT/SAFETY 闪烁
        if blink:
            phase = (time.monotonic() * (1.0 if state == "FAULT" else 0.5)) % 1.0
            if phase > 0.5:
                main_color = "#3a1a1a" if state == "FAULT" else "#3a2a1a"
                glow_color = "#5a2a2a" if state == "FAULT" else "#5a3a2a"

        # 1. 外圈告警环 (FAULT/SAFETY)
        if state in ("FAULT", "SAFETY"):
            for i in range(3, 0, -1):
                c = QColor(glow_color); c.setAlpha(40 // i)
                p.setPen(QPen(c, 2 + i * 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(cx, cy), R + 12 + i * 3, R + 12 + i * 3)

        # 2. 定子外环 (双层辉光)
        for pen in self._glow_pen(main_color, 1.5, glow=4):
            p.setPen(pen)
            p.setBrush(QBrush(QColor("#0f1419")))
            p.drawEllipse(QPointF(cx, cy), R, R)

        # 3. 定子内环
        p.setPen(QPen(QColor(main_color), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), R - 8, R - 8)

        # 4. 外圈刻度 (12 等分)
        p.setPen(QPen(QColor(main_color).darker(150), 1.0))
        for i in range(12):
            ang = i * 30.0
            rad = math.radians(ang - 90)
            x1 = cx + (R + 2) * math.cos(rad)
            y1 = cy + (R + 2) * math.sin(rad)
            x2 = cx + (R + 8) * math.cos(rad)
            y2 = cy + (R + 8) * math.sin(rad)
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # 5. 扫描弧 (READY/RUN/CALIB)
        if _STATE_STYLE.get(state, (None, None, 0, False))[2] > 0:
            self._paint_scan_arc(p, cx, cy, R + 4, glow_color)

        # 6. A/B/C 三相绕组槽 (6 槽, 每相 2 槽)
        ia = float(self._data.get("ia", 0.0) or 0.0)
        ib = float(self._data.get("ib", 0.0) or 0.0)
        ic = float(self._data.get("ic", 0.0) or 0.0)
        peak = max(float(self._data.get("peak_current", 30.0) or 30.0), 1.0)
        # 三相角度: A=0°, B=120°, C=240°, 每相两个对称槽 (+180°)
        for phase_ang, cur in ((0.0, ia), (120.0, ib), (240.0, ic)):
            for off in (0.0, 180.0):
                ang = phase_ang + off
                rad = math.radians(ang - 90)
                sx = cx + (R - 20) * math.cos(rad)
                sy = cy + (R - 20) * math.sin(rad)
                # 归一化电流强度 [0, 1]
                intensity = min(1.0, abs(cur) / peak)
                color = _CUR_POS if cur >= 0 else _CUR_NEG
                c = QColor(color)
                c.setAlpha(int(80 + 175 * intensity))
                p.setBrush(QBrush(c))
                p.setPen(QPen(QColor(color).darker(120), 1.0))
                p.drawEllipse(QPointF(sx, sy), 6, 10)

        # 7. 转子 (按 pos 旋转)
        pos = float(self._data.get("pos", 0.0) or 0.0)
        rotor_R = R - 35
        self._paint_rotor(p, cx, cy, rotor_R, pos, main_color, glow_color)

        # 8. 速度拖影弧
        vel = float(self._data.get("vel", 0.0) or 0.0)
        max_speed = max(float(self._data.get("max_speed", 1000.0) or 1000.0), 1.0)
        vel_norm = min(1.0, abs(vel) / max_speed)
        if vel_norm > 0.05:
            self._paint_vel_trail(p, cx, cy, R - 8, vel, vel_norm, glow_color)

        # 9. 位置指针 (实测 + 目标)
        self._paint_position_pointer(p, cx, cy, R, pos, main_color)
        # 目标位置 (橙红)
        cmd_pos = self._read_cmd_target_pos()
        if cmd_pos is not None:
            self._paint_position_pointer(p, cx, cy, R + 18, cmd_pos, "#ff6b35", dashed=True)

        # 10. 中心标签: 模式 + 状态
        p.setPen(QColor(main_color))
        font = QFont("Consolas", 10, QFont.Weight.Bold)
        p.setFont(font)
        mode = self._data.get("control_mode_name", "-")
        p.drawText(QRectF(cx - 60, cy - 8, 120, 16),
                   Qt.AlignmentFlag.AlignCenter, f"[{mode}]")
        p.setPen(QColor(main_color).darker(130))
        font2 = QFont("Consolas", 8)
        p.setFont(font2)
        p.drawText(QRectF(cx - 60, cy + 8, 120, 14),
                   Qt.AlignmentFlag.AlignCenter, state)

    def _paint_rotor(self, p: QPainter, cx: int, cy: int, r: int,
                     angle: float, main_color: str, glow_color: str):
        """绘制转子 + N/S 磁极。"""
        p.save()
        p.translate(cx, cy)
        p.rotate(math.degrees(angle))

        # 转子主体
        for pen in self._glow_pen(glow_color, 1.0, glow=2):
            p.setPen(pen)
            p.setBrush(QBrush(QColor("#161a24")))
            p.drawEllipse(QPointF(0, 0), r, r)

        # 4 个磁极 (N/S 交替)
        pole_R = r * 0.55
        for i in range(4):
            ang = i * 90.0
            rad = math.radians(ang)
            px = pole_R * math.cos(rad)
            py = pole_R * math.sin(rad)
            is_n = (i % 2 == 0)
            color = "#ff6b35" if is_n else "#00d4ff"
            c = QColor(color); c.setAlpha(180)
            p.setBrush(QBrush(c))
            p.setPen(QPen(QColor(color), 1.0))
            # 磁极块 (小矩形)
            p.save()
            p.translate(px, py)
            p.rotate(ang * 180 / math.pi)
            p.drawRoundedRect(QRectF(-8, -5, 16, 10), 2, 2)
            p.restore()

        # 中心轴
        p.setPen(QPen(QColor(main_color), 1.5))
        p.setBrush(QBrush(QColor("#2a3040")))
        p.drawEllipse(QPointF(0, 0), 6, 6)

        p.restore()

    def _paint_position_pointer(self, p: QPainter, cx: int, cy: int,
                                 R: int, angle: float, color: str,
                                 dashed: bool = False):
        """绘制位置指针 (从中心向外的辉光线)。"""
        rad = math.radians(angle * 180 / math.pi - 90) if False else \
              math.radians(angle - 90)  # angle 已是 rad
        # 注意: pos 是 rad, 直接用
        rad = angle - math.pi / 2
        x = cx + R * math.cos(rad)
        y = cy + R * math.sin(rad)
        pen = QPen(QColor(color), 2.0)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(QPointF(cx, cy), QPointF(x, y))
        # 末端辉光点
        for pen in self._glow_pen(color, 1.0, glow=3):
            p.setPen(pen)
            p.setBrush(QBrush(QColor(color)))
            p.drawEllipse(QPointF(x, y), 3, 3)

    def _paint_vel_trail(self, p: QPainter, cx: int, cy: int, R: int,
                          vel: float, vel_norm: float, glow_color: str):
        """速度拖影弧: 速度越高拖影越长。"""
        # 拖影弧长 = vel_norm * 270°, 方向跟随 vel 符号
        arc_deg = vel_norm * 270.0
        start_deg = -90 - arc_deg / 2
        if vel < 0:
            start_deg = -90 - arc_deg
        # 多层辉光
        for i in range(3, 0, -1):
            c = QColor(glow_color); c.setAlpha(int(60 * vel_norm / i))
            pen = QPen(c, 3 + i * 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            rect = QRectF(cx - R, cy - R, 2 * R, 2 * R)
            p.drawArc(rect, int(start_deg * 16), int(arc_deg * 16))

    def _paint_scan_arc(self, p: QPainter, cx: int, cy: int, R: int,
                         glow_color: str):
        """扫描弧: 旋转的辉光弧段。"""
        arc_len = 60.0  # 弧段长度
        start = (self._scan_phase * 360.0) % 360.0 - arc_len / 2
        for i in range(3, 0, -1):
            c = QColor(glow_color); c.setAlpha(80 // i)
            pen = QPen(c, 2 + i)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            rect = QRectF(cx - R, cy - R, 2 * R, 2 * R)
            p.drawArc(rect, int(start * 16), int(arc_len * 16))

    # ==================== 三相电流柱状条 ====================
    def _paint_current_bars(self, p: QPainter, x: int, y: int,
                             w: int, h: int):
        """绘制 ia/ib/ic 三个正负柱状条。"""
        # 标题
        p.setPen(QColor("#7FD4FF"))
        p.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        p.drawText(QRectF(x, y, w, 16), Qt.AlignmentFlag.AlignLeft, "三相电流")

        ia = float(self._data.get("ia", 0.0) or 0.0)
        ib = float(self._data.get("ib", 0.0) or 0.0)
        ic = float(self._data.get("ic", 0.0) or 0.0)
        peak = max(float(self._data.get("peak_current", 30.0) or 30.0), 1.0)

        bar_w = 28
        gap = (w - 3 * bar_w) // 4
        by = y + 24
        bh = h - 48
        mid_y = by + bh // 2

        # 零线
        p.setPen(QPen(QColor("#3a4a5a"), 1.0))
        p.drawLine(x, mid_y, x + w, mid_y)

        for i, (cur, label) in enumerate(((ia, "ia"), (ib, "ib"), (ic, "ic"))):
            bx = x + gap + i * (bar_w + gap)
            # 归一化 [-1, 1]
            norm = max(-1.0, min(1.0, cur / peak))
            color = _CUR_POS if cur >= 0 else _CUR_NEG
            # 柱体
            if norm >= 0:
                rect = QRectF(bx, mid_y - norm * bh / 2, bar_w, norm * bh / 2)
            else:
                rect = QRectF(bx, mid_y, bar_w, -norm * bh / 2)
            # 辉光
            for j in range(3, 0, -1):
                c = QColor(color); c.setAlpha(50 // j)
                p.setPen(QPen(c, 1 + j))
                p.setBrush(QBrush(c))
                p.drawRoundedRect(rect.adjusted(-j, -j, j, j), 2, 2)
            # 主体
            p.setPen(QPen(QColor(color), 1.0))
            p.setBrush(QBrush(QColor(color).darker(150)))
            p.drawRoundedRect(rect, 2, 2)
            # 标签
            p.setPen(QColor("#9FB6DD"))
            p.setFont(QFont("Consolas", 8))
            p.drawText(QRectF(bx, by - 2, bar_w, 14),
                       Qt.AlignmentFlag.AlignCenter, label)
            # 数值
            p.setPen(QColor(color))
            p.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
            p.drawText(QRectF(bx, by + bh + 4, bar_w, 14),
                       Qt.AlignmentFlag.AlignCenter, f"{cur:+.2f}")

    # ==================== 母线圆环仪表 ====================
    def _paint_bus_gauges(self, p: QPainter, x: int, y: int,
                           w: int, h: int):
        """绘制 Vbus / Ibus / Power 三个圆环仪表。"""
        p.setPen(QColor("#7FD4FF"))
        p.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        p.drawText(QRectF(x, y, w, 16), Qt.AlignmentFlag.AlignLeft, "母线状态")

        vbus = float(self._data.get("vbus", 0.0) or 0.0)
        ibus = float(self._data.get("ibus", 0.0) or 0.0)
        power = float(self._data.get("power", 0.0) or (vbus * ibus))

        r = 28
        gap = (w - 3 * 2 * r) // 4
        gy = y + 28

        # Vbus: 0-80V, <24 欠压红, >60 过压红
        vbus_norm = min(1.0, vbus / 80.0)
        vbus_color = "#00d4ff"
        if vbus < 24.0 or vbus > 60.0:
            vbus_color = "#ff3030"
        self._draw_ring_gauge(p, x + gap + r, gy + r, r,
                              vbus_norm, vbus_color, "Vbus", f"{vbus:.1f}V")

        # Ibus: 0-30A
        ibus_norm = min(1.0, abs(ibus) / 30.0)
        ibus_color = "#00d4ff" if abs(ibus) < 25.0 else "#ff6b35"
        self._draw_ring_gauge(p, x + gap * 2 + 3 * r, gy + r, r,
                              ibus_norm, ibus_color, "Ibus", f"{ibus:+.2f}A")

        # Power: -500-500W
        power_norm = min(1.0, abs(power) / 500.0)
        power_color = _FLOW_POS if power >= 0 else _FLOW_NEG
        self._draw_ring_gauge(p, x + gap * 3 + 5 * r, gy + r, r,
                              power_norm, power_color, "Power", f"{power:+.0f}W")

    def _draw_ring_gauge(self, p: QPainter, cx: int, cy: int, r: int,
                          norm: float, color: str, label: str, value: str):
        """绘制单个圆环仪表。"""
        # 背景环
        p.setPen(QPen(QColor("#2a3040"), 4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r, r)
        # 进度环
        if norm > 0.01:
            for j in range(2, 0, -1):
                c = QColor(color); c.setAlpha(80 // j)
                p.setPen(QPen(c, 4 + j * 2))
                rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
                p.drawArc(rect, 90 * 16, int(-norm * 360 * 16))
            p.setPen(QPen(QColor(color), 4))
            rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
            p.drawArc(rect, 90 * 16, int(-norm * 360 * 16))
        # 中心数值
        p.setPen(QColor(color))
        p.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        p.drawText(QRectF(cx - r, cy - 6, 2 * r, 12),
                   Qt.AlignmentFlag.AlignCenter, value)
        # 标签
        p.setPen(QColor("#9FB6DD"))
        p.setFont(QFont("Consolas", 7))
        p.drawText(QRectF(cx - r, cy + r + 2, 2 * r, 12),
                   Qt.AlignmentFlag.AlignCenter, label)

    # ==================== 加速度仪表 ====================
    def _paint_acc_gauge(self, p: QPainter, x: int, y: int,
                          w: int, h: int):
        """绘制加速度数字仪表 (含方向箭头)。"""
        p.setPen(QColor("#7FD4FF"))
        p.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        p.drawText(QRectF(x, y, w, 16), Qt.AlignmentFlag.AlignLeft, "加速度")

        acc = self._acc
        # 归一化 [-2000, 2000] rad/s²
        acc_norm = max(-1.0, min(1.0, acc / 2000.0))
        color = _CUR_POS if acc >= 0 else _CUR_NEG

        # 水平条
        bx = x + 10
        by = y + 28
        bw = w - 20
        bh = 16
        mid_x = bx + bw // 2
        # 背景条
        p.setPen(QPen(QColor("#2a3040"), 1.0))
        p.setBrush(QBrush(QColor("#161a24")))
        p.drawRoundedRect(QRectF(bx, by, bw, bh), 3, 3)
        # 零线
        p.setPen(QPen(QColor("#3a4a5a"), 1.0))
        p.drawLine(mid_x, by - 2, mid_x, by + bh + 2)
        # 加速度条
        bar_w = int(abs(acc_norm) * bw / 2)
        if acc_norm >= 0:
            rect = QRectF(mid_x, by, bar_w, bh)
        else:
            rect = QRectF(mid_x - bar_w, by, bar_w, bh)
        for j in range(2, 0, -1):
            c = QColor(color); c.setAlpha(60 // j)
            p.setPen(QPen(c, 1 + j))
            p.setBrush(QBrush(c))
            p.drawRoundedRect(rect.adjusted(-j, -j, j, j), 3, 3)
        p.setPen(QPen(QColor(color), 1.0))
        p.setBrush(QBrush(QColor(color).darker(150)))
        p.drawRoundedRect(rect, 3, 3)

        # 数字读数
        p.setPen(QColor(color))
        p.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        p.drawText(QRectF(x, by + bh + 4, w, 20),
                   Qt.AlignmentFlag.AlignCenter, f"{acc:+.1f} rad/s²")

    # ==================== 能量流粒子 ====================
    def _paint_energy_flow(self, p: QPainter,
                            x1: int, y1: int, x2: int, y2: int):
        """绘制母线到电机的能量流粒子通道。"""
        power = float(self._data.get("power", 0.0) or
                      (float(self._data.get("vbus", 0.0)) *
                       float(self._data.get("ibus", 0.0))))
        # 通道路径 (贝塞尔曲线)
        path = QPainterPath()
        path.moveTo(x1, y1)
        ctrl_x = (x1 + x2) / 2
        ctrl_y = min(y1, y2) - 40
        path.quadTo(ctrl_x, ctrl_y, x2, y2)

        # 通道底色 (淡)
        p.setPen(QPen(QColor("#1a2030"), 2.0))
        p.drawPath(path)

        # 粒子
        color = _FLOW_POS if power >= 0 else _FLOW_NEG
        n = self.N_PARTICLES
        for i in range(n):
            # 粒子位置: phase + i/N
            t = (self._particle_phase + i / n) % 1.0
            # Power ≈ 0 时粒子停在中间
            if abs(power) < 1.0:
                t = 0.5 + (i / n - 0.5) * 0.3
            pt = path.pointAtPercent(t)
            # 粒子辉光
            for j in range(2, 0, -1):
                c = QColor(color); c.setAlpha(60 // j)
                p.setPen(QPen(c, 1 + j))
                p.setBrush(QBrush(c))
                p.drawEllipse(pt, 2 + j, 2 + j)
            # 粒子核心
            p.setPen(QPen(QColor(color), 1.0))
            p.setBrush(QBrush(QColor(color)))
            p.drawEllipse(pt, 2, 2)

        # 方向箭头 (通道中点)
        mid_t = 0.5
        mid_pt = path.pointAtPercent(mid_t)
        angle = math.atan2(
            path.slopeAtPercent(mid_t + 0.01) if False else (y2 - y1),
            (x2 - x1))
        if power < 0:
            angle += math.pi  # 反向
        p.save()
        p.translate(mid_pt)
        p.rotate(math.degrees(angle))
        p.setPen(QPen(QColor(color), 1.5))
        p.setBrush(QBrush(QColor(color)))
        p.drawPolygon([QPointF(-4, -3), QPointF(4, 0), QPointF(-4, 3)])
        p.restore()

    # ==================== 底部状态条 ====================
    def _paint_status_bar(self, p: QPainter, rect: QRectF):
        """绘制底部状态条: 模式/状态/位置/速度/加速度/Vbus/Ibus/Power。"""
        # 背景
        p.fillRect(rect, QColor("#0f1419"))
        p.setPen(QPen(QColor("#2a3040"), 1.0))
        p.drawLine(rect.topLeft(), rect.topRight())

        state = self._data.get("sys_state_name", "-")
        mode = self._data.get("control_mode_name", "-")
        pos = float(self._data.get("pos", 0.0) or 0.0)
        vel = float(self._data.get("vel", 0.0) or 0.0)
        vbus = float(self._data.get("vbus", 0.0) or 0.0)
        ibus = float(self._data.get("ibus", 0.0) or 0.0)
        power = float(self._data.get("power", 0.0) or (vbus * ibus))

        # 状态色
        main_color = _STATE_STYLE.get(state, _STATE_STYLE["IDLE"])[0]

        items = [
            (f"模式: {mode}", "#7FD4FF"),
            (f"状态: {state}", main_color),
            (f"位置: {pos:+.4f} rad", "#9FB6DD"),
            (f"速度: {vel:+.3f} rad/s", "#9FB6DD"),
            (f"加速度: {self._acc:+.1f} rad/s²", "#9FB6DD"),
            (f"Vbus: {vbus:.2f} V", "#9FB6DD"),
            (f"Ibus: {ibus:+.3f} A", "#9FB6DD"),
            (f"Power: {power:+.1f} W", _FLOW_POS if power >= 0 else _FLOW_NEG),
        ]
        # 等宽字体, 水平排列
        font = QFont("Consolas", 9)
        p.setFont(font)
        fm = p.fontMetrics()
        # 计算每项宽度
        widths = [fm.horizontalAdvance(text) for text, _ in items]
        total_w = sum(widths) + 20 * (len(items) - 1) + 20
        start_x = max(10, (rect.width() - total_w) / 2)
        x = rect.x() + start_x
        y = rect.y() + (rect.height() - fm.height()) / 2
        for (text, color), w in zip(items, widths):
            p.setPen(QColor(color))
            p.drawText(QRectF(x, y, w, fm.height()),
                       Qt.AlignmentFlag.AlignLeft, text)
            x += w + 20

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

    # ==================== 辅助: 粒子 Pixmap ====================
    def _build_particle_pixmaps(self):
        """预生成粒子 Pixmap (不同尺寸)。"""
        pixmaps = []
        for size in (4, 6, 8):
            pix = QPixmap(size * 2, size * 2)
            pix.fill(QColor(0, 0, 0, 0))
            p = QPainter(pix)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            for i in range(3, 0, -1):
                c = QColor("#00d4ff"); c.setAlpha(60 // i)
                p.setPen(QPen(c, 1 + i))
                p.setBrush(QBrush(c))
                p.drawEllipse(QPointF(size, size), size + i, size + i)
            p.setPen(QPen(QColor("#00d4ff"), 1.0))
            p.setBrush(QBrush(QColor("#00d4ff")))
            p.drawEllipse(QPointF(size, size), size, size)
            p.end()
            pixmaps.append(pix)
        return pixmaps

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
