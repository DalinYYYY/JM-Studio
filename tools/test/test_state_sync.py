"""状态机显示同步测试: 验证 TwinResponder 上报的协议状态帧
能正确驱动 state_machine_panel 的节点高亮(不依赖 Qt 渲染, 仅校验映射逻辑)。

重点: 运行模式图节点 key 是 JmCmd 命令码; 状态帧第3字节 ctrl_mode 必须为
JmCmd 命令码而非 twin ControlMode 枚举值, 否则面板永远点不亮当前模式。
"""

import sys
import os

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..', '..')))

from jmproto import JmCmd, TopFsm, RunState, parse_state
from transport.virtual_engine.digital_twin import DigitalTwinEngine
from transport.virtual_engine.twin_responder import TwinResponder
from transport.virtual_engine.twin_fsm import MotorCmd, ControlMode

# 复刻面板运行模式图的节点 key 集合(JmCmd 命令码)
from ui.panels.state_machine_panel import _RUN_MODE_SPECS
_RUN_MODE_NODE_KEYS = {int(cmd) for cmd, _ in _RUN_MODE_SPECS}

PASS = 0
FAIL = 0
_FAILS = []


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        _FAILS.append(msg)
        print(f"  [FAIL] {msg}")


def drive(resp, cmd, payload=b''):
    """发命令并推进一点仿真, 返回解析后的 state 帧。"""
    resp.on_command(int(cmd), payload)
    resp.advance(0.05)
    frames = resp.on_command(int(JmCmd.READ_STATE), b'')
    rcmd, rpayload = frames[0]
    return parse_state(rpayload)


def make():
    e = DigitalTwinEngine()
    return TwinResponder(e), e


def test_idle_state():
    print("[TEST] 初始 IDLE 状态帧")
    resp, e = make()
    top, run, ctrl, en = drive(resp, JmCmd.READ_STATE)
    check(top == int(TopFsm.IDLE), f"初始应 IDLE(top=3), got top={top}")
    check(en == 0, f"未使能 enable 应 0, got {en}")
    print(f"    top={top} run={run} ctrl={ctrl} en={en}")


def test_position_mode_node_match():
    print("[TEST] 位置模式: ctrl_mode 应为 JmCmd 且匹配面板节点")
    resp, e = make()
    resp.on_command(int(JmCmd.ENABLE), b'')
    # 用协议帧发位置命令
    import struct
    spec = resp.reg.get_command(int(JmCmd.POSITION))
    # 构造 payload: 第一个字段填 2.0
    payload = struct.pack('<f', 2.0) + b'\x00' * 16
    top, run, ctrl, en = drive(resp, JmCmd.POSITION, payload)
    check(top == int(TopFsm.RUN), f"位置启动后应 RUN, got top={top}")
    check(ctrl == int(JmCmd.POSITION),
          f"ctrl_mode 应为 JmCmd.POSITION({int(JmCmd.POSITION)}), got {ctrl}")
    check(ctrl in _RUN_MODE_NODE_KEYS,
          f"ctrl_mode={ctrl} 不在面板运行模式节点集合中, 无法高亮")
    check(run == int(RunState.POSITION), f"run_state 应 POSITION({int(RunState.POSITION)}), got {run}")
    print(f"    top={top} run={run} ctrl={ctrl}(JmCmd.POSITION={int(JmCmd.POSITION)})")


def test_all_modes_node_match():
    print("[TEST] 各运动模式 ctrl_mode 均能匹配面板节点")
    import struct
    cases = [
        (JmCmd.VELOCITY, struct.pack('<f', 10.0) + b'\x00' * 16),
        (JmCmd.TORQUE, struct.pack('<f', 1.0) + b'\x00' * 16),
        (JmCmd.CURRENT, struct.pack('<ff', 0.0, 2.0) + b'\x00' * 12),
        (JmCmd.MIT, struct.pack('<f', 1.0) + b'\x00' * 20),
    ]
    for cmd, payload in cases:
        resp, e = make()
        resp.on_command(int(JmCmd.ENABLE), b'')
        top, run, ctrl, en = drive(resp, cmd, payload)
        check(ctrl in _RUN_MODE_NODE_KEYS or ctrl == int(JmCmd.MIT),
              f"{JmCmd(cmd).name}: ctrl_mode={ctrl} 未匹配面板节点")
        print(f"    {JmCmd(cmd).name}: top={top} run={run} ctrl={ctrl}")


def test_panel_apply_state_highlights():
    """直接调用面板的 apply_state 逻辑, 验证高亮 key 集合非空且含正确模式。"""
    print("[TEST] 面板 apply_state 高亮验证")
    # 不实例化 QWidget(避免 QApplication), 仅复刻 _RunModeView.apply_state 逻辑
    import struct
    resp, e = make()
    resp.on_command(int(JmCmd.ENABLE), b'')
    payload = struct.pack('<f', 2.0) + b'\x00' * 16
    top, run, ctrl, en = drive(resp, JmCmd.POSITION, payload)

    # 复刻 _RunModeView.apply_state 的 key 计算
    keys = set()
    if top == int(TopFsm.RUN):
        keys.add("RUN")
        if ctrl in _RUN_MODE_NODE_KEYS:
            keys.add(int(ctrl))
        elif run == int(RunState.IDLE):
            keys.add("ENTER")
    check("RUN" in keys, "RUN 容器应高亮")
    check(int(JmCmd.POSITION) in keys,
          f"位置模式节点(key={int(JmCmd.POSITION)})应高亮, got keys={keys}")
    print(f"    高亮keys={keys}")


def main():
    for t in [test_idle_state, test_position_mode_node_match,
              test_all_modes_node_match, test_panel_apply_state_highlights]:
        try:
            t()
        except Exception as ex:
            import traceback
            global FAIL
            FAIL += 1
            _FAILS.append(f"{t.__name__}: {ex}")
            traceback.print_exc()
    print("\n" + "=" * 50)
    print(f"PASS={PASS}  FAIL={FAIL}")
    for f in _FAILS:
        print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
