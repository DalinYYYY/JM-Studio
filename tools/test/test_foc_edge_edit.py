"""需求2: FOC 框图连线端点编辑测试。

验证:
1. _Edge 类结构与默认值
2. _FocDiagram._edges 为 _Edge 对象列表
3. _iter_edges() 返回 [(idx, p1_px, p2_px)] 像素坐标
4. _set_edge_endpoint() 写回 override 归一化坐标
5. _on_box_moved() 节点拖动时 override 端点跟随平移
6. export_dict() / apply_dict() edges 字段往返
7. _edit_mixin 默认空实现(状态机图 _DiagramView 不崩溃)
8. _hit_test 命中端点返回 ("edge_pt", "edge", (idx, end))
9. 缺 edges 节时所有连线用默认锚点(向后兼容)
"""

import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..', '..')))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QPointF, QEvent
from PyQt6.QtGui import QMouseEvent
from ui.panels.feedback_panel import _FocDiagram, _Edge
from ui.panels._edit_mixin import LayoutEditMixin

app = QApplication.instance() or QApplication(sys.argv)

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


# 构造框图并给尺寸(像素几何依赖 width/height)
diag = _FocDiagram()
diag.setGeometry(100, 100, 800, 500)


# ============ 1. _Edge 类结构 ============
print("\n[TEST 1] _Edge 类结构与默认值")
e0 = _Edge("ref", "R", "pi", "L", "fwd")
check(e0.from_key == "ref" and e0.from_side == "R" and e0.to_key == "pi"
      and e0.to_side == "L" and e0.kind == "fwd", "_Edge 基本字段")
check(e0.p1_override is None and e0.p2_override is None,
      "_Edge 默认 p1_override/p2_override 为 None")
# __slots__ 限制
check(hasattr(_Edge, "__slots__"), "_Edge 应有 __slots__")
try:
    e0.no_such = 1
    check(False, "_Edge __slots__ 应禁止额外属性")
except AttributeError:
    check(True, "_Edge __slots__ 禁止额外属性")


# ============ 2. _FocDiagram._edges 为 _Edge 列表 ============
print("\n[TEST 2] _FocDiagram._edges 为 _Edge 对象列表")
check(len(diag._edges) == 11, f"应有 11 条连线, 实际 {len(diag._edges)}")
check(all(isinstance(e, _Edge) for e in diag._edges),
      "所有连线应为 _Edge 对象")
check(all(e.p1_override is None and e.p2_override is None for e in diag._edges),
      "初始所有端点 override 为 None(用默认锚点)")


# ============ 3. _iter_edges 返回 [(idx, p1_px, p2_px)] ============
print("\n[TEST 3] _iter_edges 返回像素坐标元组")
edges = diag._iter_edges()
check(len(edges) == 11, f"_iter_edges 应返回 11 条, 实际 {len(edges)}")
check(all(len(item) == 3 for item in edges),
      "每项应为 (idx, p1, p2) 三元组")
check(all(isinstance(item[0], int) for item in edges),
      "idx 应为 int")
check(all(isinstance(item[1], QPointF) and isinstance(item[2], QPointF)
          for item in edges), "p1/p2 应为 QPointF")
# idx 连续 0..10
check([item[0] for item in edges] == list(range(11)),
      "idx 应为 0..10 连续")


# ============ 4. _set_edge_endpoint 写回 override ============
print("\n[TEST 4] _set_edge_endpoint 写回 override 归一化坐标")
diag._set_edge_endpoint(0, "p1", 0.15, 0.20)
check(diag._edges[0].p1_override == (0.15, 0.20),
      f"p1 override 应为 (0.15, 0.20), 实际 {diag._edges[0].p1_override}")
diag._set_edge_endpoint(0, "p2", 0.25, 0.30)
check(diag._edges[0].p2_override == (0.25, 0.30),
      f"p2 override 应为 (0.25, 0.30), 实际 {diag._edges[0].p2_override}")
# 其他 edge 不受影响
check(diag._edges[1].p1_override is None,
      "其他 edge 的 override 不受影响")
# 越界索引不崩溃
diag._set_edge_endpoint(999, "p1", 0.5, 0.5)
check(True, "越界索引不应崩溃")


# ============ 5. _on_box_moved 节点拖动时 override 跟随 ============
print("\n[TEST 5] _on_box_moved 节点拖动时 override 端点跟随平移")
# edge 0: ref -> pi, 已设 p1_override=(0.15, 0.20)
# 拖动 ref 节点, p1_override(以 ref 为 from)应跟随; p2_override(以 pi 为 to)不动
diag._on_box_moved("ref", 0.01, 0.02)
check(diag._edges[0].p1_override == (0.16, 0.22),
      f"拖动 ref 后 p1_override 应 +0.01/+0.02 = (0.16, 0.22), 实际 {diag._edges[0].p1_override}")
check(diag._edges[0].p2_override == (0.25, 0.30),
      f"拖动 ref 后 p2_override(pi 为 to) 不应变 (0.25, 0.30), 实际 {diag._edges[0].p2_override}")
# 拖动 pi 节点, p2_override(以 pi 为 to)应跟随
diag._on_box_moved("pi", 0.03, 0.0)
check(diag._edges[0].p2_override == (0.28, 0.30),
      f"拖动 pi 后 p2_override 应 +0.03 = (0.28, 0.30), 实际 {diag._edges[0].p2_override}")
