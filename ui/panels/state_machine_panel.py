"""状态机可视化面板.

自绘两张状态机逻辑图(QPainter), 按下位机上报的实时状态高亮当前所处节点:
  - 上图: 顶层主状态机 (top_fsm)   —— 初始化/待机/就绪/工作模式容器/故障/急停
  - 下图: 运行模式状态机 (run_state + ctrl_mode) —— 仅 top_fsm==RUN 时激活

数据来源: JmClient.state_updated(top_fsm, run_state, ctrl_mode, enable)。
面板自带周期轮询: 可见且链路已连接时, 周期发 READ_STATE(0xC1) 主动拉状态,
与遥测订阅(0xCA 带 STATE 位)互补——无论是否开遥测都能刷新当前状态高亮。

节点与连线均为数据驱动(_NODES / _EDGES), 调整布局只改数据表, 不动绘制逻辑。
状态→节点 的映射严格对齐固件 state_define.h(见 jmproto.cmd_def.TopFsm / RunState)。
"""

from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QPainterPath, QPolygonF
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QSpinBox

from jmproto import JmCmd, TopFsm, RunState, top_fsm_name, run_state_name


# ---- 配色 ----
_COL_BG = QColor("#1E1E1E")
_COL_NODE = QColor("#2B2B33")
_COL_NODE_BORDER = QColor("#555560")
_COL_TEXT = QColor("#D8D8D8")
_COL_EDGE = QColor("#6A6A75")
_COL_GROUP_BORDER = QColor("#4A6A9A")
_COL_FAULT = QColor("#B85C5C")           # 故障/急停节点底色
_COL_ACTIVE = QColor("#2E8B57")          # 高亮: 当前激活节点(绿色)
_COL_ACTIVE_BORDER = QColor("#5FE0A0")
_COL_ACTIVE_FAULT = QColor("#E04848")    # 高亮: 当前处于故障/急停(红色)
_COL_ACTIVE_FAULT_BORDER = QColor("#FF8080")
_COL_INACTIVE_TEXT = QColor("#888890")


