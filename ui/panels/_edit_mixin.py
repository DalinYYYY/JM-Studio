"""自绘图的布局编辑能力(公共 Mixin)。

FOC 框图(_FocDiagram)与状态机图(_DiagramView)绘制不同但编辑逻辑一致, 故抽到此
Mixin 复用: 拖动方框 / 右下角手柄缩放方框 / 拖动文字标注 / 拖动连线端点。
开启编辑模式后叠加网格与坐标提示。坐标全部归一化(0~1)。

子类需提供:
  _edit_geom() -> (ox, oy, w, h)         画布内容区像素几何(与 paintEvent 一致)
  _iter_boxes() -> list[(key, obj)]      可拖动/缩放的方框对象(含 .x/.y/.w/.h)
  _iter_labels() -> list[(key, x, y)]    可拖动的文字标注归一化坐标
  _set_label_pos(key, nx, ny)            写回标注坐标
可选覆盖(连线端点编辑, 默认空实现):
  _iter_edges() -> list[(idx, p1_px, p2_px)]   可拖动端点的连线(端点像素坐标)
  _set_edge_endpoint(idx, end, nx, ny)         写回端点 override 归一化坐标(end='p1'/'p2')
  _on_box_moved(key, dx, dy)                   节点拖动后连线端点跟随平移
可选覆盖:
  _box_min_w / _box_min_h                方框最小尺寸(默认 0.04)
绘制时子类在 paintEvent 末尾调用 self.draw_edit_overlay(p)。
"""

import math

from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QPen, QColor, QPainterPath, QFont

_GRID = 0.005          # 网格吸附步长
_HANDLE_PX = 12.0      # 右下角缩放手柄命中区(像素)
_EDGE_HANDLE_PX = 8.0  # 连线端点手柄命中半径(像素)
_MIN_W = 0.04
_MIN_H = 0.04


def _font(pt, bold=False):
    f = QFont()
    f.setFamilies(["Microsoft YaHei", "PingFang SC", "Segoe UI", "sans-serif"])
    f.setPointSize(pt)
    f.setBold(bold)
    return f


