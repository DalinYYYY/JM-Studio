"""实时反馈面板: FOC 电流环框图 + 电机实时旋转示意 + 系统参数分区卡片。

布局:
  上半左: _FocDiagram —— 自绘 FOC 电流闭环框图, 电气量贴附信号流环节。
  上半右: _MotorView —— 2D 电机截面旋转示意, 60fps 平滑动画(由速度航位推算,
          不受遥测帧率限制), 实时反映转子角度与转向。
  下半: 三相电流分列卡 + 母线/温度/位置·速度/状态·故障 分区卡片。

数据入口沿用原接口: update_feedback(fb) / update_state(top_fsm, run_state, ctrl_mode, enable)。
"""

import math

from PyQt6.QtCore import Qt, QRectF, QPointF, QTimer, QElapsedTimer, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QPainterPath, QLinearGradient,
    QRadialGradient, QConicalGradient,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame, QSizePolicy,
    QPushButton, QCheckBox, QSpinBox,
)

from jmproto import top_fsm_name, run_state_name, motion_state_name, cmd_name, fault_mask_to_str
from ui.panels._edit_mixin import LayoutEditMixin
from ui import layout_store
from ui.theme import theme
from ui.display_filter import AngleDeadZone


_FONT_FAMILIES = ["Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", "sans-serif"]


def _mkfont(pt, bold=False):
    f = QFont()
    f.setFamilies(_FONT_FAMILIES)
    f.setPointSize(pt)
    f.setBold(bold)
    return f


class _TProxy:
    """FOC 框图配色代理: 属性名 -> theme.c(key), 主题切换即时生效。

    保留原有 _T.EDGE 等大写属性引用不变, 仅把取值转发到全局主题。
    """
    _MAP = {
        "BG_TOP": "bg_top", "BG_BOTTOM": "bg_bottom",
        "BLOCK_TOP": "block_top", "BLOCK_BOTTOM": "block_bottom",
        "BLOCK_BORDER": "block_border", "BLOCK_TEXT": "block_text",
        "HI_BORDER": "hi_border", "EDGE": "edge", "EDGE_FB": "edge_fb",
        "TITLE": "text_strong", "MUTED": "muted",
        "VALUE": "value", "VALUE_HOT": "value_hot", "UNIT": "unit",
    }

    def __getattr__(self, name):
        key = self._MAP.get(name)
        if key is None:
            raise AttributeError(name)
        return theme.c(key)


_T = _TProxy()


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


class _Edge:
    """框图中的一条连线。

    p1_override / p2_override: 可选的归一化绝对坐标 (x, y), 覆盖默认锚点(块边中点)。
        为 None 时回退到 _port(from_block, from_side) / _port(to_block, to_side)。
        拖动节点时, 已设置的 override 会跟随节点 delta 平移(见 _on_box_moved),
        实现"跟随节点"行为。
    """
    __slots__ = ("from_key", "from_side", "to_key", "to_side", "kind",
                 "p1_override", "p2_override")

    def __init__(self, from_key, from_side, to_key, to_side, kind,
                 p1_override=None, p2_override=None):
        self.from_key = from_key
        self.from_side = from_side
        self.to_key = to_key
        self.to_side = to_side
        self.kind = kind              # 'fwd' 正向 / 'fb' 反馈
        self.p1_override = p1_override  # (x, y) 归一化, 或 None
        self.p2_override = p2_override


class _Annot:
    """框图上的一个数值标注。

    pos 为可选的独立归一化坐标 (x, y); 为 None 时回退到相对锚定块的自动位置
    (anchor 块下方), 保证向后兼容。编辑拖动后写入 pos。
    """
    __slots__ = ("key", "role", "vkey", "color", "anchor", "pos", "multiline")

    def __init__(self, key, role, vkey, color, anchor, pos=None, multiline=False):
        self.key = key
        self.role = role        # 角色文本(如 "Vbus")
        self.vkey = vkey        # 取值键(在 _vals 中)
        self.color = color
        self.anchor = anchor    # 锚定块 key(pos 为 None 时据此自动定位)
        self.pos = pos          # (x, y) 归一化, 或 None
        self.multiline = multiline  # 三相 ia/ib/ic 多行


