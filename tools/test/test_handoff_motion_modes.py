"""交接模式 GUI、载荷和经典 CAN 分包回归。直接运行此文件即可。"""

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtWidgets import QApplication

from jmproto.cmd_def import JmCmd, JmErr, RunState
from jmproto import profile_config as pc
from jmproto.registry import ProtocolRegistry, validate_motion_values
from core.motor_client import JmClient
from transport.base import Transport
from transport.can_transport import CanTransport, can_make_id
from transport.virtual_engine.twin_responder import TwinResponder
from ui.panels.motion_panel import MotionPanel


def _reject(action):
    try:
        action()
    except ValueError:
        return
    raise AssertionError('应拒绝无效运动参数')


def test_payloads(reg):
    pp = {'flags': 3, 'pos': -2.0, 'vel': 1.5, 'acc': 4.0,
          'dec': 5.0, 'jerk': 20.0}
    assert reg.pack_command(JmCmd.PP, {**pp, 'profile_rev': 7, 'seq': 8}) == struct.pack('<BfHB', 3, -2, 7, 8)
    _reject(lambda: reg.pack_command(JmCmd.PP, pp))  # 旧 21B 不能直接发送
    assert reg.pack_command(JmCmd.PV, {'vel': -3, 'acc': 4, 'dec': 6}) == struct.pack('<4f', -3, 4, 6, 0)
    assert reg.pack_command(JmCmd.PT, {'torque': 1, 'slope': 2,
                                      'current_lim': 3, 'vel_lim': 4}) == struct.pack('<4f', 1, 2, 3, 4)
    assert reg.pack_command(JmCmd.JOG, {'dir': -1, 'speed': 2,
                                       'acc': 3, 'dec': 4}) == struct.pack('<b3f', -1, 2, 3, 4)
    assert len(reg.pack_command(JmCmd.MIT, {})) == 20

    _reject(lambda: validate_motion_values(JmCmd.PP, {**pp, 'vel': 0}))
    _reject(lambda: validate_motion_values(JmCmd.PP, {**pp, 'jerk': 0}))
    _reject(lambda: reg.pack_command(JmCmd.PV, {'vel': 1, 'acc': 0, 'dec': 1}))
    _reject(lambda: reg.pack_command(JmCmd.PT, {'torque': 1, 'slope': 0}))
    _reject(lambda: reg.pack_command(JmCmd.JOG, {'dir': 2, 'speed': 1}))
    assert RunState.PROFILE_POSITION == 56


def test_classic_can_pp(reg):
    payload = reg.pack_command(JmCmd.PP, {'flags': 0, 'pos': 1, 'vel': 2,
                                          'acc': 3, 'dec': 4, 'jerk': 5,
                                          'profile_rev': 1, 'seq': 2})
    transport = CanTransport()
    frames = []
    transport._write_msg = lambda msg: frames.append(msg) or True
    assert transport._send_single(JmCmd.PP, payload, 7)
    assert len(frames) == 1
    assert frames[0].arbitration_id == can_make_id(JmCmd.PP, 7)
    assert bytes(frames[0].data) == payload


class _FakeCan(Transport):
    name = 'can'
    fd_mode = False

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, cmd, payload=b''):
        self.sent.append((int(cmd), bytes(payload)))
        return True


def test_profile_transaction():
    transport = _FakeCan()
    client = JmClient(transport)
    pp = {'flags': 1, 'pos': 1.25, 'vel': 2, 'acc': 3, 'dec': 4, 'jerk': 5}
    assert client.send_motion_command(JmCmd.PP, pp)
    assert transport.sent[0][0] == JmCmd.PROFILE_CONFIG
    token = struct.unpack_from('<H', transport.sent[0][1], 2)[0]
    for field, raw in pc.settings_for(JmCmd.PP, pp):
        assert transport.sent[-1] == (JmCmd.PROFILE_CONFIG, pc.pack_write(field, token, raw))
        client._on_frame(JmCmd.PROFILE_CONFIG, struct.pack('<BBH', pc.WRITE, field, token))
    assert transport.sent[-1] == (JmCmd.PROFILE_CONFIG, pc.pack_commit(token))
    client._on_frame(JmCmd.PROFILE_CONFIG, struct.pack('<BHH', pc.COMMIT, 9, token))
    for field, raw in pc.settings_for(JmCmd.PP, pp):
        assert transport.sent[-1] == (JmCmd.PROFILE_CONFIG, pc.pack_read(field))
        client._on_frame(JmCmd.PROFILE_CONFIG, struct.pack('<BBH', pc.READ, field, 9) + raw)
    assert transport.sent[-1][0] == JmCmd.PP
    assert len(transport.sent[-1][1]) == 8
    assert struct.unpack('<BfHB', transport.sent[-1][1])[:3] == (1, 1.25, 9)
    previous = len(transport.sent)
    assert client.send_motion_command(JmCmd.PP, {**pp, 'pos': 2.0})
    assert len(transport.sent) == previous + 1
    assert transport.sent[-1][0] == JmCmd.PP
    client._profile_on_link(False)
    assert client.send_motion_command(JmCmd.PP, pp)
    assert transport.sent[-1][0] == JmCmd.PROFILE_CONFIG
    client._on_frame(JmCmd.NACK, bytes((JmCmd.PROFILE_CONFIG, JmErr.NOT_SUPPORTED)))
    assert client._profile_pending is None
    assert not client.send_motion_command(JmCmd.PP, pp)


