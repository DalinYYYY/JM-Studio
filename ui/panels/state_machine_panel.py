"""状态机可视化面板.

自绘两张状态机逻辑图(QPainter), 按下位机上报的实时状态高亮当前所处节点:
  - 上图: 顶层主状态机 (top_fsm)   —— 初始化/待机/就绪/工作模式容器/故障/急停
  - 下图: 运行模式状态机 (run_state + ctrl_mode) —— 仅 top_fsm==RUN 时激活

数据来源: JmClient.state_updated(top_fsm, run_state, ctrl_mode, enable)。
面板自带周期轮询: 可见且链路已连接时, 周期发 READ_STATE(0xC1) 主动拉状态,
与遥测订阅(0xCA 带 STATE 位)互补——无论是否开遥测都能刷新当前状态高亮。

节点与连线均为数据驱动(_nodes / _edges), 调整布局只改数据表, 不动绘制逻辑。
状态→节点 的映射严格对齐固件 state_define.h(见 jmproto.cmd_def.TopFsm / RunState)。
"""

import math

from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QPainterPath, QLinearGradient,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QSpinBox, QFrame,
)

from jmproto import JmCmd, TopFsm, RunState, top_fsm_name, run_state_name


# 中文友好字体族(Windows 优先 YaHei, 回退通用无衬线)
_FONT_FAMILIES = ["Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", "sans-serif"]


def _mkfont(point_size, bold=False):
    f = QFont()
    f.setFamilies(_FONT_FAMILIES)
    f.setPointSize(point_size)
    f.setBold(bold)
    return f


# ============================ 主题配色 ============================
class _Theme:
    # 画布背景渐变(深→更深)
    BG_TOP = QColor("#232733")
    BG_BOTTOM = QColor("#191C24")

    # 普通节点(中性蓝灰渐变)
    NODE_TOP = QColor("#3A3F4E")
    NODE_BOTTOM = QColor("#2C303C")
    NODE_BORDER = QColor("#4D5365")
    NODE_TEXT = QColor("#E4E7EF")

    # 激活节点(青绿)
    ACTIVE_TOP = QColor("#2FB37A")
    ACTIVE_BOTTOM = QColor("#1E8E63")
    ACTIVE_BORDER = QColor("#5FE6AC")
    ACTIVE_GLOW = QColor(95, 230, 172)

    # 故障/急停节点(红)
    FAULT_TOP = QColor("#9E4B4B")
    FAULT_BOTTOM = QColor("#7E3838")
    FAULT_BORDER = QColor("#C06A6A")

    # 激活的故障节点(亮红)
    ACTIVE_FAULT_TOP = QColor("#E0524F")
    ACTIVE_FAULT_BOTTOM = QColor("#C13B38")
    ACTIVE_FAULT_BORDER = QColor("#FF8C88")
    ACTIVE_FAULT_GLOW = QColor(255, 110, 105)

    # 容器
    GROUP_FILL = QColor(90, 130, 200, 22)
    GROUP_FILL_ACTIVE = QColor(95, 230, 172, 26)
    GROUP_BORDER = QColor("#5A7BB5")
    GROUP_BORDER_ACTIVE = QColor("#5FE6AC")
    GROUP_TITLE = QColor("#9FB6DD")

    # 连线
    EDGE = QColor("#6E7488")
    EDGE_LABEL = QColor("#9298AC")

    # 文本
    TITLE = QColor("#F0F2F8")
    ACCENT = QColor("#5FE6AC")
    MUTED = QColor("#7C8294")


class _Node:
    """一个状态节点(逻辑坐标, 0~1 归一化后乘画布尺寸)。

    kind: 'state' 普通态 / 'fault' 故障类态 / 'group' 容器框 / 'mode' 运行模式块
    key:  与 top_fsm/run_state/ctrl_mode 匹配的标识(见各图 apply_state)。
    """

    __slots__ = ("key", "label", "x", "y", "w", "h", "kind")

    def __init__(self, key, label, x, y, w, h, kind="state"):
        self.key = key
        self.label = label
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.kind = kind


