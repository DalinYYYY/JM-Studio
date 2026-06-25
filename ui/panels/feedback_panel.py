"""实时反馈面板: FOC 电流环框图 + 电机实时旋转示意 + 系统参数分区卡片。

布局:
  上半左: _FocDiagram —— 自绘 FOC 电流闭环框图, 电气量贴附信号流环节。
  上半右: _MotorView —— 2D 电机截面旋转示意, 60fps 平滑动画(由速度航位推算,
          不受遥测帧率限制), 实时反映转子角度与转向。
  下半: 三相电流分列卡 + 母线/温度/位置·速度/状态·故障 分区卡片。

数据入口沿用原接口: update_feedback(fb) / update_state(top_fsm, run_state, ctrl_mode, enable)。
"""

import math

from PyQt6.QtCore import Qt, QRectF, QPointF, QTimer, QElapsedTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QPainterPath, QLinearGradient,
    QRadialGradient, QConicalGradient,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame, QSizePolicy,
    QPushButton,
)

from jmproto import top_fsm_name, run_state_name, cmd_name


_FONT_FAMILIES = ["Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", "sans-serif"]


def _mkfont(pt, bold=False):
    f = QFont()
    f.setFamilies(_FONT_FAMILIES)
    f.setPointSize(pt)
    f.setBold(bold)
    return f


class _T:
    BG_TOP = QColor("#232733")
    BG_BOTTOM = QColor("#191C24")
    BLOCK_TOP = QColor("#3A4150")
    BLOCK_BOTTOM = QColor("#2C313D")
    BLOCK_BORDER = QColor("#515872")
    BLOCK_TEXT = QColor("#E6E9F2")
    # 强调环节(电机/逆变器)用青绿描边
    HI_BORDER = QColor("#5FE6AC")
    EDGE = QColor("#7C8398")          # 正向通路
    EDGE_FB = QColor("#C9923F")       # 反馈通路(暖色, 区分电流闭环)
    TITLE = QColor("#F0F2F8")
    MUTED = QColor("#7C8294")
    VALUE = QColor("#7FD4FF")        # 数值青蓝
    VALUE_HOT = QColor("#FFB454")    # 力矩/电压等"动力"量用暖色
    UNIT = QColor("#8890A4")


class _Block:
    """框图中的一个功能块。"""
    __slots__ = ("key", "title", "x", "y", "w", "h", "hi")

    def __init__(self, key, title, x, y, w, h, hi=False):
        self.key = key
        self.title = title
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.hi = hi      # 是否强调描边