def test_classic_short_modes():
    transport = _FakeCan()
    client = JmClient(transport)
    samples = (
        (JmCmd.PV, {'vel': -2, 'acc': 3, 'dec': 4, 'current_lim': 0}, 8),
        (JmCmd.PT, {'torque': 1, 'slope': 2, 'current_lim': 3, 'vel_lim': 4}, 8),
        (JmCmd.JOG, {'dir': 1, 'speed': 2, 'acc': 3, 'dec': 4}, 5),
    )
    for revision, (cmd, values, short_len) in enumerate(samples, 1):
        assert client.send_motion_command(cmd, values)
        token = client._profile_pending['token']
        settings = pc.settings_for(cmd, values)
        for field, raw in settings:
            client._on_frame(JmCmd.PROFILE_CONFIG,
                             struct.pack('<BBH', pc.WRITE, field, token))
        client._on_frame(JmCmd.PROFILE_CONFIG,
                         struct.pack('<BHH', pc.COMMIT, revision, token))
        for field, raw in settings:
            client._on_frame(JmCmd.PROFILE_CONFIG,
                             struct.pack('<BBH', pc.READ, field, revision) + raw)
        assert transport.sent[-1][0] == cmd
        assert len(transport.sent[-1][1]) == short_len
    jog = samples[-1][1]
    assert client.send_motion_command(JmCmd.JOG, jog)
    assert transport.sent[-1][0] == JmCmd.JOG
    assert client.send_motion_command(JmCmd.JOG, {**jog, 'dir': 0})
    assert transport.sent[-1][1] == struct.pack('<bf', 0, 2)


def test_virtual_rejects_unimplemented_modes(reg):
    twin = TwinResponder()
    payloads = {
        JmCmd.PP: {'flags': 0, 'pos': 1, 'vel': 2, 'acc': 3, 'dec': 4,
                   'jerk': 5, 'profile_rev': 1, 'seq': 1},
        JmCmd.PV: {'vel': 1, 'acc': 2, 'dec': 3},
        JmCmd.PT: {'torque': 1, 'slope': 2, 'current_lim': 0, 'vel_lim': 0},
        JmCmd.JOG: {'dir': 1, 'speed': 2, 'acc': 3, 'dec': 4},
    }
    for cmd, values in payloads.items():
        assert twin.on_command(cmd, reg.pack_command(cmd, values)) == [
            (int(JmCmd.NACK), bytes((int(cmd), int(JmErr.NOT_SUPPORTED))))]


def test_panel(reg):
    app = QApplication.instance() or QApplication([])
    panel = MotionPanel(reg)
    available = {panel._combo_mode.itemData(i) for i in range(panel._combo_mode.count())}
    assert {int(c) for c in (JmCmd.MIT, JmCmd.PP, JmCmd.PV, JmCmd.PT, JmCmd.JOG)} <= available

    sent = []
    panel.send_command.connect(lambda cmd, values: sent.append((cmd, values.copy())))
    panel._combo_mode.setCurrentIndex(panel._combo_mode.findData(int(JmCmd.PP)))
    assert set(('vel', 'acc', 'dec', 'jerk')) <= panel._collect_values().keys()
    panel._field_widgets['flags'].setCurrentIndex(3)
    panel._on_send()
    assert sent[-1][0] == JmCmd.PP and sent[-1][1]['flags'] == 3
    opts = panel.get_opts()
    panel.set_opts(opts)
    assert panel._collect_values()['flags'] == 3

    panel._combo_mode.setCurrentIndex(panel._combo_mode.findData(int(JmCmd.JOG)))
    assert panel._btn_send.isHidden()
    panel._btn_jog_forward.pressed.emit()
    panel._btn_jog_forward.released.emit()
    assert [values['dir'] for _, values in sent[-2:]] == [1, 0]
    panel._btn_jog_reverse.pressed.emit()
    panel._combo_mode.setCurrentIndex(panel._combo_mode.findData(int(JmCmd.PV)))
    assert [values['dir'] for _, values in sent[-2:]] == [-1, 0]
    assert len(reg.pack_command(JmCmd.PV, panel._collect_values())) == 16
    panel.close()
    app.processEvents()


def main():
    reg = ProtocolRegistry()
    test_payloads(reg)
    test_classic_can_pp(reg)
    test_profile_transaction()
    test_classic_short_modes()
    test_virtual_rejects_unimplemented_modes(reg)
    test_panel(reg)
    print('handoff motion modes: PASS')


if __name__ == '__main__':
    main()