class _DiagramView(QWidget):
    """单张状态机图的自绘视图基类。子类提供 nodes/edges/标题/激活判定。"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self._title = title
        self._nodes = []           # list[_Node]
        self._edges = []           # list[(from_key, to_key, label)]
        self._active_keys = set()  # 当前高亮的节点 key
        self._note = ""
        self.setMinimumHeight(300)
        self.setMinimumWidth(420)

    def set_active(self, keys):
        new = set(keys)
        if new != self._active_keys:
            self._active_keys = new
            self.update()

    def set_note(self, text):
        if text != self._note:
            self._note = text
            self.update()

    # ---- 坐标换算: 逻辑(0~1) -> 像素, 留边距 ----
    def _rect_of(self, node, ox, oy, w, h):
        return QRectF(ox + node.x * w, oy + node.y * h, node.w * w, node.h * h)

    def _node_by_key(self, key):
        for n in self._nodes:
            if n.key == key:
                return n
        return None

    # ------------------------------------------------------------------
    def paintEvent(self, _evt):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        # 背景渐变
        bg = QLinearGradient(0, 0, 0, self.height())
        bg.setColorAt(0.0, _Theme.BG_TOP)
        bg.setColorAt(1.0, _Theme.BG_BOTTOM)
        p.fillRect(self.rect(), QBrush(bg))

        # 细点阵网格(技术感, 极低对比)
        self._draw_dot_grid(p)

        ox, oy = 18.0, 44.0
        w = max(1.0, self.width() - 2 * ox)
        h = max(1.0, self.height() - oy - 18.0)

        self._draw_header(p, ox, w)

        rects = {n.key: self._rect_of(n, ox, oy, w, h) for n in self._nodes}

        # 容器先画(在底层), 连线其次, 普通节点最后
        for n in self._nodes:
            if n.kind == "group":
                self._draw_group(p, n, rects[n.key])
        self._draw_edges(p, rects)
        for n in self._nodes:
            if n.kind != "group":
                self._draw_node(p, n, rects[n.key])

        p.end()

    def _draw_dot_grid(self, p, step=26):
        p.save()
        p.setPen(QPen(QColor(255, 255, 255, 10), 1.0))
        y = 50
        while y < self.height():
            x = 20
            while x < self.width():
                p.drawPoint(QPointF(x, y))
                x += step
            y += step
        p.restore()

    def _draw_header(self, p, ox, w):
        # 左侧强调竖条
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_Theme.ACCENT))
        p.drawRoundedRect(QRectF(ox, 16, 3.5, 18), 1.5, 1.5)

        p.setPen(_Theme.TITLE)
        f = _mkfont(11, bold=True)
        p.setFont(f)
        p.drawText(QRectF(ox + 12, 12, w - 12, 26),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._title)

        # 右上角图例: ● 当前  ● 故障/急停
        self._draw_legend(p, ox, w)

        if self._note:
            p.setPen(_Theme.MUTED)
            fn = _mkfont(8)
            p.setFont(fn)
            p.drawText(QRectF(ox, 30, w, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._note)

    def _draw_legend(self, p, ox, w):
        fl = _mkfont(8)
        p.setFont(fl)
        fm = p.fontMetrics()
        items = [(_Theme.ACTIVE_BORDER, "当前"), (_Theme.ACTIVE_FAULT_BORDER, "故障/急停")]
        # 从右往左排
        x = ox + w
        for color, text in reversed(items):
            tw = fm.horizontalAdvance(text)
            x -= tw
            p.setPen(_Theme.MUTED)
            p.drawText(QRectF(x, 12, tw, 22),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            x -= 8
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawEllipse(QPointF(x, 23), 4, 4)
            x -= 16

    # ---- 连线: 正交布线 + 圆角转角 ----
    def _draw_edges(self, p, rects):
        pen = QPen(_Theme.EDGE, 1.7)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        fe = _mkfont(8)
        for edge in self._edges:
            from_key, to_key, label = edge[0], edge[1], edge[2]
            spec = edge[3] if len(edge) > 3 else None
            r1 = rects.get(from_key)
            r2 = rects.get(to_key)
            if r1 is None or r2 is None:
                continue
            pts = _route(r1, r2, spec)
            if len(pts) < 2:
                continue
            self._draw_rounded_polyline(p, pts, pen)
            self._draw_arrow_head(p, pts[-2], pts[-1])
            if label:
                p.setPen(_Theme.EDGE_LABEL)
                p.setFont(fe)
                lp = self._label_anchor(pts, spec)
                self._draw_edge_label(p, lp, label)

    @staticmethod
    def _label_anchor(pts, spec):
        """标签锚点: 默认取最长段中点, 可由 spec['label_seg'] 指定第几段。"""
        if spec and 'label_at' in spec:
            return spec['label_at']
        # 找最长的一段
        best_i, best_len = 1, -1.0
        for i in range(1, len(pts)):
            a, b = pts[i - 1], pts[i]
            d = abs(b.x() - a.x()) + abs(b.y() - a.y())
            if d > best_len:
                best_len, best_i = d, i
        a, b = pts[best_i - 1], pts[best_i]
        return QPointF((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0)

    def _draw_edge_label(self, p, center, text):
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        pad = 4
        rect = QRectF(center.x() - tw / 2 - pad, center.y() - th / 2 - 1,
                      tw + 2 * pad, th + 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(25, 28, 36, 210))
        p.drawRoundedRect(rect, 4, 4)
        p.setPen(_Theme.EDGE_LABEL)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    @staticmethod
    def _draw_rounded_polyline(p, pts, pen, radius=9.0):
        path = QPainterPath()
        path.moveTo(pts[0])
        for i in range(1, len(pts) - 1):
            prev, cur, nxt = pts[i - 1], pts[i], pts[i + 1]
            len1 = math.hypot(cur.x() - prev.x(), cur.y() - prev.y())
            len2 = math.hypot(nxt.x() - cur.x(), nxt.y() - cur.y())
            if len1 < 1e-3 or len2 < 1e-3:
                continue
            r = min(radius, len1 / 2.0, len2 / 2.0)
            p1 = QPointF(cur.x() + (prev.x() - cur.x()) / len1 * r,
                         cur.y() + (prev.y() - cur.y()) / len1 * r)
            p2 = QPointF(cur.x() + (nxt.x() - cur.x()) / len2 * r,
                         cur.y() + (nxt.y() - cur.y()) / len2 * r)
            path.lineTo(p1)
            path.quadTo(cur, p2)
        path.lineTo(pts[-1])
        p.strokePath(path, pen)

    @staticmethod
    def _draw_arrow_head(p, a, b):
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        size = 8.0
        p1 = QPointF(b.x() - size * math.cos(ang - math.pi / 7),
                     b.y() - size * math.sin(ang - math.pi / 7))
        p2 = QPointF(b.x() - size * math.cos(ang + math.pi / 7),
                     b.y() - size * math.sin(ang + math.pi / 7))
        path = QPainterPath()
        path.moveTo(b)
        path.lineTo(p1)
        path.lineTo(p2)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.fillPath(path, QBrush(_Theme.EDGE))

    # ---- 容器框 ----
    def _draw_group(self, p, node, rect):
        active = node.key in self._active_keys
        fill = _Theme.GROUP_FILL_ACTIVE if active else _Theme.GROUP_FILL
        border = _Theme.GROUP_BORDER_ACTIVE if active else _Theme.GROUP_BORDER

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(rect, 12, 12)

        pen = QPen(border, 1.5, Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 12, 12)

        # 标题胶囊(嵌在顶边左侧, 进出线走中部/右侧避开)
        f = _mkfont(8, bold=True)
        p.setFont(f)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(node.label)
        chip = QRectF(rect.x() + 14, rect.y() - 9, tw + 16, 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(_Theme.BG_TOP))
        p.drawRoundedRect(chip, 9, 9)
        p.setPen(_Theme.GROUP_BORDER_ACTIVE if active else _Theme.GROUP_TITLE)
        p.drawText(chip, Qt.AlignmentFlag.AlignCenter, node.label)

    # ---- 节点 ----
    def _draw_node(self, p, node, rect):
        active = node.key in self._active_keys
        is_fault = node.kind == "fault"

        # 警示说明块: 琥珀色虚线边, 非流程节点
        if node.kind == "warn":
            p.setBrush(QColor(200, 150, 60, 26))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(rect, 7, 7)
            pen = QPen(QColor("#C8963C"), 1.3, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, 7, 7)
            p.setPen(QColor("#E0B566"))
            p.setFont(_mkfont(8))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, node.label)
            return

        if active:
            top = _Theme.ACTIVE_FAULT_TOP if is_fault else _Theme.ACTIVE_TOP
            bottom = _Theme.ACTIVE_FAULT_BOTTOM if is_fault else _Theme.ACTIVE_BOTTOM
            border = _Theme.ACTIVE_FAULT_BORDER if is_fault else _Theme.ACTIVE_BORDER
            glow = _Theme.ACTIVE_FAULT_GLOW if is_fault else _Theme.ACTIVE_GLOW
            border_w = 2.0
        else:
            top = _Theme.FAULT_TOP if is_fault else _Theme.NODE_TOP
            bottom = _Theme.FAULT_BOTTOM if is_fault else _Theme.NODE_BOTTOM
            border = _Theme.FAULT_BORDER if is_fault else _Theme.NODE_BORDER
            glow = None
            border_w = 1.3

        radius = 8 if node.kind != "mode" else 7

        # 激活辉光: 多层渐淡描边模拟外发光
        if glow is not None:
            for i, alpha in ((6, 26), (4, 46), (2, 80)):
                gpen = QPen(QColor(glow.red(), glow.green(), glow.blue(), alpha), border_w + i)
                p.setPen(gpen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rect, radius + i / 2, radius + i / 2)

        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, bottom)
        p.setPen(QPen(border, border_w))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(rect, radius, radius)

        # 顶部高光线(玻璃感)
        hl = QPainterPath()
        inset = 2.0
        hl.moveTo(rect.left() + radius, rect.top() + inset)
        hl.lineTo(rect.right() - radius, rect.top() + inset)
        p.setPen(QPen(QColor(255, 255, 255, 28), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(hl)

        # 文本
        if active:
            p.setPen(QColor("#FFFFFF"))
        elif is_fault:
            p.setPen(QColor("#F2E2E2"))
        else:
            p.setPen(_Theme.NODE_TEXT)
        f = _mkfont(9 if node.kind != "mode" else 8, bold=active)
        p.setFont(f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, node.label)


def _anchor(rect, side, t=0.5):
    """矩形某条边上的锚点。side: 'L'/'R'/'T'/'B'; t: 沿该边的归一化位置(0~1)。"""
    if side == 'L':
        return QPointF(rect.left(), rect.top() + rect.height() * t)
    if side == 'R':
        return QPointF(rect.right(), rect.top() + rect.height() * t)
    if side == 'T':
        return QPointF(rect.left() + rect.width() * t, rect.top())
    # 'B'
    return QPointF(rect.left() + rect.width() * t, rect.bottom())


def _route(r1, r2, spec=None):
    """两矩形间的正交折线。

    spec(可选 dict) 控制锚点与走廊, 用于避免多线重叠:
      from / to : 出/入边 'L'/'R'/'T'/'B'
      ft / tt   : 出/入锚点沿边位置 0~1(默认 0.5)
      cx        : 竖直走廊的绝对 x(像素), 用于 L/R 之间的 Z 形折点
      cy        : 水平走廊的绝对 y(像素), 用于 T/B 之间的 Z 形折点
    无 spec 时退化为自动(按中心方向选边, 走中线)。"""
    if not spec:
        return _auto_route(r1, r2)

    s_from = spec.get('from')
    s_to = spec.get('to')
    ft = spec.get('ft', 0.5)
    tt = spec.get('tt', 0.5)
    if s_from is None or s_to is None:
        return _auto_route(r1, r2)

    a = _anchor(r1, s_from, ft)
    b = _anchor(r2, s_to, tt)

    horiz_from = s_from in ('L', 'R')
    horiz_to = s_to in ('L', 'R')

    if horiz_from and horiz_to:
        cx = spec.get('cx', (a.x() + b.x()) / 2.0)
        if abs(a.y() - b.y()) < 1.0:
            return [a, b]
        return [a, QPointF(cx, a.y()), QPointF(cx, b.y()), b]
    if (not horiz_from) and (not horiz_to):
        if 'cy' in spec and spec['cy'] is not None:
            cy = spec['cy']
        elif s_from == 'B' and s_to == 'B':
            cy = max(a.y(), b.y()) + 26.0     # 都从底边出, 绕到下方走廊
        elif s_from == 'T' and s_to == 'T':
            cy = min(a.y(), b.y()) - 26.0     # 都从顶边出, 绕到上方走廊
        else:
            cy = (a.y() + b.y()) / 2.0
        if abs(a.x() - b.x()) < 1.0:
            return [a, b]
        return [a, QPointF(a.x(), cy), QPointF(b.x(), cy), b]
    # 一横一竖: L 形(在拐角处折一次)
    if horiz_from:
        return [a, QPointF(b.x(), a.y()), b]
    return [a, QPointF(a.x(), b.y()), b]


def _auto_route(r1, r2):
    """自动正交布线: 按中心方向选边, 走中线 Z 形。"""
    c1 = r1.center()
    c2 = r2.center()
    dx = c2.x() - c1.x()
    dy = c2.y() - c1.y()
    eps = 6.0
    if abs(dx) >= abs(dy):
        if dx >= 0:
            a = QPointF(r1.right(), c1.y())
            b = QPointF(r2.left(), c2.y())
        else:
            a = QPointF(r1.left(), c1.y())
            b = QPointF(r2.right(), c2.y())
        if abs(dy) < eps:
            return [a, b]
        mid = (a.x() + b.x()) / 2.0
        return [a, QPointF(mid, a.y()), QPointF(mid, b.y()), b]
    else:
        if dy >= 0:
            a = QPointF(c1.x(), r1.bottom())
            b = QPointF(c2.x(), r2.top())
        else:
            a = QPointF(c1.x(), r1.top())
            b = QPointF(c2.x(), r2.bottom())
        if abs(dx) < eps:
            return [a, b]
        mid = (a.y() + b.y()) / 2.0
        return [a, QPointF(a.x(), mid), QPointF(b.x(), mid), b]


class _TopFsmView(_DiagramView):
    """顶层主状态机图。节点 key 用 TopFsm 名称字符串。"""

    def __init__(self, parent=None):
        super().__init__("主状态机 · top_fsm", parent)
        self._nodes = [
            _Node("INIT", "初始化模式", 0.37, 0.03, 0.26, 0.12, "state"),
            _Node("IDLE", "待机模式", 0.37, 0.28, 0.26, 0.12, "state"),
            _Node("READY", "就绪 · 已使能", 0.03, 0.28, 0.22, 0.12, "state"),
            _Node("WORK", "工作模式", 0.33, 0.54, 0.34, 0.44, "group"),
            _Node("RUN", "运行模式", 0.375, 0.605, 0.25, 0.078, "mode"),
            _Node("CONFIG", "配置模式", 0.375, 0.700, 0.25, 0.078, "mode"),
            _Node("CALIB", "标定模式", 0.375, 0.795, 0.25, 0.078, "mode"),
            _Node("BOOTLOADER", "升级模式", 0.375, 0.890, 0.25, 0.078, "mode"),
            _Node("FAULT", "故障 / 急停", 0.74, 0.27, 0.24, 0.14, "fault"),
        ]
        self._edges = [
            ("INIT", "IDLE", "初始化完成", {'from': 'B', 'to': 'T'}),
            ("IDLE", "WORK", "模式选择", {'from': 'B', 'to': 'T', 'ft': 0.5, 'tt': 0.6}),
            ("IDLE", "READY", "使能", {'from': 'L', 'to': 'R'}),
            ("READY", "RUN", "运动指令", {'from': 'B', 'to': 'L', 'ft': 0.35, 'tt': 0.5}),
            ("INIT", "FAULT", "初始故障", {'from': 'R', 'to': 'T', 'tt': 0.4}),
            ("WORK", "FAULT", "检测故障", {'from': 'R', 'to': 'B', 'ft': 0.14, 'tt': 0.6}),
            ("FAULT", "IDLE", "清除故障", {'from': 'L', 'to': 'R', 'tt': 0.4}),
            ("WORK", "IDLE", "切换待机", {'from': 'L', 'to': 'B', 'ft': 0.45, 'tt': 0.35}),
        ]
        self.set_note("运行/配置/标定/升级须从待机进入")

    def apply_state(self, top_fsm, run_state, ctrl_mode, enable):
        try:
            t = TopFsm(top_fsm)
        except ValueError:
            self.set_active(set())
            return
        mapping = {
            TopFsm.INIT: {"INIT"},
            TopFsm.IDLE: {"IDLE"},
            TopFsm.READY: {"READY"},
            TopFsm.RUN: {"WORK", "RUN"},
            TopFsm.CONFIG: {"WORK", "CONFIG"},
            TopFsm.CALIB: {"WORK", "CALIB"},
            TopFsm.BOOTLOADER: {"WORK", "BOOTLOADER"},
            TopFsm.FAULT: {"FAULT"},
            TopFsm.SAFETY: {"FAULT"},
        }
        self.set_active(mapping.get(t, set()))


class _RunModeView(_DiagramView):
    """运行模式状态机图。

    图中方框是 ctrl_mode_e(命令码), 当 top_fsm==RUN 时按上报 ctrl_mode 点亮;
    run_state 决定 IDLE/停机 等的实际显示。非 RUN 态整图置灰(无高亮)。
    """

    def __init__(self, parent=None):
        super().__init__("运行模式状态机 · 仅 top_fsm==RUN 激活", parent)
        self._nodes = [
            _Node("IDLE_GW", "待机模式", 0.00, 0.05, 0.16, 0.16, "state"),
            _Node("FAULT_GW", "异常模式", 0.00, 0.56, 0.16, 0.16, "fault"),
            _Node("ENTER", "IDLE", 0.205, 0.30, 0.115, 0.16, "mode"),
            _Node("RUN", "运行模式", 0.37, 0.02, 0.61, 0.96, "group"),
            _Node(int(JmCmd.POSITION_TORQUE), "PT", 0.41, 0.12, 0.155, 0.11, "mode"),
            _Node(int(JmCmd.ETHERCAT_CST), "CST", 0.595, 0.12, 0.155, 0.11, "mode"),
            _Node(int(JmCmd.PP), "PP", 0.78, 0.12, 0.155, 0.11, "mode"),
            _Node(int(JmCmd.PV), "PV", 0.41, 0.27, 0.155, 0.11, "mode"),
            _Node(int(JmCmd.ETHERCAT_CSV), "CSV", 0.595, 0.27, 0.155, 0.11, "mode"),
            _Node(int(JmCmd.ETHERCAT_CSP), "CSP", 0.78, 0.27, 0.155, 0.11, "mode"),
            _Node(int(JmCmd.PVT), "PVT", 0.78, 0.42, 0.155, 0.11, "mode"),
            _Node(int(JmCmd.MIT), "MIT", 0.78, 0.59, 0.155, 0.11, "mode"),
            _Node("DISABLE_RUN", "禁止运动切换", 0.41, 0.44, 0.155, 0.18, "warn"),
            _Node(int(JmCmd.HOMING), "回零", 0.41, 0.81, 0.155, 0.11, "mode"),
            _Node(int(JmCmd.STOP), "停机", 0.595, 0.81, 0.155, 0.11, "mode"),
        ]
        self._edges = [
            ("IDLE_GW", "ENTER", "主动切换", {'from': 'R', 'to': 'L', 'ft': 0.5, 'tt': 0.35}),
            ("FAULT_GW", "ENTER", "异常自动切回", {'from': 'R', 'to': 'L', 'ft': 0.5, 'tt': 0.75}),
            ("ENTER", "RUN", "", {'from': 'R', 'to': 'L'}),
            ("RUN", "FAULT_GW", "运行模式切换", {'from': 'L', 'to': 'T', 'ft': 0.85}),
        ]
        self.set_note("运动模式间不能相互切换, 须从待机进入")

    def apply_state(self, top_fsm, run_state, ctrl_mode, enable):
        keys = set()
        if top_fsm == int(TopFsm.RUN):
            keys.add("RUN")
            if self._node_by_key(int(ctrl_mode)) is not None:
                keys.add(int(ctrl_mode))
            elif run_state == int(RunState.IDLE):
                keys.add("ENTER")
        elif top_fsm in (int(TopFsm.FAULT), int(TopFsm.SAFETY)):
            keys.add("FAULT_GW")
        elif top_fsm in (int(TopFsm.IDLE), int(TopFsm.READY)):
            keys.add("IDLE_GW")
        self.set_active(keys)


class StateMachinePanel(QWidget):
    """状态机可视化 tab: 两张图 + 当前状态条 + 周期轮询开关。

    poll_state 信号: 需要主动拉一次状态时发出(主窗口连到 JmClient.query_state)。
    主窗口在链路连接/断开时调用 set_link_active() 控制轮询闸门。
    """

    poll_state = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._link_active = False
        self._last_state = (None, None, None, None)
        self._build()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._on_poll_tick)
        self._apply_poll_settings()

    def _build(self):
        self.setStyleSheet("""
            QWidget#smRoot { background: #16181F; }
            QLabel { color: #C8CCD8; }
            QCheckBox { color: #C8CCD8; }
            QSpinBox {
                background: #2A2E3A; color: #E4E7EF;
                border: 1px solid #444A5A; border-radius: 4px;
                padding: 2px 4px;
            }
            QSpinBox:disabled { color: #666; }
        """)
        self.setObjectName("smRoot")

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        root.addWidget(self._build_status_bar())

        self._top_view = _TopFsmView()
        self._run_view = _RunModeView()
        root.addWidget(self._top_view, 1)
        root.addWidget(self._run_view, 1)

    def _build_status_bar(self) -> QFrame:
        bar = QFrame()
        bar.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2A2F3D, stop:1 #232733);
                border: 1px solid #383E4E; border-radius: 8px;
            }
        """)
        bar.setFixedHeight(46)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 12, 0)
        lay.setSpacing(10)

        self._dot = QLabel("●")
        self._dot.setStyleSheet("color:#666; font-size:14px; border:none;")
        lay.addWidget(self._dot)

        self._lbl_state = QLabel("未连接")
        self._lbl_state.setStyleSheet(
            "font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 13px; "
            "font-weight: bold; color: #8A90A0; border: none;")
        lay.addWidget(self._lbl_state)
        lay.addStretch()

        self._chk_poll = QCheckBox("周期请求状态")
        self._chk_poll.setStyleSheet("border:none;")
        self._chk_poll.setChecked(True)
        self._chk_poll.toggled.connect(self._apply_poll_settings)
        lay.addWidget(self._chk_poll)

        lbl = QLabel("周期")
        lbl.setStyleSheet("border:none; color:#9298AC;")
        lay.addWidget(lbl)
        self._spin_period = QSpinBox()
        self._spin_period.setRange(50, 5000)
        self._spin_period.setSingleStep(50)
        self._spin_period.setValue(200)
        self._spin_period.setSuffix(" ms")
        self._spin_period.setFixedWidth(86)
        self._spin_period.valueChanged.connect(self._apply_poll_settings)
        lay.addWidget(self._spin_period)

        return bar

    # ---- 轮询闸门 ----
    def _apply_poll_settings(self):
        self._spin_period.setEnabled(self._chk_poll.isChecked())
        self._restart_poll()

    def _restart_poll(self):
        self._poll_timer.stop()
        if self._link_active and self._chk_poll.isChecked() and self.isVisible():
            self._poll_timer.start(self._spin_period.value())

    def _on_poll_tick(self):
        if self._link_active and self.isVisible():
            self.poll_state.emit()

    def set_link_active(self, active: bool):
        self._link_active = bool(active)
        self._restart_poll()
        if not active:
            self._dot.setStyleSheet("color:#666; font-size:14px; border:none;")
            self._lbl_state.setText("未连接")
            self._lbl_state.setStyleSheet(
                "font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 13px; "
                "font-weight: bold; color: #8A90A0; border: none;")

    def showEvent(self, evt):
        super().showEvent(evt)
        self._restart_poll()

    def hideEvent(self, evt):
        super().hideEvent(evt)
        self._poll_timer.stop()

    # ---- 状态更新 ----
    def update_state(self, top_fsm: int, run_state: int, ctrl_mode: int, enable: int):
        self._last_state = (top_fsm, run_state, ctrl_mode, enable)
        self._top_view.apply_state(top_fsm, run_state, ctrl_mode, enable)
        self._run_view.apply_state(top_fsm, run_state, ctrl_mode, enable)

        is_fault = top_fsm in (int(TopFsm.FAULT), int(TopFsm.SAFETY))
        en_txt = "ON" if enable else "OFF"
        color = "#FF8C88" if is_fault else "#5FE6AC"
        self._dot.setStyleSheet(f"color:{color}; font-size:14px; border:none;")
        text = (f"{top_fsm_name(top_fsm)}   ·   运行子态 {run_state_name(run_state)}"
                f"   ·   使能 {en_txt}")
        self._lbl_state.setText(text)
        self._lbl_state.setStyleSheet(
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 13px; "
            f"font-weight: bold; color: {color}; border: none;")
