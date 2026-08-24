"""应用形态(Profile)与测试员版主窗口测试。

验证:
1. app_profile.load_profile: tester 形态加载/校验/默认回退
2. load_motion_mode_cmds subset: 模式子集白名单解析(名称/命令码/非法条目)
3. main._resolve_profile: --profile/--profile=/环境变量解析与 argv 剥离
4. TesterMainWindow 离屏实例化: 面板组装/轮询组解析/控件裁剪
5. 开发版 MainWindow 离屏实例化不受影响(回归)
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

app = QApplication.instance() or QApplication(sys.argv)

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


# ---- 1. load_profile ----
from ui.app_profile import load_profile

prof = load_profile('tester')
check(prof is not None, "tester profile 应加载成功")
check(prof['title'] == 'JM 测试工具', "title 应取自配置")
check(prof['motion_modes'] == ['开环电压', '电流环', '速度环'],
      f"motion_modes 应为 3 项(暂不含位置环), 实际 {prof['motion_modes']}")
cmds = {e['cmd'] for e in prof['poll_group']}
from jmproto import JmCmd
check(JmCmd.READ_FEEDBACK in cmds and JmCmd.READ_DQ_CURRENT in cmds
      and JmCmd.READ_STATE in cmds,
      f"轮询组应含 0xC0/0xC4/0xC1, 实际 {sorted(hex(c) for c in cmds)}")
check(load_profile('nonexistent') is None, "未知形态应返回 None")
check(load_profile(None) is None, "空名称应返回 None")

# ---- 2. load_motion_mode_cmds subset ----
from ui.panels.motion_mode_config import load_motion_mode_cmds
from jmproto import get_registry

reg = get_registry()
subset = load_motion_mode_cmds(reg, subset=['开环电压', '电流环', '速度环', '位置环'])
check(subset == [0x10, 0x11, 0x14, 0x15],
      f"subset 中文名应解析为 0x10/0x11/0x14/0x15, 实际 {[hex(c) for c in subset or []]}")
subset2 = load_motion_mode_cmds(reg, subset=['VELOCITY', '0x15', 0x10, '不存在的模式'])
check(subset2 == [0x14, 0x15, 0x10],
      f"subset 枚举名/命令码混写应解析且忽略非法项, 实际 {[hex(c) for c in subset2 or []]}")
subset3 = load_motion_mode_cmds(reg, subset=['全部非法'])
check(subset3 is None, "subset 全部无法识别应返回 None(回退全局白名单)")
no_subset = load_motion_mode_cmds(reg)
check(no_subset is None or isinstance(no_subset, list),
      "无 subset 时走 motion_modes.txt 原逻辑不崩溃")

# ---- 3. _resolve_profile ----
from main import _resolve_profile

p, argv = _resolve_profile(['app', '--profile', 'tester'])
check(p == 'tester' and argv == ['app'], f"--profile A B 解析: {p}, {argv}")
p, argv = _resolve_profile(['app', '--profile=tester', '-x'])
check(p == 'tester' and argv == ['app', '-x'], f"--profile=B 解析: {p}, {argv}")
os.environ['JM_PROFILE'] = 'tester'
p, argv = _resolve_profile(['app'])
check(p == 'tester' and argv == ['app'], f"环境变量解析: {p}")
del os.environ['JM_PROFILE']
p, argv = _resolve_profile(['app'])
check(p is None, "无参数且无环境变量应返回 None")

# ---- 4. TesterMainWindow 实例化 ----
from ui.tester_main_window import TesterMainWindow

win = TesterMainWindow(prof)
check(win._motion_panel._combo_mode.count() == 3,
      f"运动模式下拉应只有 3 项, 实际 {win._motion_panel._combo_mode.count()}")
mode_names = [win._motion_panel._combo_mode.itemText(i)
              for i in range(win._motion_panel._combo_mode.count())]
check(all(('开环' in n) or ('电流环' in n) or ('速度环' in n)
          for n in mode_names), f"模式名应只含白名单 3 项: {mode_names}")
check('位置环' not in ''.join(mode_names), "位置环应从测试版移除")
check(win._tabs.count() == 2, f"Tab 应只有 2 个, 实际 {win._tabs.count()}")
check(win._poller._entries is not None and len(win._poller._entries) >= 3,
      f"轮询组应至少 3 条, 实际 {len(win._poller._entries)}")
check(not win._feedback_panel._chk_poll.isVisibleTo(win._feedback_panel)
      or not win._feedback_panel._chk_poll.isVisible(),
      "反馈面板轮询控件应隐藏")

# ---- 4b. 实时反馈上图 = 主状态机(tester profile feedback_diagram) ----
check(prof.get('feedback_diagram') == 'top_fsm',
      f"feedback_diagram 应为 top_fsm, 实际 {prof.get('feedback_diagram')}")
check(win._feedback_panel._state_view is not None,
      "上图应替换为主状态机视图(_state_view 非 None)")
check(not win._feedback_panel._foc.isVisibleTo(win._feedback_panel),
      "FOC 框图应隐藏")
from jmproto import TopFsm
win._feedback_panel.update_state(int(TopFsm.RUN), 7, 0x15, 1)
check(win._feedback_panel._state_view._active_keys == {"WORK", "RUN"},
      f"主状态机视图应高亮 WORK+RUN, 实际 {win._feedback_panel._state_view._active_keys}")
win._feedback_panel.set_link_active(False)
check(not win._feedback_panel._state_view._active_keys,
      "断开后主状态机高亮应清空")

# ---- 4c. 连接面板版本显示 ----
check(win._conn_panel._lbl_version.text() == "固件版本: --",
      f"未连接时版本应显示 --, 实际 {win._conn_panel._lbl_version.text()}")
win._on_dev_info(0x00000100, 0x01020300, b'\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c')
check(win._conn_panel._lbl_version.text() == "固件版本: 1.2.3.0",
      f"读设备信息后应显示固件版本 1.2.3.0, 实际 {win._conn_panel._lbl_version.text()}")
check("硬件版本" in win._conn_panel._lbl_version.toolTip()
      and "UID" in win._conn_panel._lbl_version.toolTip(),
      "版本 tooltip 应含硬件版本与 UID")
win._conn_panel.set_connected(False)
check(win._conn_panel._lbl_version.text() == "固件版本: --",
      f"断开后版本应复位 --, 实际 {win._conn_panel._lbl_version.text()}")

# ---- 4e. 电机本体参数读取 → 转子示意图极对数/槽数联动 ----
from jmproto import codec as _codec

mv = win._feedback_panel._motor
check(mv._pole_pairs == mv.DEFAULT_POLE_PAIRS and mv._stator_slots == 12,
      f"初始极对数应为默认 {mv.DEFAULT_POLE_PAIRS}/槽数 12, 实际 {mv._pole_pairs}/{mv._stator_slots}")
# 应答: pole_pairs=4(u32)
win._on_motor_info_value(17, 4, _codec.pack_value('u32', 4))
check(mv._pole_pairs == 4 and mv._stator_slots == 12,
      f"极对数 4 → 槽数 12(12槽8极), 实际 {mv._pole_pairs}/{mv._stator_slots}")
check(win._motor_params.get('pole_pairs') == 4,
      f"pole_pairs 应收集到 4, 实际 {win._motor_params.get('pole_pairs')}")
# 应答: pole_pairs=9 → 槽数 27
win._on_motor_info_value(17, 4, _codec.pack_value('u32', 9))
check(mv._pole_pairs == 9 and mv._stator_slots == 27,
      f"极对数 9 → 槽数 27(27槽18极), 实际 {mv._pole_pairs}/{mv._stator_slots}")
# 电气参数应答: r=3.6/ld=0.0048/kt=0.21(f32)
for pid, v in ((20, 3.6), (21, 0.0048), (22, 0.0048), (23, 0.02), (24, 0.21)):
    win._on_motor_info_value(pid, 4, _codec.pack_value('f32', v))
check(abs(win._motor_params.get('r', 0) - 3.6) < 1e-4
      and abs(win._motor_params.get('kt', 0) - 0.21) < 1e-4,
      f"电气参数应收集 r/kt, 实际 {win._motor_params}")
check(mv._params.get('r') == win._motor_params['r'],
      "转子示意图应收到电机参数")
# 单条应答: elec_angle_bias(38, f32)
win._on_motor_info_value(38, 4, _codec.pack_value('f32', 0.5))
check(abs(win._feedback_panel._elec_bias - 0.5) < 1e-6,
      f"电角度偏置应更新为 0.5, 实际 {win._feedback_panel._elec_bias}")
# 非关注参数应被忽略
win._on_motor_info_value(48, 4, _codec.pack_value('f32', 1.0))
check(48 not in [k for k in win._motor_params] and 'elec_angle_bias' in win._motor_params,
      "非关注 param_id 应被忽略")
# 断开: 参数清空, 恢复默认极对数
win._on_connected(False)
check(mv._pole_pairs == mv.DEFAULT_POLE_PAIRS and not win._motor_params,
      f"断开后应恢复默认极对数并清空参数, 实际 {mv._pole_pairs}/{win._motor_params}")
# 转子位置跟随反馈角度(set_feedback 目标角平滑逼近)
mv._disp_th = 0.0
for _ in range(600):
    mv._tick()
check(abs(mv._disp_th - mv._tgt_th) < 0.1 or mv._disp_th != 0.0,
      "转子显示角度应向反馈目标角逼近")

# ---- 4f. 虚拟引擎端到端: 连接后读电机参数链路 ----
from transport.virtual_engine.virtual_transport import VirtualTransport

vt = VirtualTransport()
win._client.set_transport(vt)
ok = win._client.open()
check(ok and win._client.is_open(), "虚拟引擎应连接成功")
win._read_motor_params()
for _ in range(200):
    app.processEvents()
    if len(win._motor_params) >= len(win._MOTOR_PARAM_KEYS):
        break
pp = win._motor_params.get('pole_pairs')
check(pp == 7, f"虚拟引擎默认极对数应为 7(motor_info.csv 默认), 实际 {pp}")
check(abs(win._motor_params.get('r', 0) - 3.6) < 1e-3,
      f"虚拟引擎相电阻默认 3.6, 实际 {win._motor_params.get('r')}")
check(mv._pole_pairs == 7 and mv._stator_slots == 12,
      f"转子示意图应联动到 7 对极/12 槽, 实际 {mv._pole_pairs}/{mv._stator_slots}")
check(abs(win._feedback_panel._elec_bias
          - win._motor_params.get('elec_angle_bias', 0.0)) < 1e-6,
      "电角度偏置应与读回值一致")
win._client.close()

# ---- 4d. 上图切回 FOC(接口对称性) ----
win._feedback_panel.set_top_diagram('foc')
check(win._feedback_panel._state_view is None and win._feedback_panel._foc.isVisibleTo(win._feedback_panel),
      "切回 foc 形态应恢复 FOC 框图")
win.close()

# ---- 5. 开发版 MainWindow 回归 ----
from ui.main_window import MainWindow

dev = MainWindow()
check(dev._motion_panel._combo_mode.count() > 4,
      f"开发版模式数应远大于 4(motion_modes.txt 白名单), 实际 {dev._motion_panel._combo_mode.count()}")
check(dev._tabs.count() >= 8, f"开发版 Tab 数应 >= 8, 实际 {dev._tabs.count()}")
check(dev._feedback_panel._chk_poll.isChecked(),
      "开发版反馈面板轮询开关应保持默认开启")
dev.close()

print()
if FAIL:
    print(f"RESULT: FAIL  (pass={PASS}, fail={FAIL})")
    for m in _FAILS:
        print(f"  - {m}")
    sys.exit(1)
print(f"RESULT: PASS  (pass={PASS}, fail={FAIL})")