class _FocDiagram(QWidget):
    """FOC 电流闭环框图(自绘), 在对应环节标注实时电气量。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._vals = {}     # key -> 显示字符串
        self._edit = False
        self._drag_key = None
        self._corner_btns = []   # 浮于左下角的编辑/导出按钮

        # 归一化布局(0~1)。正向通路在上, 反馈通路在下(由拖拽编辑定稿)。
        self._blocks = [
            _Block("ref",     "电流指令\nid*/iq*", 0.005, 0.060, 0.120, 0.200),
            _Block("pi",      "电流环 PI",        0.155, 0.060, 0.130, 0.200),
            _Block("ipark",   "反Park\n(θ)",      0.315, 0.060, 0.110, 0.200),
            _Block("svpwm",   "SVPWM",            0.455, 0.060, 0.120, 0.200),
            _Block("inv",     "逆变器",           0.605, 0.060, 0.120, 0.200, hi=True),
            _Block("motor",   "PMSM\n电机",       0.775, 0.060, 0.135, 0.200, hi=True),
            _Block("clarke",  "Clarke",           0.780, 0.720, 0.120, 0.200),
            _Block("park",    "Park\n(θ)",        0.570, 0.720, 0.110, 0.200),
            _Block("idq",     "idq 反馈",         0.315, 0.715, 0.120, 0.200),
            _Block("enc",     "编码器\nθ / ω",    0.570, 0.425, 0.135, 0.200, hi=True),
        ]
        # 连线: (from, fromSide, to, toSide, kind)  kind: 'fwd' 正向 / 'fb' 反馈
        self._edges = [
            ("ref", "R", "pi", "L", "fwd"),
            ("pi", "R", "ipark", "L", "fwd"),
            ("ipark", "R", "svpwm", "L", "fwd"),
            ("svpwm", "R", "inv", "L", "fwd"),
            ("inv", "R", "motor", "L", "fwd"),
            # 反馈主链: 电机三相 ↓ Clarke → Park → idq → 闭环回 PI
            ("motor", "B", "clarke", "T", "fb"),
            ("clarke", "L", "park", "R", "fb"),
            ("park", "L", "idq", "R", "fb"),
            ("idq", "L", "pi", "B", "fb"),
            # 电机 → 编码器(机械耦合), 编码器 θ ↓ Park(正上方直接向下喂)
            ("motor", "B", "enc", "T", "fb"),
            ("enc", "B", "park", "T", "fb"),
        ]
        # 数值标注: (锚定块key, 角色文本, 值key, 颜色) —— 画在块下方/相关线旁
        self._annots = [
            ("inv", "Vbus", "vbus", _T.VALUE_HOT),
            ("motor", "τ", "torque", _T.VALUE_HOT),
            ("idq", "id/iq", "idq", _T.VALUE),
            ("enc", "θ/ω", "thw", _T.VALUE),
        ]

    def set_values(self, vals: dict):
        self._vals = dict(vals)
        self.update()

    def set_corner_buttons(self, *btns):
        """登记浮于框图左下角的按钮(编辑/导出), 由 resizeEvent 重定位。"""
        self._corner_btns = list(btns)
        self._reposition_corner()

    def _reposition_corner(self):
        margin, gap = 10, 6
        x = margin
        y = self.height() - margin
        for b in self._corner_btns:
            b.adjustSize()
            b.move(x, y - b.height())
            x += b.width() + gap

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reposition_corner()

    # ==================== 拖拽编辑模式 ====================
    # 开启后可用鼠标拖动方块调整布局, 实时显示归一化坐标; 拖好后 export_layout()
    # 打印/返回 JSON, 直接粘回 _blocks 即定稿。所见即所得, 无移植误差。
    def set_edit_mode(self, on: bool):
        self._edit = bool(on)
        self._drag_key = None
        self.setMouseTracking(self._edit)
        self.setCursor(Qt.CursorShape.OpenHandCursor if self._edit else Qt.CursorShape.ArrowCursor)
        self.update()

    def is_edit_mode(self) -> bool:
        return getattr(self, "_edit", False)

    def export_layout(self) -> str:
        """导出当前方块布局为可直接粘回 _blocks 的 Python 代码片段。"""
        lines = []
        for b in self._blocks:
            hi = ", hi=True" if b.hi else ""
            title = b.title.replace("\n", "\\n")
            lines.append(
                f'            _Block("{b.key}", "{title}", '
                f'{b.x:.3f}, {b.y:.3f}, {b.w:.3f}, {b.h:.3f}{hi}),')
        return "\n".join(lines)

    def _geom(self):
        ox, oy = 14.0, 34.0
        w = max(1.0, self.width() - 2 * ox)
        h = max(1.0, self.height() - oy - 14.0)
        return ox, oy, w, h

    def _block_at(self, pos):
        ox, oy, w, h = self._geom()
        for b in reversed(self._blocks):
            r = QRectF(ox + b.x * w, oy + b.y * h, b.w * w, b.h * h)
            if r.contains(pos):
                return b
        return None

    def mousePressEvent(self, e):
        if not self.is_edit_mode():
            return super().mousePressEvent(e)
        b = self._block_at(e.position())
        if b is not None:
            ox, oy, w, h = self._geom()
            self._drag_key = b.key
            self._drag_dx = e.position().x() - (ox + b.x * w)
            self._drag_dy = e.position().y() - (oy + b.y * h)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.update()

    def mouseMoveEvent(self, e):
        if not self.is_edit_mode() or self._drag_key is None:
            return
        ox, oy, w, h = self._geom()
        b = next((x for x in self._blocks if x.key == self._drag_key), None)
        if b is None:
            return
        nx = (e.position().x() - self._drag_dx - ox) / w
        ny = (e.position().y() - self._drag_dy - oy) / h
        # 网格吸附 0.005, 并夹在画布内
        b.x = max(0.0, min(1.0 - b.w, round(nx / 0.005) * 0.005))
        b.y = max(0.0, min(1.0 - b.h, round(ny / 0.005) * 0.005))
        self.update()

    def mouseReleaseEvent(self, e):
        if not self.is_edit_mode():
            return super().mouseReleaseEvent(e)
        self._drag_key = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()

    # ---- 锚点 ----
    @staticmethod
    def _port(r, side):
        if side == "L":
            return QPointF(r.left(), r.center().y())
        if side == "R":
            return QPointF(r.right(), r.center().y())
        if side == "T":
            return QPointF(r.center().x(), r.top())
        return QPointF(r.center().x(), r.bottom())

    def paintEvent(self, _evt):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        bg = QLinearGradient(0, 0, 0, self.height())
        bg.setColorAt(0.0, _T.BG_TOP)
        bg.setColorAt(1.0, _T.BG_BOTTOM)
        p.fillRect(self.rect(), QBrush(bg))

        ox, oy = 14.0, 34.0
        w = max(1.0, self.width() - 2 * ox)
        h = max(1.0, self.height() - oy - 14.0)

        # 标题
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_T.HI_BORDER))
        p.drawRoundedRect(QRectF(ox, 12, 3.5, 16), 1.5, 1.5)
        p.setPen(_T.TITLE)
        p.setFont(_mkfont(10, bold=True))
        p.drawText(QRectF(ox + 11, 8, w, 22),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "FOC 电流闭环 · 实时电气量")

        # 右上图例: ── 正向通路   ┄ 反馈/采样
        self._draw_legend(p, ox, w)

        rects = {b.key: QRectF(ox + b.x * w, oy + b.y * h, b.w * w, b.h * h)
                 for b in self._blocks}

        self._draw_edges(p, rects)
        for b in self._blocks:
            self._draw_block(p, b, rects[b.key])
        self._draw_annots(p, rects)

        if self._edit:
            self._draw_edit_overlay(p, ox, oy, w, h, rects)

        p.end()

    def _draw_edit_overlay(self, p, ox, oy, w, h, rects):
        # 网格
        p.setPen(QPen(QColor(255, 255, 255, 16), 1.0))
        for i in range(0, 21):
            x = ox + w * i / 20.0
            p.drawLine(QPointF(x, oy), QPointF(x, oy + h))
            y = oy + h * i / 20.0
            p.drawLine(QPointF(ox, y), QPointF(ox + w, y))
        # 每块标坐标
        p.setFont(_mkfont(7))
        for b in self._blocks:
            r = rects[b.key]
            p.setPen(QColor("#FFD080"))
            p.drawText(QRectF(r.x(), r.bottom() + 1, r.width(), 12),
                       Qt.AlignmentFlag.AlignHCenter,
                       f"{b.x:.3f},{b.y:.3f}")
        # 角标提示
        p.setPen(QColor("#FFD080"))
        p.setFont(_mkfont(8, bold=True))
        p.drawText(QRectF(ox, oy, w, 16), Qt.AlignmentFlag.AlignRight,
                   "编辑模式: 拖动方块, 完成后点[导出布局]")

    def _draw_legend(self, p, ox, w):
        p.setFont(_mkfont(8))
        fm = p.fontMetrics()
        items = [(_T.EDGE, False, "正向通路"), (_T.EDGE_FB, True, "反馈 / 采样")]
        x = ox + w
        for color, dashed, text in reversed(items):
            tw = fm.horizontalAdvance(text)
            x -= tw
            p.setPen(_T.MUTED)
            p.drawText(QRectF(x, 8, tw, 22),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            x -= 8
            pen = QPen(color, 1.8)
            if dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setDashPattern([4, 3])
            p.setPen(pen)
            p.drawLine(QPointF(x - 20, 19), QPointF(x, 19))
            x -= 30

    def _draw_edges(self, p, rects):
        fwd_pen = QPen(_T.EDGE, 1.8)
        fwd_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        fb_pen = QPen(_T.EDGE_FB, 1.6, Qt.PenStyle.DashLine)
        fb_pen.setDashPattern([5, 4])
        fb_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        for from_key, fs, to_key, ts, kind in self._edges:
            r1 = rects.get(from_key)
            r2 = rects.get(to_key)
            if r1 is None or r2 is None:
                continue
            a = self._port(r1, fs)
            b = self._port(r2, ts)
            pts = self._ortho(a, fs, b, ts)
            is_fb = kind == "fb"
            self._stroke(p, pts, fb_pen if is_fb else fwd_pen)
            self._arrow(p, pts[-2], pts[-1], _T.EDGE_FB if is_fb else _T.EDGE)

    @staticmethod
    def _ortho(a, fs, b, ts):
        horiz_from = fs in ("L", "R")
        horiz_to = ts in ("L", "R")
        if horiz_from and horiz_to:
            if abs(a.y() - b.y()) < 1:
                return [a, b]
            mx = (a.x() + b.x()) / 2.0
            return [a, QPointF(mx, a.y()), QPointF(mx, b.y()), b]
        if (not horiz_from) and (not horiz_to):
            if abs(a.x() - b.x()) < 1:
                return [a, b]
            my = (a.y() + b.y()) / 2.0
            return [a, QPointF(a.x(), my), QPointF(b.x(), my), b]
        if horiz_from:
            return [a, QPointF(b.x(), a.y()), b]
        return [a, QPointF(a.x(), b.y()), b]

    @staticmethod
    def _stroke(p, pts, pen, radius=7.0):
        import math
        path = QPainterPath()
        path.moveTo(pts[0])
        for i in range(1, len(pts) - 1):
            prev, cur, nxt = pts[i - 1], pts[i], pts[i + 1]
            l1 = math.hypot(cur.x() - prev.x(), cur.y() - prev.y())
            l2 = math.hypot(nxt.x() - cur.x(), nxt.y() - cur.y())
            if l1 < 1e-3 or l2 < 1e-3:
                continue
            r = min(radius, l1 / 2, l2 / 2)
            p1 = QPointF(cur.x() + (prev.x() - cur.x()) / l1 * r,
                         cur.y() + (prev.y() - cur.y()) / l1 * r)
            p2 = QPointF(cur.x() + (nxt.x() - cur.x()) / l2 * r,
                         cur.y() + (nxt.y() - cur.y()) / l2 * r)
            path.lineTo(p1)
            path.quadTo(cur, p2)
        path.lineTo(pts[-1])
        p.strokePath(path, pen)

    @staticmethod
    def _arrow(p, a, b, color=None):
        import math
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        s = 7.0
        p1 = QPointF(b.x() - s * math.cos(ang - math.pi / 7),
                     b.y() - s * math.sin(ang - math.pi / 7))
        p2 = QPointF(b.x() - s * math.cos(ang + math.pi / 7),
                     b.y() - s * math.sin(ang + math.pi / 7))
        path = QPainterPath()
        path.moveTo(b)
        path.lineTo(p1)
        path.lineTo(p2)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.fillPath(path, QBrush(color if color is not None else _T.EDGE))

    def _draw_block(self, p, b, rect):
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0.0, _T.BLOCK_TOP)
        grad.setColorAt(1.0, _T.BLOCK_BOTTOM)
        border = _T.HI_BORDER if b.hi else _T.BLOCK_BORDER
        p.setPen(QPen(border, 1.8 if b.hi else 1.3))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(rect, 7, 7)

        # 顶部高光
        p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
        p.drawLine(QPointF(rect.left() + 7, rect.top() + 2),
                   QPointF(rect.right() - 7, rect.top() + 2))

        p.setPen(_T.BLOCK_TEXT)
        p.setFont(_mkfont(8, bold=b.hi))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, b.title)

    def _draw_annots(self, p, rects):
        """在相关块下方画 角色=值 的标注胶囊。"""
        for key, role, vkey, color in self._annots:
            r = rects.get(key)
            if r is None:
                continue
            val = self._vals.get(vkey, "--")
            text = f"{role} = {val}"
            self._chip(p, QPointF(r.center().x(), r.bottom() + 14), text, color)

        # 三相电流贴在 电机->Clarke 的反馈线竖直段旁(电机正下方), 三相分行显示
        rm = rects.get("motor")
        rc = rects.get("clarke")
        if rm is not None and rc is not None:
            lines = self._vals.get("iabc_lines", ["ia --", "ib --", "ic --"])
            mid = QPointF(rm.center().x(), rm.bottom() + (rc.top() - rm.bottom()) * 0.5)
            self._chip_multiline(p, mid, lines, _T.VALUE)

    def _chip_multiline(self, p, center, lines, color):
        """多行标注胶囊: 每行一个相电流, 竖排不挤。"""
        p.setFont(_mkfont(8, bold=True))
        fm = p.fontMetrics()
        tw = max(fm.horizontalAdvance(s) for s in lines)
        lh = fm.height()
        pad_x, pad_y = 8, 5
        w = tw + 2 * pad_x
        h = lh * len(lines) + 2 * pad_y
        rect = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 23, 30, 225))
        p.drawRoundedRect(rect, 7, 7)
        p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 90), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 7, 7)
        p.setPen(color)
        for i, s in enumerate(lines):
            p.drawText(QRectF(rect.x() + pad_x, rect.y() + pad_y + i * lh, tw, lh),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, s)

    def _chip(self, p, center, text, color):
        p.setFont(_mkfont(8, bold=True))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        pad = 6
        rect = QRectF(center.x() - tw / 2 - pad, center.y() - th / 2 - 1,
                      tw + 2 * pad, th + 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 23, 30, 220))
        p.drawRoundedRect(rect, th / 2, th / 2)
        p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 90), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, th / 2, th / 2)
        p.setPen(color)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


class _ParamCard(QFrame):
    """系统参数分区卡片: 标题 + 若干 (名:值 单位) 行。"""

    def __init__(self, title, rows, parent=None):
        """rows: list[(label, attr, fmt, unit)]"""
        super().__init__(parent)
        self._rows = rows
        self._vals = {}
        self.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #2A2F3D, stop:1 #232733);
                border: 1px solid #383E4E; border-radius: 8px;
            }
            QLabel { border: none; }
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(5)

        head = QLabel(title)
        head.setStyleSheet("color:#9FB6DD; font-weight:bold; border:none;")
        head.setFont(_mkfont(9, bold=True))
        lay.addWidget(head)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background:#383E4E; border:none;")
        lay.addWidget(line)

        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self._val_labels = {}
        for i, (label, attr, _fmt, unit) in enumerate(rows):
            name = QLabel(label)
            name.setStyleSheet("color:#AEB4C4; border:none;")
            name.setFont(_mkfont(9))
            val = QLabel("--")
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val.setStyleSheet(
                "color:#7FD4FF; font-family:Consolas,'Microsoft YaHei',monospace; "
                "font-size:13px; font-weight:bold; border:none;")
            u = QLabel(unit)
            u.setStyleSheet("color:#8890A4; border:none;")
            u.setFont(_mkfont(8))
            u.setFixedWidth(46)
            self._val_labels[attr] = val
            grid.addWidget(name, i, 0)
            grid.addWidget(val, i, 1)
            grid.addWidget(u, i, 2)
        lay.addLayout(grid)
        lay.addStretch()

    def update_values(self, fb):
        for label, attr, fmt, unit in self._rows:
            lbl = self._val_labels.get(attr)
            if lbl is None:
                continue
            try:
                lbl.setText(fmt.format(getattr(fb, attr)))
            except Exception:
                pass

    def set_text(self, attr, text, color=None):
        lbl = self._val_labels.get(attr)
        if lbl is None:
            return
        lbl.setText(text)
        if color:
            lbl.setStyleSheet(
                f"color:{color}; font-family:Consolas,'Microsoft YaHei',monospace; "
                f"font-size:13px; font-weight:bold; border:none;")


class _MotorView(QWidget):
    """2D 电机截面旋转示意。

    平滑动画核心: 内部维护"显示角度" _disp_th, 每帧(60fps)按最近一次上报的机械
    速度 ω 做航位推算递推, 因此即便遥测帧率很低, 转子也能匀速顺滑旋转; 每次收到新
    的真实角度时, 把显示角度柔和地拉向真实值(避免跳变)。
    """

    DEFAULT_POLE_PAIRS = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(210)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._pole_pairs = self.DEFAULT_POLE_PAIRS
        self._stator_slots = 12
        self._disp_th = 0.0
        self._tgt_th = 0.0
        self._omega = 0.0
        self._iq = 0.0
        self._enabled = False
        self._params = {}        # 电机本体参数(读回后填充)
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last_ms = self._clock.elapsed()
        self._timer = QTimer(self)
        self._timer.setInterval(16)   # ~60fps
        self._timer.timeout.connect(self._tick)

    # 仅在可见时跑动画, 省 CPU
    def showEvent(self, e):
        super().showEvent(e)
        self._last_ms = self._clock.elapsed()
        self._timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def set_feedback(self, single_rad, omega, iq, enabled):
        self._tgt_th = float(single_rad)
        self._omega = float(omega)
        self._iq = float(iq)
        self._enabled = bool(enabled)

    def set_motor_params(self, params: dict):
        """电机本体参数(r/ld/lq/flux/kt/ke/pole_pairs)。极对数为0或缺失时用默认值。"""
        self._params = dict(params)
        pp = int(params.get("pole_pairs", 0) or 0)
        self._pole_pairs = pp if pp > 0 else self.DEFAULT_POLE_PAIRS
        self.update()

    def _tick(self):
        now = self._clock.elapsed()
        dt = (now - self._last_ms) / 1000.0
        self._last_ms = now
        if dt <= 0:
            return
        self._disp_th += self._omega * dt
        err = math.atan2(math.sin(self._tgt_th - self._disp_th),
                         math.cos(self._tgt_th - self._disp_th))
        self._disp_th += err * min(1.0, dt * 3.0) * 0.25
        self.update()

    def paintEvent(self, _evt):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg = QLinearGradient(0, 0, 0, self.height())
        bg.setColorAt(0.0, _T.BG_TOP)
        bg.setColorAt(1.0, _T.BG_BOTTOM)
        p.fillRect(self.rect(), QBrush(bg))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_T.HI_BORDER))
        p.drawRoundedRect(QRectF(12, 12, 3.5, 16), 1.5, 1.5)
        p.setPen(_T.TITLE)
        p.setFont(_mkfont(10, bold=True))
        p.drawText(QRectF(23, 8, self.width() - 30, 22),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "转子位置")

        cx = self.width() / 2.0
        params_h = 92.0                      # 底部参数区高度
        avail_top = 34.0
        avail_h = self.height() - avail_top - params_h
        cy = avail_top + avail_h / 2.0
        R = min(self.width() - 28, avail_h) / 2.0 - 8
        if R < 18:
            p.end()
            return

        self._draw_stator(p, cx, cy, R)
        self._draw_rotor(p, cx, cy, R * 0.70, self._disp_th)
        self._draw_pointer(p, cx, cy, R, self._disp_th)
        self._draw_readout(p, cx, cy, R)
        self._draw_params(p, self.height() - params_h)
        p.end()

    def _draw_stator(self, p, cx, cy, R):
        # 薄外壳: 外圆与内孔间只留一道窄环
        p.setPen(QPen(QColor("#444B5E"), 1.6))
        grad = QRadialGradient(cx, cy, R)
        grad.setColorAt(0.90, QColor("#2A2F3D"))
        grad.setColorAt(1.0, QColor("#3A4256"))
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(cx, cy), R, R)
        p.setPen(QPen(QColor("#3A4152"), 1.2))
        p.setBrush(QColor("#20242E"))
        p.drawEllipse(QPointF(cx, cy), R * 0.86, R * 0.86)
        # 定子齿(指向圆心)
        p.setBrush(QColor("#333A49"))
        p.setPen(QPen(QColor("#475064"), 1.0))
        rt_out = R * 0.86
        rt_in = R * 0.64
        for i in range(self._stator_slots):
            a = 2 * math.pi * i / self._stator_slots
            self._draw_tooth(p, cx, cy, a, rt_in, rt_out, 0.14)

    def _draw_tooth(self, p, cx, cy, a, r_in, r_out, half_w):
        def pt(r, da):
            return QPointF(cx + r * math.cos(a + da), cy + r * math.sin(a + da))
        path = QPainterPath()
        path.moveTo(pt(r_out, -half_w))
        path.lineTo(pt(r_out, half_w))
        path.lineTo(pt(r_in, half_w * 0.6))
        path.lineTo(pt(r_in, -half_w * 0.6))
        path.closeSubpath()
        p.drawPath(path)

    def _draw_rotor(self, p, cx, cy, r, th):
        # 转子盘
        grad = QRadialGradient(cx, cy, r)
        grad.setColorAt(0.0, QColor("#3C4356"))
        grad.setColorAt(1.0, QColor("#2A303E"))
        p.setPen(QPen(QColor("#4A5167"), 1.5))
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(cx, cy), r, r)

        # 磁极: 用扇形环交替 N(红)/S(蓝), 不重叠, 随角度旋转
        n = self._pole_pairs * 2
        outer = QRectF(cx - r * 0.92, cy - r * 0.92, r * 1.84, r * 1.84)
        seg = 360.0 / n
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(n):
            is_n = (i % 2 == 0)
            col = QColor("#C8554F") if is_n else QColor("#4F7FD0")
            start = math.degrees(th) + i * seg + 1.5
            path = QPainterPath()
            path.moveTo(QPointF(cx, cy))
            path.arcTo(outer, -start, -(seg - 3.0))   # Qt 角度逆时针为正, 取负顺时针
            path.closeSubpath()
            p.setBrush(col)
            p.drawPath(path)

        # 内圈遮罩(留出磁极环宽度), 形成磁极在外环的观感
        grad2 = QRadialGradient(cx, cy, r * 0.58)
        grad2.setColorAt(0.0, QColor("#363D4E"))
        grad2.setColorAt(1.0, QColor("#2C3340"))
        p.setBrush(QBrush(grad2))
        p.drawEllipse(QPointF(cx, cy), r * 0.58, r * 0.58)

        # d 轴标记(小三角, 指示转子磁链方向)
        dax = cx + r * 0.5 * math.cos(th)
        day = cy + r * 0.5 * math.sin(th)
        p.setBrush(QColor("#E0C060"))
        p.setPen(Qt.PenStyle.NoPen)
        tri = QPainterPath()
        for k in range(3):
            aa = th + k * 2 * math.pi / 3
            pt = QPointF(dax + 5 * math.cos(aa), day + 5 * math.sin(aa))
            if k == 0:
                tri.moveTo(pt)
            else:
                tri.lineTo(pt)
        tri.closeSubpath()
        p.drawPath(tri)

        # 轴心
        p.setBrush(QColor("#11141B"))
        p.setPen(QPen(QColor("#566076"), 1.2))
        p.drawEllipse(QPointF(cx, cy), r * 0.18, r * 0.18)

    def _draw_pointer(self, p, cx, cy, R, th):
        col = _T.HI_BORDER if self._enabled else QColor("#6A7288")
        ex = cx + R * 0.66 * math.cos(th)
        ey = cy + R * 0.66 * math.sin(th)
        pen = QPen(col, 2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(cx, cy), QPointF(ex, ey))
        ang = th
        s = 9.0
        a1 = QPointF(ex - s * math.cos(ang - math.pi / 6), ey - s * math.sin(ang - math.pi / 6))
        a2 = QPointF(ex - s * math.cos(ang + math.pi / 6), ey - s * math.sin(ang + math.pi / 6))
        path = QPainterPath()
        path.moveTo(QPointF(ex, ey))
        path.lineTo(a1)
        path.lineTo(a2)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.fillPath(path, QBrush(col))

    def _draw_readout(self, p, cx, cy, R):
        deg = math.degrees(self._disp_th) % 360.0
        rpm = self._omega * 60.0 / (2 * math.pi)
        p.setPen(_T.MUTED)
        p.setFont(_mkfont(8))
        p.drawText(QRectF(cx - R, cy + R - 4, 2 * R, 16),
                   Qt.AlignmentFlag.AlignCenter,
                   f"θ {deg:5.1f}°   ω {rpm:6.1f} rpm")

    # 软件用到的关键电机参数(读回 None 时显示 --)
    _PARAM_DEFS = [
        ("r", "R", "{:.3f}", "Ω"),
        ("ld", "Ld", "{:.3f}", "mH"),
        ("lq", "Lq", "{:.3f}", "mH"),
        ("flux", "ψ", "{:.4f}", "Wb"),
        ("kt", "Kt", "{:.3f}", "Nm/A"),
        ("pole_pairs", "Pn", "{:d}", ""),
    ]

    def _draw_params(self, p, y0):
        """转子图下方: 两列网格列出关键电机参数。"""
        x0 = 14.0
        w = self.width() - 28.0
        # 分隔线 + 小标题
        p.setPen(QPen(QColor("#383E4E"), 1))
        p.drawLine(QPointF(x0, y0), QPointF(x0 + w, y0))
        p.setPen(_T.MUTED)
        p.setFont(_mkfont(8, bold=True))
        p.drawText(QRectF(x0, y0 + 2, w, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "电机参数")

        cols = 2
        col_w = w / cols
        row_h = 19.0
        gy = y0 + 18
        for i, (key, label, fmt, unit) in enumerate(self._PARAM_DEFS):
            r = i // cols
            c = i % cols
            cell_x = x0 + c * col_w
            cell_y = gy + r * row_h
            # 取值: ld/lq 由 H 换算 mH
            v = self._params.get(key, None)
            if v is None:
                vtxt = "--"
            else:
                try:
                    if key in ("ld", "lq"):
                        vtxt = fmt.format(float(v) * 1000.0)
                    elif key == "pole_pairs":
                        vtxt = fmt.format(int(v))
                    else:
                        vtxt = fmt.format(float(v))
                except Exception:
                    vtxt = str(v)
            p.setPen(QColor("#9298AC"))
            p.setFont(_mkfont(8))
            p.drawText(QRectF(cell_x, cell_y, col_w * 0.34, row_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            p.setPen(_T.VALUE)
            p.setFont(_mkfont(9, bold=True))
            vtext = f"{vtxt} {unit}".strip()
            p.drawText(QRectF(cell_x + col_w * 0.30, cell_y, col_w * 0.66 - 6, row_h),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, vtext)


class FeedbackPanel(QWidget):
    """实时反馈: 上 FOC 框图 + 下系统参数分区卡片。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("QWidget#fbRoot { background:#16181F; }")
        self.setObjectName("fbRoot")
        self._enabled = False
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # 上: FOC 框图(左, 占主) + 电机旋转示意(右)
        top = QHBoxLayout()
        top.setSpacing(8)
        self._foc = _FocDiagram()
        self._motor = _MotorView()
        self._foc.setStyleSheet("border:1px solid #383E4E; border-radius:8px;")
        self._motor.setStyleSheet("border:1px solid #383E4E; border-radius:8px;")
        top.addWidget(self._foc, 5)
        top.addWidget(self._motor, 2)
        root.addLayout(top, 3)

        # 布局编辑/导出按钮: 作为 FOC 框图的子控件, 浮于其左下角
        _btn_css = ("QPushButton{background:rgba(42,46,58,0.85);color:#C8CCD8;"
                    "border:1px solid #444A5A;border-radius:4px;padding:0 8px;font-size:11px;}"
                    "QPushButton:checked{background:#C8963C;color:#1a1a1a;font-weight:bold;}")
        self._btn_edit = QPushButton("布局编辑: 关", self._foc)
        self._btn_edit.setCheckable(True)
        self._btn_edit.setFixedHeight(22)
        self._btn_edit.setStyleSheet(_btn_css)
        self._btn_edit.toggled.connect(self._on_edit_toggled)
        self._btn_export = QPushButton("导出布局", self._foc)
        self._btn_export.setFixedHeight(22)
        self._btn_export.setStyleSheet(_btn_css)
        self._btn_export.clicked.connect(self._on_export_layout)
        self._foc.set_corner_buttons(self._btn_edit, self._btn_export)

        # 下: 系统参数分区卡片(母线+温度合并, 位置/速度, 状态/故障)
        cards = QHBoxLayout()
        cards.setSpacing(8)

        self._card_bus = _ParamCard("母线 / 功率 / 温度", [
            ("母线电压 Vbus", "vbus", "{:.2f}", "V"),
            ("母线电流 Ibus", "ibus", "{:.3f}", "A"),
            ("功率 P", "power", "{:.2f}", "W"),
            ("温度 FET", "temp_fet", "{:.1f}", "°C"),
            ("温度 电机", "temp_motor", "{:.1f}", "°C"),
        ])
        self._card_motion = _ParamCard("位置 / 速度", [
            ("位置 pos", "pos", "{:.4f}", "rad"),
            ("速度 vel", "vel", "{:.4f}", "rad/s"),
            ("多圈计数", "multiturn", "{}", ""),
            ("单圈位置", "single", "{:.4f}", "rad"),
        ])
        self._card_state = _ParamCard("状态 / 故障", [
            ("顶层状态", "_fsm", "{}", ""),
            ("运行子态", "_run", "{}", ""),
            ("控制模式", "_ctrl", "{}", ""),
            ("使能", "_en", "{}", ""),
            ("故障掩码", "_fault", "{}", ""),
            ("警告掩码", "_warn", "{}", ""),
        ])

        cards.addWidget(self._card_bus, 1)
        cards.addWidget(self._card_motion, 1)
        cards.addWidget(self._card_state, 1)
        root.addLayout(cards, 2)

    # ---- 布局编辑(调试: 拖动 FOC 方块) ----
    def _on_edit_toggled(self, on: bool):
        self._foc.set_edit_mode(on)
        self._btn_edit.setText("布局编辑: 开" if on else "布局编辑: 关")

    def _on_export_layout(self):
        code = self._foc.export_layout()
        # 写文件(与本模块同目录), 方便直接取用
        import os
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "foc_layout_export.txt")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(code + "\n")
        except Exception:
            out_path = "(写入失败)"
        # 复制到剪贴板
        try:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(code)
        except Exception:
            pass
        print("\n# ==== FOC 布局导出 (粘回 _FocDiagram._blocks) ====\n" + code + "\n")
        # 弹窗显示, 可全选复制
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QPlainTextEdit, QLabel, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("FOC 布局导出")
        dlg.resize(560, 360)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"已复制到剪贴板, 并保存到:\n{out_path}\n粘回 _FocDiagram._blocks 即定稿:"))
        edit = QPlainTextEdit(code)
        edit.setReadOnly(True)
        edit.setStyleSheet("font-family:Consolas,monospace;font-size:12px;")
        lay.addWidget(edit)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, dlg)
        bb.accepted.connect(dlg.accept)
        lay.addWidget(bb)
        dlg.exec()

    # ---- 数据入口(接口保持不变) ----
    def update_feedback(self, fb):
        self._card_bus.update_values(fb)
        self._card_motion.update_values(fb)

        # 电机旋转示意(单圈角 + 速度 + iq)
        self._motor.set_feedback(
            getattr(fb, "single", 0.0), getattr(fb, "vel", 0.0),
            getattr(fb, "iq", 0.0), self._enabled)

        # FOC 框图电气量
        def f(attr, fmt):
            try:
                return fmt.format(getattr(fb, attr))
            except Exception:
                return "--"
        self._foc.set_values({
            "vbus": f("vbus", "{:.1f} V"),
            "torque": f("torque", "{:.3f} Nm"),
            "idq": f"{f('id','{:.2f}')}/{f('iq','{:.2f}')} A",
            "iabc_lines": [           # 三相分行, 不挤一行
                f"ia {f('ia','{:+.2f}')} A",
                f"ib {f('ib','{:+.2f}')} A",
                f"ic {f('ic','{:+.2f}')} A",
            ],
            "thw": f"{f('single','{:.3f}')}rad / {f('vel','{:.2f}')}",
        })

        # 故障/警告
        self._card_state.set_text(
            "_fault", f"0x{fb.fault_mask:04X}",
            "#FF6B6B" if fb.fault_mask else "#7FD4FF")
        self._card_state.set_text(
            "_warn", f"0x{fb.warn_mask:04X}",
            "#FFB454" if fb.warn_mask else "#7FD4FF")

    def set_motor_params(self, params: dict):
        """电机本体参数(r/ld/lq/flux/kt/ke/pole_pairs), 转发到转子示意图下方显示。"""
        self._motor.set_motor_params(params)

    def update_state(self, top_fsm, run_state, ctrl_mode, enable):
        self._enabled = bool(enable)
        is_fault = top_fsm in (1, 2)
        self._card_state.set_text("_fsm", top_fsm_name(top_fsm),
                                  "#FF6B6B" if is_fault else "#5FE6AC")
        self._card_state.set_text("_run", run_state_name(run_state))
        self._card_state.set_text("_ctrl", cmd_name(ctrl_mode))
        self._card_state.set_text("_en", "ON" if enable else "OFF",
                                  "#5FE6AC" if enable else "#8890A4")