class LayoutEditMixin:
    """拖动/缩放方框 + 拖动标注 的通用编辑逻辑。"""

    # ---- 编辑状态(在子类 __init__ 后惰性初始化) ----
    def _ensure_edit_state(self):
        if not hasattr(self, "_edit"):
            self._edit = False
            self._drag_key = None
            self._drag_mode = None      # 'move' / 'resize' / 'label'
            self._drag_off = (0.0, 0.0)

    @property
    def _box_min_w(self):
        return _MIN_W

    @property
    def _box_min_h(self):
        return _MIN_H

    # ---- 对外开关 ----
    def set_edit_mode(self, on: bool):
        self._ensure_edit_state()
        self._edit = bool(on)
        self._drag_key = None
        self._drag_mode = None
        self.setMouseTracking(self._edit)
        self.setCursor(Qt.CursorShape.OpenHandCursor if self._edit else Qt.CursorShape.ArrowCursor)
        self.update()

    def is_edit_mode(self) -> bool:
        return getattr(self, "_edit", False)

    # ---- 命中测试 ----
    def _box_rect_px(self, obj, geom):
        ox, oy, w, h = geom
        return QRectF(ox + obj.x * w, oy + obj.y * h, obj.w * w, obj.h * h)

    def _hit_test(self, pos):
        """返回 (mode, kind, key)。优先级: 连线端点 > 缩放手柄 > 方框体 > 标注。"""
        geom = self._edit_geom()
        # 0) 连线端点(优先于方框, 因端点常贴在方框边上)
        for idx, p1, p2 in self._iter_edges():
            if math.hypot(pos.x() - p1.x(), pos.y() - p1.y()) <= _EDGE_HANDLE_PX:
                return ("edge_pt", "edge", (idx, "p1"))
            if math.hypot(pos.x() - p2.x(), pos.y() - p2.y()) <= _EDGE_HANDLE_PX:
                return ("edge_pt", "edge", (idx, "p2"))
        # 1) 方框(逆序: 上层优先), 先判右下角手柄
        boxes = list(self._iter_boxes())
        for key, obj in reversed(boxes):
            r = self._box_rect_px(obj, geom)
            handle = QRectF(r.right() - _HANDLE_PX, r.bottom() - _HANDLE_PX, _HANDLE_PX, _HANDLE_PX)
            if handle.contains(pos):
                return ("resize", "box", key)
        for key, obj in reversed(boxes):
            r = self._box_rect_px(obj, geom)
            if r.contains(pos):
                return ("move", "box", key)
        # 2) 标注(命中区按估算尺寸)
        ox, oy, w, h = geom
        for key, lx, ly in self._iter_labels():
            cx, cy = ox + lx * w, oy + ly * h
            if abs(pos.x() - cx) <= 42 and abs(pos.y() - cy) <= 14:
                return ("label", "label", key)
        return (None, None, None)

    # ---- 鼠标 ----
    def mousePressEvent(self, e):
        self._ensure_edit_state()
        if not self._edit:
            return super().mousePressEvent(e)
        mode, _kind, key = self._hit_test(e.position())
        geom = self._edit_geom()
        ox, oy, w, h = geom
        if mode == "edge_pt":
            # key = (idx, "p1"/"p2")
            self._drag_key, self._drag_mode = key, "edge_pt"
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif mode in ("move", "resize"):
            obj = dict(self._iter_boxes()).get(key)
            self._drag_key, self._drag_mode = key, mode
            if mode == "move":
                self._drag_off = (e.position().x() - (ox + obj.x * w),
                                  e.position().y() - (oy + obj.y * h))
            self.setCursor(Qt.CursorShape.ClosedHandCursor if mode == "move"
                           else Qt.CursorShape.SizeFDiagCursor)
        elif mode == "label":
            self._drag_key, self._drag_mode = key, "label"
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.update()

    def mouseMoveEvent(self, e):
        self._ensure_edit_state()
        if not self._edit:
            return
        ox, oy, w, h = self._edit_geom()
        # 悬停光标提示(未拖动时)
        if self._drag_mode is None:
            mode, _k, _key = self._hit_test(e.position())
            self.setCursor({"resize": Qt.CursorShape.SizeFDiagCursor,
                            "move": Qt.CursorShape.OpenHandCursor,
                            "label": Qt.CursorShape.PointingHandCursor,
                            "edge_pt": Qt.CursorShape.PointingHandCursor}.get(
                               mode, Qt.CursorShape.ArrowCursor))
            return
        if self._drag_mode == "edge_pt":
            idx, end = self._drag_key
            nx = (e.position().x() - ox) / w
            ny = (e.position().y() - oy) / h
            self._set_edge_endpoint(idx, end,
                                    _clamp(_snap(nx), 0.0, 1.0),
                                    _clamp(_snap(ny), 0.0, 1.0))
            self.update()
            return
        if self._drag_mode == "label":
            nx = (e.position().x() - ox) / w
            ny = (e.position().y() - oy) / h
            self._set_label_pos(self._drag_key,
                                _clamp(_snap(nx), 0.0, 1.0), _clamp(_snap(ny), 0.0, 1.0))
            self.update()
            return
        obj = dict(self._iter_boxes()).get(self._drag_key)
        if obj is None:
            return
        if self._drag_mode == "move":
            nx = (e.position().x() - self._drag_off[0] - ox) / w
            ny = (e.position().y() - self._drag_off[1] - oy) / h
            new_x = _clamp(_snap(nx), 0.0, 1.0 - obj.w)
            new_y = _clamp(_snap(ny), 0.0, 1.0 - obj.h)
            dx = new_x - obj.x
            dy = new_y - obj.y
            obj.x = new_x
            obj.y = new_y
            # 拖动节点时, 已 override 的连线端点跟随平移("跟随节点"行为)
            if dx != 0 or dy != 0:
                self._on_box_moved(self._drag_key, dx, dy)
        else:  # resize
            nw = (e.position().x() - (ox + obj.x * w)) / w
            nh = (e.position().y() - (oy + obj.y * h)) / h
            obj.w = _clamp(_snap(nw), self._box_min_w, 1.0 - obj.x)
            obj.h = _clamp(_snap(nh), self._box_min_h, 1.0 - obj.y)
        self.update()

    def mouseReleaseEvent(self, e):
        self._ensure_edit_state()
        if not self._edit:
            return super().mouseReleaseEvent(e)
        self._drag_key = None
        self._drag_mode = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()

    # ---- 编辑叠加层 ----
    def draw_edit_overlay(self, p):
        self._ensure_edit_state()
        if not self._edit:
            return
        ox, oy, w, h = self._edit_geom()
        # 网格
        p.setPen(QPen(QColor(255, 255, 255, 16), 1.0))
        for i in range(0, 21):
            x = ox + w * i / 20.0
            p.drawLine(QPointF(x, oy), QPointF(x, oy + h))
            y = oy + h * i / 20.0
            p.drawLine(QPointF(ox, y), QPointF(ox + w, y))
        # 每框: 坐标数字 + 右下角缩放手柄
        p.setFont(_font(7))
        for key, obj in self._iter_boxes():
            r = self._box_rect_px(obj, (ox, oy, w, h))
            p.setPen(QColor("#FFD080"))
            p.drawText(QRectF(r.x(), r.bottom() + 1, max(60.0, r.width()), 12),
                       Qt.AlignmentFlag.AlignHCenter,
                       f"{obj.x:.3f},{obj.y:.3f} · {obj.w:.3f}×{obj.h:.3f}")
            # 手柄小三角
            hp = QPainterPath()
            hp.moveTo(r.right(), r.bottom() - _HANDLE_PX)
            hp.lineTo(r.right(), r.bottom())
            hp.lineTo(r.right() - _HANDLE_PX, r.bottom())
            hp.closeSubpath()
            p.fillPath(hp, QColor("#FFD080"))
        # 标注命中点
        for key, lx, ly in self._iter_labels():
            cx, cy = ox + lx * w, oy + ly * h
            p.setPen(QPen(QColor("#7FD4FF"), 1.0))
            p.setBrush(QColor(127, 212, 255, 60))
            p.drawEllipse(QPointF(cx, cy), 4, 4)
        # 连线端点手柄(可拖动调整箭头起终点)
        for idx, p1, p2 in self._iter_edges():
            for pt in (p1, p2):
                p.setPen(QPen(QColor("#FFB07A"), 1.4))
                p.setBrush(QColor(255, 176, 122, 120))
                p.drawEllipse(pt, 5, 5)
        # 提示
        p.setPen(QColor("#FFD080"))
        p.setFont(_font(8, bold=True))
        p.drawText(QRectF(ox, oy - 2, w, 14), Qt.AlignmentFlag.AlignRight,
                   "编辑: 拖方框移动·拖右下角改大小·拖圆点移文字/连线端点, 完成点[导出布局]")

    # ---- 子类需实现(默认空实现, 避免误用崩溃) ----
    def _edit_geom(self):
        return (0.0, 0.0, max(1.0, self.width()), max(1.0, self.height()))

    def _iter_boxes(self):
        return []

    def _iter_labels(self):
        return []

    def _set_label_pos(self, key, nx, ny):
        pass

    # ---- 连线编辑(可选: 子类如 _FocDiagram 覆盖以支持端点拖动) ----
    def _iter_edges(self):
        """返回 [(idx, p1_px, p2_px)]; 默认空, 子类覆盖以启用端点编辑。"""
        return []

    def _set_edge_endpoint(self, idx, end, nx, ny):
        """写回端点 override 归一化坐标; end='p1'/'p2'。默认空实现。"""
        pass

    def _on_box_moved(self, key, dx, dy):
        """节点拖动后通知连线跟随; 默认空实现。"""
        pass


def _snap(v):
    return round(v / _GRID) * _GRID


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))