class _Node:
    """一个状态节点(逻辑坐标, 0~1 归一化后乘画布尺寸)。

    kind: 'state' 普通态 / 'fault' 故障类态 / 'group' 容器框 / 'mode' 运行模式块
    key:  与 top_fsm/run_state/ctrl_mode 匹配的标识(见各图 _match)。
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
        self._nodes = []          # list[_Node]
        self._edges = []          # list[(from_key, to_key, label)]
        self._active_keys = set()  # 当前高亮的节点 key
        self._note = ""
        self.setMinimumHeight(280)
        self.setMinimumWidth(360)

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
    def _rect_of(self, node, pad_x, pad_y, w, h):
        return QRectF(
            pad_x + node.x * w,
            pad_y + node.y * h,
            node.w * w,
            node.h * h,
        )

    def _node_by_key(self, key):
        for n in self._nodes:
            if n.key == key:
                return n
        return None

    def paintEvent(self, _evt):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), _COL_BG)

        pad_x, pad_y = 16.0, 36.0
        w = max(1.0, self.width() - 2 * pad_x)
        h = max(1.0, self.height() - pad_y - 16.0)

        # 标题
        p.setPen(_COL_TEXT)
        f = QFont()
        f.setPointSize(11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(pad_x, 6, w, 24), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._title)

        # 备注(右上)
        if self._note:
            p.setPen(_COL_INACTIVE_TEXT)
            fn = QFont()
            fn.setPointSize(9)
            p.setFont(fn)
            p.drawText(QRectF(pad_x, 6, w, 24), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._note)

        rects = {n.key: self._rect_of(n, pad_x, pad_y, w, h) for n in self._nodes}

        # 先画连线(在节点下方)
        self._draw_edges(p, rects)

        # 再画节点
        for n in self._nodes:
            self._draw_node(p, n, rects[n.key])

        p.end()

    def _draw_edges(self, p, rects):
        pen = QPen(_COL_EDGE, 1.6)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        fe = QFont()
        fe.setPointSize(8)
        for from_key, to_key, label in self._edges:
            r1 = rects.get(from_key)
            r2 = rects.get(to_key)
            if r1 is None or r2 is None:
                continue
            pts = _ortho_points(r1, r2)
            if len(pts) < 2:
                continue
            p.setPen(pen)
            self._draw_rounded_polyline(p, pts, pen)
            # 箭头方向取最后一段
            self._draw_arrow_head(p, pts[-2], pts[-1])
            if label:
                p.setPen(_COL_INACTIVE_TEXT)
                p.setFont(fe)
                # 标签放在中间段中点上方
                i = len(pts) // 2
                a, b = pts[i - 1], pts[i]
                mid = QPointF((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0 - 4)
                p.drawText(mid, label)

    @staticmethod
    def _draw_rounded_polyline(p, pts, pen, radius=8.0):
        """按折点序列绘制正交折线, 在每个转角处用二次贝塞尔做小圆角过渡。"""
        import math
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
        import math
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        size = 7.0
        p1 = QPointF(b.x() - size * math.cos(ang - math.pi / 7),
                     b.y() - size * math.sin(ang - math.pi / 7))
        p2 = QPointF(b.x() - size * math.cos(ang + math.pi / 7),
                     b.y() - size * math.sin(ang + math.pi / 7))
        path = QPainterPath()
        path.moveTo(b)
        path.lineTo(p1)
        path.lineTo(p2)
        path.closeSubpath()
        p.fillPath(path, QBrush(_COL_EDGE))

    def _draw_node(self, p, node, rect):
        active = node.key in self._active_keys
        is_fault = node.kind == "fault"

        if node.kind == "group":
            # 容器框: 虚线边, 半透明, 不参与高亮填充
            pen = QPen(_COL_GROUP_BORDER, 1.6, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, 8, 8)
            p.setPen(_COL_TEXT if not active else _COL_ACTIVE_BORDER)
            f = QFont(); f.setPointSize(9); f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(rect.x(), rect.y() + 4, rect.width(), 18),
                       Qt.AlignmentFlag.AlignHCenter, node.label)
            return

        # 普通态 / 故障态 / 运行模式块
        if active:
            fill = _COL_ACTIVE_FAULT if is_fault else _COL_ACTIVE
            border = _COL_ACTIVE_FAULT_BORDER if is_fault else _COL_ACTIVE_BORDER
            border_w = 2.4
        else:
            fill = _COL_FAULT if is_fault else _COL_NODE
            border = _COL_NODE_BORDER
            border_w = 1.4

        p.setPen(QPen(border, border_w))
        p.setBrush(QBrush(fill))
        radius = 6 if node.kind != "mode" else 5
        p.drawRoundedRect(rect, radius, radius)

        # 文本
        if active:
            p.setPen(QColor("#FFFFFF"))
        elif is_fault:
            p.setPen(QColor("#F0E0E0"))
        else:
            p.setPen(_COL_TEXT)
        f = QFont()
        f.setPointSize(9 if node.kind != "mode" else 8)
        f.setBold(active)
        p.setFont(f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, node.label)


def _ortho_points(r1, r2):
    """计算两矩形间的正交(横平竖直)折线折点序列。

    水平为主时走 左右出/入 + 中线竖折(Z形); 竖直为主时走 上下出/入 + 中线横折。
    两节点近似对齐时退化为直线。返回值供 _draw_rounded_polyline 渲染。"""
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
        super().__init__("主状态机 (top_fsm)", parent)
        # 布局: 归一化坐标 (x,y,w,h)
        self._nodes = [
            _Node("INIT", "初始化模式", 0.30, 0.00, 0.30, 0.13, "state"),
            _Node("IDLE", "待机模式", 0.30, 0.28, 0.30, 0.13, "state"),
            _Node("WORK", "工作模式", 0.26, 0.52, 0.38, 0.46, "group"),
            _Node("RUN", "运行模式", 0.30, 0.60, 0.30, 0.085, "mode"),
            _Node("CONFIG", "配置模式", 0.30, 0.70, 0.30, 0.085, "mode"),
            _Node("CALIB", "标定模式", 0.30, 0.80, 0.30, 0.085, "mode"),
            _Node("BOOTLOADER", "升级模式", 0.30, 0.90, 0.30, 0.085, "mode"),
            _Node("READY", "就绪(已使能)", 0.00, 0.28, 0.22, 0.13, "state"),
            _Node("FAULT", "故障/急停模式", 0.70, 0.28, 0.30, 0.20, "fault"),
        ]
        self._edges = [
            ("INIT", "IDLE", "初始化完成"),
            ("IDLE", "WORK", "模式选择"),
            ("IDLE", "READY", "使能"),
            ("READY", "RUN", "运动指令"),
            ("INIT", "FAULT", "初始故障"),
            ("WORK", "FAULT", "检测故障"),
            ("FAULT", "IDLE", "清除故障"),
            ("WORK", "IDLE", "切换待机"),
        ]
        self.set_note("运行/配置/标定/升级须从待机进入")

    def apply_state(self, top_fsm, run_state, ctrl_mode, enable):
        """根据 top_fsm 点亮对应节点。"""
        keys = set()
        try:
            t = TopFsm(top_fsm)
        except ValueError:
            self.set_active(keys)
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

    # 方框 key 用 JmCmd 命令码(与固件 ctrl_mode_e 同值)
    def __init__(self, parent=None):
        super().__init__("运行模式状态机 (top_fsm==RUN 激活)", parent)
        self._nodes = [
            _Node("IDLE_GW", "待机模式", 0.00, 0.05, 0.16, 0.16, "state"),
            _Node("FAULT_GW", "异常模式", 0.00, 0.55, 0.16, 0.16, "fault"),
            _Node("ENTER", "IDLE", 0.20, 0.30, 0.12, 0.16, "mode"),
            _Node("RUN", "运行模式", 0.36, 0.00, 0.62, 1.00, "group"),
            # 运行模式容器内的控制律块(key=JmCmd 命令码)
            _Node(int(JmCmd.POSITION_TORQUE), "PT", 0.40, 0.10, 0.16, 0.11, "mode"),
            _Node(int(JmCmd.ETHERCAT_CST), "CST", 0.59, 0.10, 0.16, 0.11, "mode"),
            _Node(int(JmCmd.PP), "PP", 0.78, 0.10, 0.16, 0.11, "mode"),
            _Node(int(JmCmd.PV), "PV", 0.40, 0.26, 0.16, 0.11, "mode"),
            _Node(int(JmCmd.ETHERCAT_CSV), "CSV", 0.59, 0.26, 0.16, 0.11, "mode"),
            _Node(int(JmCmd.ETHERCAT_CSP), "CSP", 0.78, 0.26, 0.16, 0.11, "mode"),
            _Node(int(JmCmd.PVT), "PVT", 0.78, 0.44, 0.16, 0.11, "mode"),
            _Node(int(JmCmd.MIT), "MIT", 0.78, 0.62, 0.16, 0.11, "mode"),
            _Node("DISABLE_RUN", "禁止运动切换", 0.40, 0.46, 0.16, 0.18, "fault"),
            _Node(int(JmCmd.HOMING), "回零", 0.40, 0.82, 0.16, 0.11, "mode"),
            _Node(int(JmCmd.STOP), "停机", 0.59, 0.82, 0.16, 0.11, "mode"),
        ]
        self._edges = [
            ("IDLE_GW", "ENTER", "主动切换"),
            ("FAULT_GW", "ENTER", "发生异常自动切回"),
            ("ENTER", "RUN", ""),
            ("RUN", "FAULT_GW", "运行模式切换"),
        ]
        self.set_note("运动模式间不能相互切换, 须从待机进入")

    def apply_state(self, top_fsm, run_state, ctrl_mode, enable):
        keys = set()
        if top_fsm == int(TopFsm.RUN):
            keys.add("RUN")
            # ctrl_mode 是命令码, 直接匹配方框 key
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
    """状态机可视化 tab: 两张图 + 当前状态文字 + 周期轮询开关。

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
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # 顶部: 当前状态条 + 轮询控制
        bar = QHBoxLayout()
        bar.setSpacing(12)

        self._lbl_state = QLabel("当前状态: --")
        self._lbl_state.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 13px; font-weight: bold; color: #5FE0A0;")
        bar.addWidget(self._lbl_state)
        bar.addStretch()

        self._chk_poll = QCheckBox("周期请求状态")
        self._chk_poll.setChecked(True)
        self._chk_poll.toggled.connect(self._apply_poll_settings)
        bar.addWidget(self._chk_poll)

        bar.addWidget(QLabel("周期(ms):"))
        self._spin_period = QSpinBox()
        self._spin_period.setRange(50, 5000)
        self._spin_period.setSingleStep(50)
        self._spin_period.setValue(200)
        self._spin_period.valueChanged.connect(self._apply_poll_settings)
        bar.addWidget(self._spin_period)

        root.addLayout(bar)

        # 两张图
        self._top_view = _TopFsmView()
        self._run_view = _RunModeView()
        root.addWidget(self._top_view, 1)
        root.addWidget(self._run_view, 1)

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
        """链路连接状态变化时由主窗口调用, 控制轮询启停。"""
        self._link_active = bool(active)
        self._restart_poll()
        if not active:
            self._lbl_state.setText("当前状态: 未连接")
            self._lbl_state.setStyleSheet(
                "font-family: Consolas, monospace; font-size: 13px; font-weight: bold; color: #888;")

    # 可见性变化时启停轮询, 不在该 tab 时不浪费带宽
    def showEvent(self, evt):
        super().showEvent(evt)
        self._restart_poll()

    def hideEvent(self, evt):
        super().hideEvent(evt)
        self._poll_timer.stop()

    # ---- 状态更新(主窗口连 JmClient.state_updated) ----
    def update_state(self, top_fsm: int, run_state: int, ctrl_mode: int, enable: int):
        self._last_state = (top_fsm, run_state, ctrl_mode, enable)
        self._top_view.apply_state(top_fsm, run_state, ctrl_mode, enable)
        self._run_view.apply_state(top_fsm, run_state, ctrl_mode, enable)

        en_txt = "ON" if enable else "OFF"
        color = "#5FE0A0"
        if top_fsm in (int(TopFsm.FAULT), int(TopFsm.SAFETY)):
            color = "#FF8080"
        text = (f"当前状态: {top_fsm_name(top_fsm)}"
                f"  |  运行子态: {run_state_name(run_state)}"
                f"  |  使能: {en_txt}")
        self._lbl_state.setText(text)
        self._lbl_state.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 13px; font-weight: bold; color: {color};")