# 未 override 的端点不跟随(它们本就由块边锚点决定)
check(diag._edges[1].p1_override is None,
      "未 override 的端点不受 _on_box_moved 影响")


# ============ 6. export_dict / apply_dict edges 往返 ============
print("\n[TEST 6] export_dict / apply_dict edges 字段往返")
exported = diag.export_dict()
check("edges" in exported, "export_dict 应含 edges 字段")
check("0" in exported["edges"], "edge 0 有 override 应被导出")
check("p1" in exported["edges"]["0"] and "p2" in exported["edges"]["0"],
      "edge 0 导出应含 p1/p2")
# edge 1 无 override, 不应导出
check("1" not in exported["edges"], "edge 1 无 override 不应导出")

# 往返: 新建框图, apply_dict, 验证 override 还原
diag2 = _FocDiagram()
diag2.setGeometry(100, 100, 800, 500)
check(diag2._edges[0].p1_override is None, "新框图初始无 override")
diag2.apply_dict(exported)
check(diag2._edges[0].p1_override == diag._edges[0].p1_override,
      f"apply_dict 后 p1_override 应还原 = {diag._edges[0].p1_override}, 实际 {diag2._edges[0].p1_override}")
check(diag2._edges[0].p2_override == diag._edges[0].p2_override,
      f"apply_dict 后 p2_override 应还原 = {diag._edges[0].p2_override}, 实际 {diag2._edges[0].p2_override}")
check(diag2._edges[1].p1_override is None,
      "edge 1 无 override, apply_dict 后仍为 None")


# ============ 7. _edit_mixin 默认空实现 ============
print("\n[TEST 7] _edit_mixin 默认空实现(状态机图不崩溃)")
from ui.panels.state_machine_panel import _TopFsmView
sm = _TopFsmView()
check(list(sm._iter_edges()) == [], "_TopFsmView 默认 _iter_edges 返回 []")
# 调用空实现不崩溃
sm._set_edge_endpoint(0, "p1", 0.5, 0.5)
sm._on_box_moved("x", 0.1, 0.1)
check(True, "_TopFsmView 空实现 _set_edge_endpoint/_on_box_moved 不崩溃")


# ============ 8. _hit_test 命中端点 ============
print("\n[TEST 8] _hit_test 命中端点返回 edge_pt")
diag.set_edit_mode(True)
edges_px = diag._iter_edges()
# edge 0 的 p1 像素坐标
idx0, p1_0, p2_0 = edges_px[0]
# 在 p1 附近做命中测试
mode, kind, key = diag._hit_test(p1_0)
check(mode == "edge_pt", f"命中 p1 端点 mode 应为 edge_pt, 实际 {mode}")
check(kind == "edge", f"命中 kind 应为 edge, 实际 {kind}")
check(key == (0, "p1"), f"命中 key 应为 (0, 'p1'), 实际 {key}")
# 在 p2 附近
mode2, _, key2 = diag._hit_test(p2_0)
check(mode2 == "edge_pt" and key2 == (0, "p2"),
      f"命中 p2 端点应为 (0, 'p2'), 实际 {key2}")


# ============ 9. 缺 edges 节时向后兼容 ============
print("\n[TEST 9] 缺 edges 节时所有连线用默认锚点(向后兼容)")
diag3 = _FocDiagram()
diag3.setGeometry(100, 100, 800, 500)
# 旧版布局(无 edges 字段)
legacy = {
    "blocks": {"ref": [0.005, 0.06, 0.12, 0.2]},
    "annots": {"vbus": [0.665, 0.32]},
}
diag3.apply_dict(legacy)
check(all(e.p1_override is None and e.p2_override is None for e in diag3._edges),
      "缺 edges 节时所有连线 override 应保持 None")


# ============ 10. 需求2: 增大命中区(8→14px), 端点偏移 10px 也能命中 ============
print("\n[TEST 10] 需求2: 增大命中区, 端点偏移 10px 也能命中")
from ui.panels._edit_mixin import _EDGE_HANDLE_PX
check(_EDGE_HANDLE_PX >= 14.0, f"_EDGE_HANDLE_PX 应 >=14, 实际 {_EDGE_HANDLE_PX}")
# edge 0 的 p1 像素坐标, 偏移 10px(原 8px 半径会 miss, 现 14px 会 hit)
idx0, p1_0, p2_0 = diag._iter_edges()[0]
offset_pos = QPointF(p1_0.x() + 10.0, p1_0.y())
mode, _k, key = diag._hit_test(offset_pos)
check(mode == "edge_pt", f"偏移 10px 应命中端点(14px 命中区), 实际 mode={mode}")
check(key == (0, "p1"), f"偏移命中 key 应为 (0,'p1'), 实际 {key}")


# ============ 11. 需求2: _hover_edge 悬停态 ============
print("\n[TEST 11] 需求2: _hover_edge 悬停态初始为 None, set_edit_mode 清空")
diag4 = _FocDiagram()
diag4.setGeometry(100, 100, 800, 500)
diag4._ensure_edit_state()
check(getattr(diag4, "_hover_edge", "MISS") is None, "_hover_edge 初始应为 None")
# set_edit_mode(False) 清空
diag4._hover_edge = (0, "p1")
diag4.set_edit_mode(False)
check(diag4._hover_edge is None, "set_edit_mode(False) 应清空 _hover_edge")


print(f"\n{'='*60}")
print(f"PASS={PASS}  FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
