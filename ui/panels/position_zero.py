"""系统控制区的设备零位按钮：静止检查、二次确认、持久化应答及超时处理。"""
import math
import struct

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QMessageBox

from jmproto.cmd_def import JmCmd, JmErr, TopFsm
from jmproto import position_config as pc


class PositionZeroController(QObject):
    """管理单个设零按钮；依赖设备状态和实际速度，CAN 可要求先确认目标节点。"""
    def __init__(self, button, parent=None):
        super().__init__(parent)
        self._button = button
        self._client = None
        self._connected = False
        self._top = None
        self._velocity = None
        self._gear_ratio = 1.0
        self._flags = 0
        self._pending = False
        self._confirming = False
        self._supported = True
        self._require_can_selection = False
        self._node_ready = True
        self._poll = QTimer(self)
        self._poll.setInterval(1500)
        self._poll.timeout.connect(self._query)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(4000)
        self._timeout.timeout.connect(self._timed_out)
        button.clicked.connect(self._set_current_zero)
        button.setToolTip('电机停止后，将当前位置设为设备零位并持久化保存。')
        self._update_button()

    def bind_client(self, client, require_can_selection=False):
        self._client = client
        self._require_can_selection = require_can_selection
        client.position_config_received.connect(self._on_reply)
        client.nack_received.connect(self._on_nack)
        client.state_updated.connect(self._on_state)
        client.feedback_updated.connect(self._on_feedback)
        client.motor_info_read_result.connect(self._on_motor_info)
        client.connected.connect(self.set_link_active)
        client.can_node_selected.connect(self._on_can_node_selected)

    def set_link_active(self, active):
        self._connected = bool(active)
        self._top = None
        self._velocity = None
        self._gear_ratio = 1.0
        self._flags = 0
        self._pending = False
        self._supported = True
        self._node_ready = not (self._require_can_selection and self._client.transport.name == 'can')
        self._timeout.stop()
        self._button.setToolTip('电机停止后，将当前位置设为设备零位并持久化保存。')
        if active and self._node_ready:
            self._poll.start()
            self._query()
        else:
            self._poll.stop()
        self._update_button()

    def _query(self):
        if self._client and self._connected and self._node_ready and self._supported and not self._pending:
            # 零位在连接、进入界面、保存和复位时回读；编码器未稳定时继续查询。
            if not self._flags & pc.READY:
                self._client.query_position_config()
            self._client.query_state()
            self._client.query_pos_vel()

    def _on_can_node_selected(self, node_id):
        self._node_ready = True
        self._supported = True
        self._top = None
        self._velocity = None
        self._gear_ratio = 1.0
        self._flags = 0
        self._pending = False
        self._timeout.stop()
        self._update_button()
        if self._connected:
            self._poll.start()
            # JmClient 在节点选择信号之后发送零位状态查询。
            self._client.query_state()
            self._client.query_pos_vel()

    def _on_state(self, top, run, mode, enabled):
        previous = self._top
        self._top = top
        if top == int(TopFsm.INIT) and previous != top:
            self._flags = 0  # 观察到硬件重启，清状态并查询至编码器稳定。
        if (top == int(TopFsm.IDLE) and previous not in (None, int(TopFsm.IDLE))
                and self._connected and self._node_ready and self._supported and not self._pending):
            self._client.query_position_config()
        self._update_button()

    def _on_feedback(self, feedback):
        if 'vel' in getattr(feedback, 'filled_fields', ()):
            self._velocity = float(feedback.vel)
            self._update_button()

    def _on_motor_info(self, param_id, ptype, value):
        if param_id == 28 and len(value) == 4:  # MotorCalibParam.gear_ratio，无量纲。
            ratio = struct.unpack('<f', bytes(value))[0]
            if math.isfinite(ratio) and ratio >= 1:
                self._gear_ratio = ratio
                self._update_button()

    def _can_set(self):
        """用减速比将关节速度还原为电机速度，和固件的 0.1 rad/s 阈值保持一致。"""
        return (self._connected and self._node_ready and self._supported and not self._pending
                and not self._confirming and self._top in (int(TopFsm.IDLE), int(TopFsm.READY))
                and bool(self._flags & pc.READY) and self._velocity is not None
                and math.isfinite(self._velocity) and abs(self._velocity) * self._gear_ratio <= 0.1)

    def _update_button(self):
        self._button.setEnabled(self._can_set())
        self._button.setText('零位保存中…' if self._pending else '设备零位设置')

    def _set_current_zero(self):
        """二次确认后重新检查静止条件，只发送当前位置设零并保存操作。"""
        if not self._can_set():
            return
        self._confirming = True
        self._update_button()
        answer = QMessageBox.question(
            self._button.window(), '设备零位设置',
            '确定将当前位置设为设备零位并持久化保存？\n\n'
            '保存成功后，位置控制和反馈均以此位置为零。请保持电机停止。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        self._confirming = False
        self._update_button()
        if answer != QMessageBox.StandardButton.Yes:
            return
        # 二次确认期间状态和反馈仍在更新，发送前必须再次检查静止条件。
        if not self._can_set():
            QMessageBox.warning(self._button.window(), '设备零位设置',
                                '电机状态已变化。请停止电机后重新设置。')
            return
        self._pending = True
        self._timeout.start()
        self._update_button()
        if not self._client.set_current_position_zero():
            self._timed_out('零位指令发送失败。')

    def _on_reply(self, op, flags, value):
        """状态帧只更新按钮；匹配的设零应答且 SAVED 置位才提示保存成功。"""
        self._flags = flags
        self._update_button()
        if not self._pending or op != pc.ZERO_CURRENT:
            return
        self._pending = False
        self._timeout.stop()
        self._update_button()
        if flags & (pc.CONFIGURED | pc.SAVED) == (pc.CONFIGURED | pc.SAVED):
            self._button.setToolTip(f'设备零位已保存，偏移 {value:.5f} rad。电机停止后可重新设置。')
            if flags & pc.VALID:
                QMessageBox.information(self._button.window(), '设备零位设置',
                                        '当前位置已设为设备零位，并已完成持久化保存。')
            else:
                QMessageBox.warning(self._button.window(), '设备零位设置',
                                    '零位已保存，但反馈尚未稳定。请保持电机停止并检查编码器状态。')
        else:
            QMessageBox.warning(self._button.window(), '设备零位设置',
                                '设备未确认零位已保存，请重新查询后再设置。')

    def _on_nack(self, cmd, error):
        if cmd != int(JmCmd.POSITION_CONFIG):
            return
        was_pending = self._pending
        self._pending = False
        self._timeout.stop()
        if error in (int(JmErr.UNSUPPORTED), int(JmErr.NOT_SUPPORTED)):
            self._supported = False
            self._poll.stop()
            message = '当前固件未支持设备零位设置。'
        elif error == int(JmErr.STATE_DENY):
            message = '零位设置被拒绝：电机必须停止，且编码器已稳定。'
        else:
            message = f'零位保存失败（0x{error:02X}）。请重新查询设备状态。'
        self._button.setToolTip(message)
        self._update_button()
        if was_pending:
            QMessageBox.warning(self._button.window(), '设备零位设置', message)

    def _timed_out(self, message='零位应答超时，保存结果尚未确认。请重新查询设备状态。'):
        self._pending = False
        self._flags = 0
        self._timeout.stop()
        self._button.setToolTip(message)
        self._update_button()
        QMessageBox.warning(self._button.window(), '设备零位设置', message)