class _FocDiagram(LayoutEditMixin, QWidget):
    """FOC 电流闭环框图(自绘), 在对应环节标注实时电气量。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._vals = {}     # key -> 显示字符串
        self._dirty = False  # 脏标记: set_values 置位, flush 时清除并 update
        self._ensure_edit_state()
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
        # 连线: _Edge(from, fromSide, to, toSide, kind)  kind: 'fwd' 正向 / 'fb' 反馈
        # p1_override/p2_override 默认 None (用块边锚点); 编辑后写入归一化绝对坐标。
        self._edges = [
            _Edge("ref", "R", "pi", "L", "fwd"),
            _Edge("pi", "R", "ipark", "L", "fwd"),
            _Edge("ipark", "R", "svpwm", "L", "fwd"),
            _Edge("svpwm", "R", "inv", "L", "fwd"),
            _Edge("inv", "R", "motor", "L", "fwd"),
            # 反馈主链: 电机三相 ↓ Clarke → Park → idq → 闭环回 PI
            _Edge("motor", "B", "clarke", "T", "fb"),
            _Edge("clarke", "L", "park", "R", "fb"),
            _Edge("park", "L", "idq", "R", "fb"),
            _Edge("idq", "L", "pi", "B", "fb"),
            # 电机 → 编码器(机械耦合), 编码器 θ ↓ Park(正上方直接向下喂)
            _Edge("motor", "B", "enc", "T", "fb"),
            _Edge("enc", "B", "park", "T", "fb"),
        ]
        # 数值标注(_Annot): pos=None 时自动锚定到块下方; iabc 三相多行。
        self._annots = [
            _Annot("vbus", "Vbus", "vbus", _T.VALUE_HOT, anchor="inv"),
            _Annot("torque", "τ", "torque", _T.VALUE_HOT, anchor="motor"),
            _Annot("idq", "id/iq", "idq", _T.VALUE, anchor="idq"),
            _Annot("thw", "θ/ω", "thw", _T.VALUE, anchor="enc"),
            _Annot("iabc", "", "iabc_lines", _T.VALUE, anchor="motor", multiline=True),
        ]

    def set_values(self, vals: dict):
        self._vals = dict(vals)
        self._dirty = True  # 标记脏, 由 flush 统一触发重绘(避免每帧 update)

    def flush(self):
        """由外部定时器统一调用, 仅在脏时触发重绘"""
        if self._dirty:
            self._dirty = False
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

    # ==================== 布局编辑(LayoutEditMixin 钩子) ====================
    def _geom(self):
        ox, oy = 14.0, 34.0
        w = max(1.0, self.width() - 2 * ox)
        h = max(1.0, self.height() - oy - 14.0)
        return ox, oy, w, h

    def _edit_geom(self):
        return self._geom()

    def _iter_boxes(self):
        return [(b.key, b) for b in self._blocks]

    def _annot_pos(self, a):
        """标注归一化坐标: 优先 a.pos, 否则按锚定块自动算(块下方居中)。"""
        if a.pos is not None:
            return a.pos
        b = next((x for x in self._blocks if x.key == a.anchor), None)
        if b is None:
            return (0.5, 0.5)
        if a.multiline:   # 三相: 锚块下方稍远
            return (b.x + b.w / 2, b.y + b.h + 0.18)
        return (b.x + b.w / 2, b.y + b.h + 0.06)

    def _iter_labels(self):
        out = []
        for a in self._annots:
            x, y = self._annot_pos(a)
            out.append((a.key, x, y))
        return out

    def _set_label_pos(self, key, nx, ny):
        for a in self._annots:
            if a.key == key:
                a.pos = (nx, ny)
                return

    def export_dict(self) -> dict:
        """导出 FOC 布局: blocks(x,y,w,h) + annots(x,y) + edges(p1/p2 override)。"""
        edges_out = {}
        for i, e in enumerate(self._edges):
            d = {}
            if e.p1_override is not None:
                d["p1"] = [round(e.p1_override[0], 3), round(e.p1_override[1], 3)]
            if e.p2_override is not None:
                d["p2"] = [round(e.p2_override[0], 3), round(e.p2_override[1], 3)]
            if d:
                edges_out[str(i)] = d
        return {
            "blocks": {b.key: [round(b.x, 3), round(b.y, 3),
                               round(b.w, 3), round(b.h, 3)] for b in self._blocks},
            "annots": {a.key: [round(p[0], 3), round(p[1], 3)]
                       for a in self._annots for p in [self._annot_pos(a)]},
            "edges": edges_out,
        }

    def apply_dict(self, d: dict):
        """套用 JSON 布局(缺项保持默认)。"""
        if not isinstance(d, dict):
            return
        blocks = d.get("blocks", {})
        for b in self._blocks:
            v = blocks.get(b.key)
            if isinstance(v, (list, tuple)) and len(v) == 4:
                b.x, b.y, b.w, b.h = (float(v[0]), float(v[1]), float(v[2]), float(v[3]))
        annots = d.get("annots", {})
        for a in self._annots:
            v = annots.get(a.key)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                a.pos = (float(v[0]), float(v[1]))
        edges = d.get("edges", {})
        for i, edge in enumerate(self._edges):
            v = edges.get(str(i))
            if not isinstance(v, dict):
                continue
            p1 = v.get("p1")
            if isinstance(p1, (list, tuple)) and len(p1) == 2:
                edge.p1_override = (float(p1[0]), float(p1[1]))
            p2 = v.get("p2")
            if isinstance(p2, (list, tuple)) and len(p2) == 2:
                edge.p2_override = (float(p2[0]), float(p2[1]))
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

        self.draw_edit_overlay(p)

        p.end()

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
        for edge in self._edges:
            r1 = rects.get(edge.from_key)
            r2 = rects.get(edge.to_key)
            if r1 is None or r2 is None:
                continue
            a = self._edge_point(edge, "p1", r1)
            b = self._edge_point(edge, "p2", r2)
            pts = self._ortho(a, edge.from_side, b, edge.to_side)
            is_fb = edge.kind == "fb"
            self._stroke(p, pts, fb_pen if is_fb else fwd_pen)
            self._arrow(p, pts[-2], pts[-1], _T.EDGE_FB if is_fb else _T.EDGE)

    def _edge_point(self, edge, which, r):
        """计算端点像素坐标: 优先 override, 否则用块边锚点。"""
        override = edge.p1_override if which == "p1" else edge.p2_override
        if override is not None:
            ox, oy, w, h = self._geom()
            return QPointF(ox + override[0] * w, oy + override[1] * h)
        side = edge.from_side if which == "p1" else edge.to_side
        return self._port(r, side)

    def _iter_edges(self):
        """返回 [(idx, p1_px, p2_px)] 供编辑命中测试用(像素坐标)。"""
        out = []
        ox, oy, w, h = self._geom()
        rects = {b.key: QRectF(ox + b.x * w, oy + b.y * h, b.w * w, b.h * h)
                 for b in self._blocks}
        for i, edge in enumerate(self._edges):
            r1 = rects.get(edge.from_key)
            r2 = rects.get(edge.to_key)
            if r1 is None or r2 is None:
                continue
            p1 = self._edge_point(edge, "p1", r1)
            p2 = self._edge_point(edge, "p2", r2)
            out.append((i, p1, p2))
        return out

    def _set_edge_endpoint(self, idx, end, nx, ny):
        """写回端点 override 坐标(归一化)。end: 'p1' / 'p2'。"""
        if 0 <= idx < len(self._edges):
            edge = self._edges[idx]
            if end == "p1":
                edge.p1_override = (nx, ny)
            else:
                edge.p2_override = (nx, ny)

    def _on_box_moved(self, key, dx, dy):
        """节点拖动后, 所有以该节点为端点的 edge 的 override 端点跟随平移。

        未 override 的端点不受影响(它们本就由块边锚点决定, 自动跟随)。
        """
        from ui.panels._edit_mixin import _snap
        for edge in self._edges:
            if edge.from_key == key and edge.p1_override is not None:
                edge.p1_override = (_snap(edge.p1_override[0] + dx),
                                    _snap(edge.p1_override[1] + dy))
            if edge.to_key == key and edge.p2_override is not None:
                edge.p2_override = (_snap(edge.p2_override[0] + dx),
                                    _snap(edge.p2_override[1] + dy))

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
        """画数值标注: 位置取 _annot_pos(优先独立坐标, 否则锚定块下方)。"""
        ox, oy, w, h = self._geom()
        for a in self._annots:
            ax, ay = self._annot_pos(a)
            center = QPointF(ox + ax * w, oy + ay * h)
            if a.multiline:
                lines = self._vals.get(a.vkey, ["ia --", "ib --", "ic --"])
                self._chip_multiline(p, center, lines, a.color)
            else:
                val = self._vals.get(a.vkey, "--")
                text = f"{a.role} = {val}" if a.role else str(val)
                self._chip(p, center, text, a.color)

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
        self._name_lbls = []
        self._unit_lbls = []
        self._val_color = {}     # attr -> 覆盖色(语义色, 如故障红); 默认用 theme value
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(5)

        self._head = QLabel(title)
        self._head.setFont(_mkfont(9, bold=True))
        lay.addWidget(self._head)

        self._line = QFrame()
        self._line.setFixedHeight(1)
        lay.addWidget(self._line)

        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self._val_labels = {}
        for i, (label, attr, _fmt, unit) in enumerate(rows):
            name = QLabel(label)
            name.setFont(_mkfont(9))
            self._name_lbls.append(name)
            val = QLabel("--")
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            u = QLabel(unit)
            u.setFont(_mkfont(8))
            u.setFixedWidth(46)
            self._unit_lbls.append(u)
            self._val_labels[attr] = val
            grid.addWidget(name, i, 0)
            grid.addWidget(val, i, 1)
            grid.addWidget(u, i, 2)
        lay.addLayout(grid)
        lay.addStretch()
        self.apply_theme()

    def _val_style(self, color_hex):
        return (f"color:{color_hex}; font-family:Consolas,'Microsoft YaHei',monospace; "
                "font-size:13px; font-weight:bold; border:none;")

    def apply_theme(self):
        self.setStyleSheet(
            "QFrame {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, "
            "stop:0 {top}, stop:1 {bot}); border: 1px solid {bd}; border-radius: 8px; }} "
            "QLabel {{ border: none; }}".format(
                top=theme.hex("card_top"), bot=theme.hex("card_bottom"), bd=theme.hex("border")))
        self._head.setStyleSheet(f"color:{theme.hex('title')}; font-weight:bold; border:none;")
        self._line.setStyleSheet(f"background:{theme.hex('border')}; border:none;")
        for n in self._name_lbls:
            n.setStyleSheet(f"color:{theme.hex('text')}; border:none;")
        for u in self._unit_lbls:
            u.setStyleSheet(f"color:{theme.hex('muted')}; border:none;")
        for attr, lbl in self._val_labels.items():
            col = self._val_color.get(attr) or theme.hex("value")
            lbl.setStyleSheet(self._val_style(col))

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
        # 仅颜色变化时才重设样式表(setStyleSheet 极其昂贵, 避免每帧调用)
        new_color = color or theme.hex("value")
        old_color = self._val_color.get(attr)
        if old_color != new_color:
            self._val_color[attr] = new_color
            lbl.setStyleSheet(self._val_style(new_color))


class _MotorView(QWidget):
    """2D 电机截面旋转示意。

    平滑动画核心: 内部维护"显示角度" _disp_th, 每帧(60fps)按最近一次上报的机械
    速度 ω 做航位推算递推, 因此即便遥测帧率很低, 转子也能匀速顺滑旋转; 每次收到新
    的真实角度时, 把显示角度柔和地拉向真实值(避免跳变)。
    """

    DEFAULT_POLE_PAIRS = 7

    # 极对数 → 定子槽数(常见分数槽集中绕组槽极配合, 仅视觉示意; 未知回退 12)
    _SLOTS_BY_PP = {1: 6, 2: 6, 3: 9, 4: 12, 5: 12, 6: 18, 7: 12,
                    8: 24, 9: 27, 10: 12, 11: 12, 12: 36, 13: 12, 14: 12}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(210)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._pole_pairs = self.DEFAULT_POLE_PAIRS
        self._stator_slots = self._SLOTS_BY_PP.get(self._pole_pairs, 12)
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
        self._timer.setInterval(33)   # ~30fps (足够流畅, 降低 paintEvent 开销)
        self._timer.timeout.connect(self._tick)

    # 仅在可见时跑动画, 省 CPU
    def showEvent(self, e):
        super().showEvent(e)
        # 切换回本面板时, 显示角度直接对齐到最新真实角度, 避免从历史位置缓慢旋转到当前
        self._disp_th = self._tgt_th
        self._last_ms = self._clock.elapsed()
        self._timer.start()
        self.update()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def set_feedback(self, single_rad, omega, iq, enabled):
        # single_rad 已由 FeedbackPanel.update_feedback 做位置死区滤波, 这里直接接收
        self._tgt_th = float(single_rad or 0.0)
        self._omega = float(omega or 0.0)
        self._iq = float(iq or 0.0)
        self._enabled = bool(enabled)

    def set_enabled(self, enabled: bool):
        """只更新使能高亮状态; 由状态帧驱动, 避免等待下一帧反馈才刷新边框。"""
        new_enabled = bool(enabled)
        if self._enabled != new_enabled:
            self._enabled = new_enabled
            self.update()

    def set_motor_params(self, params: dict):
        """电机本体参数(r/ld/lq/flux/kt/ke/pole_pairs)。极对数为0或缺失时用默认值。

        磁极扇形数与定子槽数随极对数联动(槽极配合见 _SLOTS_BY_PP)。
        """
        self._params = dict(params)
        pp = int(params.get("pole_pairs", 0) or 0)
        self._pole_pairs = pp if pp > 0 else self.DEFAULT_POLE_PAIRS
        self._stator_slots = self._SLOTS_BY_PP.get(self._pole_pairs, 12)
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
        # 薄外壳: 外圆与内孔间只留一道窄环。使能时外壳高亮(青绿), 否则暗色。
        if self._enabled:
            shell_edge = theme.c("hi_border")
            grad = QRadialGradient(cx, cy, R)
            grad.setColorAt(0.90, theme.c("active_bottom"))
            grad.setColorAt(1.0, theme.c("active_top"))
            # 外发光环, 强化"通电"观感
            for i, alpha in ((6, 36), (3, 70)):
                gc = theme.c("hi_border")
                p.setPen(QPen(QColor(gc.red(), gc.green(), gc.blue(), alpha), 1.6 + i))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(cx, cy), R + i / 2, R + i / 2)
        else:
            shell_edge = theme.c("rotor_shell")
            grad = QRadialGradient(cx, cy, R)
            grad.setColorAt(0.90, theme.c("block_bottom"))
            grad.setColorAt(1.0, theme.c("rotor_shell"))
        p.setPen(QPen(shell_edge, 1.8 if self._enabled else 1.6))
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(cx, cy), R, R)
        p.setPen(QPen(theme.c("rotor_tooth_border"), 1.2))
        p.setBrush(theme.c("rotor_bore"))
        p.drawEllipse(QPointF(cx, cy), R * 0.86, R * 0.86)
        # 定子齿(指向圆心)
        p.setBrush(theme.c("rotor_tooth"))
        p.setPen(QPen(theme.c("rotor_tooth_border"), 1.0))
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
        grad.setColorAt(0.0, theme.c("node_top"))
        grad.setColorAt(1.0, theme.c("node_bottom"))
        p.setPen(QPen(theme.c("rotor_tooth_border"), 1.5))
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(cx, cy), r, r)

        # 磁极: 用扇形环交替 N(红)/S(蓝), 不重叠, 随角度旋转
        n = self._pole_pairs * 2
        outer = QRectF(cx - r * 0.92, cy - r * 0.92, r * 1.84, r * 1.84)
        seg = 360.0 / n
        col_n, col_s = theme.c("rotor_n"), theme.c("rotor_s")
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(n):
            is_n = (i % 2 == 0)
            col = col_n if is_n else col_s
            start = math.degrees(th) + i * seg + 1.5
            path = QPainterPath()
            path.moveTo(QPointF(cx, cy))
            path.arcTo(outer, -start, -(seg - 3.0))   # Qt 角度逆时针为正, 取负顺时针
            path.closeSubpath()
            p.setBrush(col)
            p.drawPath(path)

        # 内圈遮罩(留出磁极环宽度), 形成磁极在外环的观感
        grad2 = QRadialGradient(cx, cy, r * 0.58)
        grad2.setColorAt(0.0, theme.c("node_top"))
        grad2.setColorAt(1.0, theme.c("node_bottom"))
        p.setBrush(QBrush(grad2))
        p.drawEllipse(QPointF(cx, cy), r * 0.58, r * 0.58)

        # d 轴标记(小三角, 指示转子磁链方向)
        dax = cx + r * 0.5 * math.cos(th)
        day = cy + r * 0.5 * math.sin(th)
        p.setBrush(theme.c("value_hot"))
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
        p.setBrush(theme.c("rotor_hub"))
        p.setPen(QPen(theme.c("rotor_tooth_border"), 1.2))
        p.drawEllipse(QPointF(cx, cy), r * 0.18, r * 0.18)

    def _draw_pointer(self, p, cx, cy, R, th):
        col = _T.HI_BORDER if self._enabled else theme.c("muted")
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
        ("_slots", "Z", "{:d}", ""),
    ]

    def _draw_params(self, p, y0):
        """转子图下方: 两列网格列出关键电机参数。"""
        x0 = 14.0
        w = self.width() - 28.0
        # 分隔线 + 小标题
        p.setPen(QPen(theme.c("border"), 1))
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
            # 取值: ld/lq 由 H 换算 mH; 槽数取当前槽极配合(非协议参数)
            if key == "_slots":
                v = self._stator_slots
            else:
                v = self._params.get(key, None)
            if v is None:
                vtxt = "--"
            else:
                try:
                    if key in ("ld", "lq"):
                        vtxt = fmt.format(float(v) * 1000.0)
                    elif key in ("pole_pairs", "_slots"):
                        vtxt = fmt.format(int(v))
                    else:
                        vtxt = fmt.format(float(v))
                except Exception:
                    vtxt = str(v)
            p.setPen(theme.c("muted"))
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

    poll_state = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fbRoot")
        self._enabled = False
        self._last_state = (None, None, None, None)
        self._pole_pairs = _MotorView.DEFAULT_POLE_PAIRS
        self._elec_bias = 0.0     # elec_angle_bias (rad), 由 set_motor_params 注入
        # 位置变化死区: 静止时锁定 single, 避免编码器噪声导致数值卡片/示意图抖动;
        # 数值卡片与转子示意图共用同一滤波实例, 保证显示一致
        self._single_filter = AngleDeadZone(pos_threshold=0.05)
        self._disp_pos = 0.0        # 解锁时缓存的 pos, 锁定时保持不变
        self._disp_multiturn = 0    # 解锁时缓存的 multiturn, 锁定时保持不变
        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(False)
        self._poll_timer.timeout.connect(self._on_poll_tick)
        self._state_view = None   # 上图替换视图('top_fsm' 形态), None=默认 FOC 框图
        self._build()
        self.apply_theme()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        self._poll_bar = self._build_poll_bar()
        root.addWidget(self._poll_bar)

        # 上: FOC 框图(左, 占主) + 电机旋转示意(右)
        top = QHBoxLayout()
        top.setSpacing(8)
        self._foc = _FocDiagram()
        self._motor = _MotorView()
        top.addWidget(self._foc, 5)
        top.addWidget(self._motor, 2)
        root.addLayout(top, 3)
        self._top_layout = top   # 供 set_top_diagram 替换上图

        # 布局编辑/导出按钮: 作为 FOC 框图的子控件, 浮于其左下角
        self._btn_edit = QPushButton("布局编辑: 关", self._foc)
        self._btn_edit.setCheckable(True)
        self._btn_edit.setFixedHeight(22)
        self._btn_edit.toggled.connect(self._on_edit_toggled)
        self._btn_export = QPushButton("导出布局", self._foc)
        self._btn_export.setFixedHeight(22)
        self._btn_export.clicked.connect(self._on_export_layout)
        self._foc.set_corner_buttons(self._btn_edit, self._btn_export)
        # 默认隐藏, 仅"菜单配置 > 布局编辑"开启后显示
        self._btn_edit.setVisible(False)
        self._btn_export.setVisible(False)

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
        self._card_motion = _ParamCard("位置 / 角度 / 速度", [
            ("电机位置", "pos", "{:+.4f}", "rad"),
            ("电机速度", "vel", "{:+.3f}", "rad/s"),
            ("机械角度", "_mech_deg", "{:.1f}", "°"),
            ("电角度", "_elec_deg", "{:.1f}", "°"),
            ("多圈计数", "multiturn", "{}", ""),
        ])
        self._card_state = _ParamCard("状态 / 故障", [
            ("顶层状态", "_fsm", "{}", ""),
            ("运行子态", "_run", "{}", ""),
            ("运动子态", "_motion", "{}", ""),
            ("控制模式", "_ctrl", "{}", ""),
            ("使能", "_en", "{}", ""),
            ("故障掩码", "_fault", "{}", ""),
            ("警告掩码", "_warn", "{}", ""),
        ])

        cards.addWidget(self._card_bus, 1)
        cards.addWidget(self._card_motion, 1)
        cards.addWidget(self._card_state, 1)
        root.addLayout(cards, 2)

    def _build_poll_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("fbPollBar")
        bar.setFixedHeight(46)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 12, 0)
        lay.setSpacing(10)

        self._dot = QLabel("●")
        self._dot.setStyleSheet("font-size:14px; border:none;")
        self._dot_col = None  # 缓存当前颜色, 避免每帧 setStyleSheet
        lay.addWidget(self._dot)

        self._lbl_state = QLabel("未连接")
        self._lbl_state_col = None  # 缓存当前颜色
        lay.addWidget(self._lbl_state)

        lay.addStretch()

        # P2-5: CAN 模式提示(默认隐藏, 主窗口连接 CAN 后显示)
        self._lbl_can_hint = QLabel("CAN 模式: 电机可能主动推送反馈, 轮询可关闭")
        self._lbl_can_hint.setStyleSheet(
            "color: #F9A825; font-size: 11px; border: none;")
        self._lbl_can_hint.setVisible(False)
        lay.addWidget(self._lbl_can_hint)

        self._chk_poll = QCheckBox("周期请求状态")
        self._chk_poll.setStyleSheet("border:none;")
        self._chk_poll.setChecked(True)
        self._chk_poll.toggled.connect(self._apply_poll_settings)
        lay.addWidget(self._chk_poll)

        self._lbl_period = QLabel("周期")
        self._lbl_period.setStyleSheet("border:none;")
        lay.addWidget(self._lbl_period)

        self._spin_period = QSpinBox()
        self._spin_period.setRange(50, 5000)
        self._spin_period.setSingleStep(50)
        self._spin_period.setValue(200)
        self._spin_period.setSuffix(" ms")
        self._spin_period.setFixedWidth(86)
        self._spin_period.valueChanged.connect(self._apply_poll_settings)
        lay.addWidget(self._spin_period)

        self._apply_poll_settings()
        return bar

    # ---- 布局编辑(菜单门控) ----
    def set_layout_edit(self, on: bool):
        """由主窗口"菜单配置 > 布局编辑"门控: 显示/隐藏编辑按钮; 关闭时退出编辑。"""
        self._btn_edit.setVisible(on)
        self._btn_export.setVisible(on)
        if not on:
            self._btn_edit.setChecked(False)
            self._foc.set_edit_mode(False)

    def load_layout(self):
        """启动时加载 FOC 布局(resources/ui_layout.json 的 foc 节)。"""
        self._foc.apply_dict(layout_store.load_section("foc"))

    def apply_poll_config(self, enabled: bool, period_ms: int):
        self._chk_poll.setChecked(bool(enabled))
        self._spin_period.setValue(max(50, int(period_ms)))
        self._apply_poll_settings()

    def get_poll_period(self) -> int:
        return int(self._spin_period.value())

    def apply_theme(self):
        """主题切换: 重建本面板内联样式 + 重绘自绘图。"""
        self.setStyleSheet(f"QWidget#fbRoot {{ background:{theme.hex('app_bg')}; }}")
        self._poll_bar.setStyleSheet(
            f"QFrame#fbPollBar {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, "
            f"stop:0 {theme.hex('card_top')}, stop:1 {theme.hex('card_bottom')}); "
            f"border:1px solid {theme.hex('border')}; border-radius:8px; }}")
        border_css = f"border:1px solid {theme.hex('border')}; border-radius:8px;"
        self._foc.setStyleSheet(border_css)
        self._motor.setStyleSheet(border_css)
        btn_css = (
            f"QPushButton{{background:{theme.hex('input_bg')};color:{theme.hex('text')};"
            f"border:1px solid {theme.hex('input_border')};border-radius:4px;padding:0 8px;font-size:11px;}}"
            f"QPushButton:checked{{background:{theme.hex('warn')};color:#FFFFFF;font-weight:bold;}}")
        self._btn_edit.setStyleSheet(btn_css)
        self._btn_export.setStyleSheet(btn_css)
        for card in (self._card_bus, self._card_motion, self._card_state):
            card.apply_theme()
        self._foc.update()
        self._motor.update()
        if self._state_view is not None:   # 状态机图配色走 _ThemeProxy 动态取色
            self._state_view.update()

    def _on_edit_toggled(self, on: bool):
        self._foc.set_edit_mode(on)
        self._btn_edit.setText("布局编辑: 开" if on else "布局编辑: 关")

    def _on_export_layout(self):
        data = self._foc.export_dict()
        ok = layout_store.save("foc", data)
        import json
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QPlainTextEdit, QLabel, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("FOC 布局已保存")
        dlg.resize(520, 360)
        lay = QVBoxLayout(dlg)
        tip = "已保存到 " + layout_store.path() if ok else "保存失败(检查目录写权限)"
        lay.addWidget(QLabel(tip + "\n下次启动自动加载。当前 FOC 布局:"))
        edit = QPlainTextEdit(json.dumps(data, ensure_ascii=False, indent=2))
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

        # 机械角度(单圈 0~360°) + 电角度(0~360°): 由 single + pole_pairs + elec_bias 计算。
        # theta_e = (theta_m * pp + bias) % 2π, 而 theta_m ≡ single (mod 2π),
        # 故 theta_e = (single * pp + bias) % 2π, 与物理引擎一致且适用真机固件。
        # 位置死区滤波: 静止时锁定 single, 避免编码器 0.01rad 噪声导致数值/示意图抖动;
        # 数值卡片与示意图共用同一滤波结果, 保证显示一致。
        # 关键: 锁定时同步把速度置零! 否则 _MotorView._tick() 的航位推算会用噪声速度
        # (位置差分得到, 0.01rad@50Hz → ±0.5rad/s)持续推进显示角度, 拉回系数又追不上,
        # 形成"漂移-拉回"的低频摆动, 把位置噪声放大成肉眼可见的指针晃动
        raw_single = float(getattr(fb, "single", 0.0) or 0.0)
        vel = float(getattr(fb, "vel", 0.0) or 0.0)
        single = self._single_filter.update(raw_single, vel)
        disp_vel = 0.0 if self._single_filter.is_locked else vel
        mech_deg = math.degrees(single) % 360.0
        elec_rad = (single * self._pole_pairs + self._elec_bias) % (2.0 * math.pi)
        elec_deg = math.degrees(elec_rad) % 360.0
        self._card_motion.set_text("_mech_deg", f"{mech_deg:.1f}")
        self._card_motion.set_text("_elec_deg", f"{elec_deg:.1f}")
        # 速度卡片: 锁定时置零, 避免噪声速度跳动
        self._card_motion.set_text("vel", f"{disp_vel:+.3f}")
        # pos(多圈位置) 与 multiturn: 锁定时保持上次值不变, 避免编码器噪声导致 pos
        # 末位抖动和 multiturn int 跳变 ±1。pos 与 single/multiturn 的关系:
        # pos = multiturn * 2π + single, 锁定时 single 已锁定, pos/multiturn 也应锁定。
        if not self._single_filter.is_locked:
            v_pos = getattr(fb, "pos", None)
            if v_pos is not None:
                self._disp_pos = float(v_pos)
            v_mt = getattr(fb, "multiturn", None)
            if v_mt is not None:
                self._disp_multiturn = int(v_mt)
        self._card_motion.set_text("pos", f"{self._disp_pos:+.4f}")
        self._card_motion.set_text("multiturn", str(self._disp_multiturn))

        # 电机旋转示意(滤波后的单圈角 + 静止置零的速度 + iq)
        self._motor.set_feedback(
            single, disp_vel, getattr(fb, "iq", 0.0), self._enabled)

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
            "thw": f"{single:.3f}rad / {disp_vel:+.2f}",
        })

        # 故障/警告: 显示十六进制 + 解码描述(故障时加载完整中文信息)
        fault_mask = fb.fault_mask
        fault_lbl = self._card_state._val_labels.get("_fault")
        if fault_mask:
            full_desc = fault_mask_to_str(fault_mask)
            # 单行过长时截断, 完整描述见故障信息 tab + tooltip
            short = full_desc if len(full_desc) <= 24 else full_desc[:22] + "..."
            self._card_state.set_text(
                "_fault", f"0x{fault_mask:04X} {short}", "#FF6B6B")
            if fault_lbl is not None:
                fault_lbl.setToolTip(f"故障掩码 0x{fault_mask:04X}\n{full_desc}\n详见 [故障信息] tab")
        else:
            self._card_state.set_text("_fault", "0x0000 无故障", "#7FD4FF")
            if fault_lbl is not None:
                fault_lbl.setToolTip("无故障")
        self._card_state.set_text(
            "_warn", f"0x{fb.warn_mask:04X}",
            "#FFB454" if fb.warn_mask else "#7FD4FF")

        # 运动子状态 (twin 扩展, 反映 STANDSTILL/MOVING/DECEL/HOLDING/BRAKING)
        ms = getattr(fb, "motion_state", 0)
        # 不同状态用不同颜色: 静止=灰, 运动=蓝, 减速=橙, 保持=绿
        motion_col = {0: "#8890A4", 1: "#5F9EFF", 2: "#FFB454",
                      3: "#5FE6AC", 4: "#FF6B6B"}.get(int(ms), "#7FD4FF")
        self._card_state.set_text("_motion", motion_state_name(ms), motion_col)

        self._refresh_state_styles()
        self._foc.flush()  # FOC 框图脏标记刷新(避免 set_values 每次直接 update)

    def set_motor_params(self, params: dict):
        """电机本体参数(r/ld/lq/flux/kt/ke/pole_pairs), 转发到转子示意图下方显示。"""
        self._motor.set_motor_params(params)
        pp = int(params.get("pole_pairs", 0) or 0)
        if pp > 0:
            self._pole_pairs = pp
        self._elec_bias = float(params.get("elec_angle_bias", 0.0) or 0.0)

    def update_state(self, top_fsm, run_state, ctrl_mode, enable):
        self._last_state = (top_fsm, run_state, ctrl_mode, enable)
        self._enabled = bool(enable)
        self._motor.set_enabled(self._enabled)
        if self._state_view is not None:
            self._state_view.apply_state(top_fsm, run_state, ctrl_mode, enable)
        is_fault = top_fsm in (1, 2)
        self._card_state.set_text("_fsm", top_fsm_name(top_fsm),
                                  "#FF6B6B" if is_fault else "#5FE6AC")
        self._card_state.set_text("_run", run_state_name(run_state))
        self._card_state.set_text("_ctrl", cmd_name(ctrl_mode))
        self._card_state.set_text("_en", "ON" if enable else "OFF",
                                  "#5FE6AC" if enable else "#8890A4")
        self._refresh_state_styles()

    def _refresh_state_styles(self):
        top, run_state, _ctrl_mode, enable = self._last_state
        if top is None:
            dot, col = theme.hex("muted"), theme.hex("muted")
            text = "未连接"
        elif top in (1, 2):
            dot = col = theme.hex("danger")
            text = (f"{top_fsm_name(top)}   ·   运行子态 {run_state_name(run_state)}"
                    f"   ·   使能 {'ON' if enable else 'OFF'}")
        else:
            dot = col = theme.hex("accent")
            text = (f"{top_fsm_name(top)}   ·   运行子态 {run_state_name(run_state)}"
                    f"   ·   使能 {'ON' if enable else 'OFF'}")
        # 仅颜色变化时才重设样式表(避免每帧 setStyleSheet)
        if self._dot_col != dot:
            self._dot_col = dot
            self._dot.setStyleSheet(f"color:{dot}; font-size:14px; border:none;")
        if self._lbl_state_col != col:
            self._lbl_state_col = col
            self._lbl_state.setStyleSheet(
                f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 13px; "
                f"font-weight: bold; color: {col}; border: none;")
        self._lbl_state.setText(text)

    def set_link_active(self, active: bool):
        if not active:
            self._last_state = (None, None, None, None)
            self._enabled = False
            self._single_filter.reset(0.0)   # 断开时重置死区, 避免重连后锁定到旧值
            self._disp_pos = 0.0
            self._disp_multiturn = 0
            self._motor.set_enabled(False)
            if self._state_view is not None:
                self._state_view.apply_state(None, None, None, None)   # 清空高亮
            self._refresh_state_styles()

    def set_can_mode(self, enabled: bool):
        """P2-5: CAN 模式提示显隐。

        CAN 模式下电机可能主动推送反馈帧(MIT/READ_FEEDBACK), 无需主窗口周期轮询;
        串口模式下反馈需主窗口主动 READ_FEEDBACK 拉取。提示用户可关闭轮询。
        """
        self._lbl_can_hint.setVisible(enabled)

    def set_top_diagram(self, kind: str):
        """上图视图切换: 'foc' 电流环框图(默认) / 'top_fsm' 主状态机。

        测试员版用主状态机替代 FOC 框图, 状态数据仍走 update_state 分发;
        布局配置与开发版状态机 Tab 共享 ui_layout.json 的 top_fsm 节。
        """
        want_fsm = (kind == 'top_fsm')
        if want_fsm and self._state_view is None:
            from ui.panels.state_machine_panel import TopFsmView
            self._foc.setVisible(False)
            self._state_view = TopFsmView()
            self._state_view.apply_dict(layout_store.load_section("top_fsm"))
            self._top_layout.replaceWidget(self._foc, self._state_view)
            self._top_layout.setStretchFactor(self._state_view, 5)
        elif not want_fsm and self._state_view is not None:
            self._state_view.setVisible(False)
            self._top_layout.replaceWidget(self._state_view, self._foc)
            self._top_layout.setStretchFactor(self._foc, 5)
            self._state_view = None
            self._foc.setVisible(True)

    def set_poll_controls_visible(self, visible: bool):
        """轮询控件(周期请求开关+周期输入)显隐。

        测试员版由主窗口统一轮询调度, 隐藏本面板自带控件并停止内部
        轮询定时器(poll_state 不再发出), 仅保留状态指示。
        """
        self._chk_poll.setVisible(visible)
        self._lbl_period.setVisible(visible)
        self._spin_period.setVisible(visible)
        if not visible and self._chk_poll.isChecked():
            self._chk_poll.setChecked(False)   # 触发 _apply_poll_settings 停止内部轮询

    def _apply_poll_settings(self):
        self._spin_period.setEnabled(not self._chk_poll.isChecked())
        self._restart_poll()

    def _restart_poll(self):
        self._poll_timer.stop()
        if self._chk_poll.isChecked() and self.isVisible():
            self._poll_timer.start(self._spin_period.value())

    def _on_poll_tick(self):
        if self._chk_poll.isChecked() and self.isVisible():
            self.poll_state.emit()

    def showEvent(self, evt):
        super().showEvent(evt)
        self._restart_poll()

    def hideEvent(self, evt):
        super().hideEvent(evt)
        self._poll_timer.stop()
