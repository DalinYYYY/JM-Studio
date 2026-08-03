"""CAN 节点地址、发现和 SET_CAN_ID 响应回归测试。"""

import struct

import can
from PyQt6.QtCore import QCoreApplication

from core.motor_client import JmClient
from jmproto import JmCmd
from jmproto.crc16 import crc16_calc
from transport.base import Transport
from transport.can_transport import CAN_MULTI_FLAG, CAN_SEG_LAST, CanTransport, can_make_id
from ui.main_window import format_version


_APP = QCoreApplication.instance() or QCoreApplication([])


def test_version_is_displayed_as_four_decimal_components():
    assert format_version(0x02012001) == "2.1.32.1"
    assert format_version(0x00000000) == "0.0.0.0"


def test_send_to_captures_target_at_enqueue_time():
    tp = CanTransport()
    tp._bus = object()
    tp._running = True

    assert tp.send_to(37, JmCmd.READ_DEV_INFO)
    cmd, payload, node_id = tp._tx_queue.get_nowait()
    assert (cmd, payload, node_id) == (JmCmd.READ_DEV_INFO, b"", 37)


def test_can_di_broadcast_uses_motor_id_zero_and_single_frame_payload():
    tp = CanTransport()
    tp._bus = object()
    tp._running = True
    client = JmClient(tp)

    assert client.discover_can_di(3, 8, 2, 0x12345678)
    cmd, payload, node_id = tp._tx_queue.get_nowait()
    assert cmd == JmCmd.CAN_DI_DISCOVER
    assert node_id == 0
    assert payload == struct.pack('<BBBBI', 1, 3, 8, 2, 0x12345678)


def test_can_di_set_request_fits_classic_can_frame():
    tp = CanTransport()
    tp._bus = object()
    tp._running = True
    client = JmClient(tp)
    di56 = bytes.fromhex('01020304050607')

    assert client.set_can_id_by_di(di56, 23)
    cmd, payload, node_id = tp._tx_queue.get_nowait()
    assert (cmd, node_id) == (JmCmd.CAN_DI_SET_ID, 0)
    assert payload == di56 + bytes([23])
    assert len(payload) == 8


def test_can_di_responses_preserve_duplicate_node_rows():
    tp = CanTransport()
    tp._motor_id = 1
    tp.set_discovery_mode(True)
    client = JmClient(tp)
    found = []
    client.can_di_discovered.connect(
        lambda node_id, di56, guard: found.append((node_id, di56, guard)))

    responses = [
        bytes.fromhex('0102030405060711'),
        bytes.fromhex('1112131415161722'),
        bytes.fromhex('2122232425262733'),
    ]
    for payload in responses:
        msg = can.Message(
            arbitration_id=can_make_id(JmCmd.CAN_DI_DISCOVER, 2),
            data=payload,
            is_extended_id=True,
        )
        tp._handle_rx_msg(msg)

    assert len(found) == 3
    assert [entry[0] for entry in found] == [2, 2, 2]
    assert len({entry[1] + bytes([entry[2]]) for entry in found}) == 3


def test_can_di_set_ack_reports_old_and_new_node_ids():
    tp = Transport()
    client = JmClient(tp)
    results = []
    client.can_di_set_result.connect(
        lambda node_id, di56, status: results.append((node_id, di56, status)))
    di56 = bytes.fromhex('01020304050607')

    tp.addressed_frame_received.emit(2, JmCmd.CAN_DI_SET_ID, di56 + b'\x00')
    tp.addressed_frame_received.emit(5, JmCmd.CAN_DI_SET_ID, di56 + b'\x00')

    assert results == [(2, di56, 0), (5, di56, 0)]


def test_discovery_reassembles_dev_info_with_source_node():
    tp = CanTransport()
    tp._motor_id = 1
    tp.set_discovery_mode(True)
    addressed = []
    normal = []
    tp.addressed_frame_received.connect(
        lambda node_id, cmd, payload: addressed.append((node_id, cmd, payload)))
    tp.frame_received.connect(lambda cmd, payload: normal.append((cmd, payload)))

    payload = struct.pack("<II12s8s", 0x01020304, 0x05060708, b"uid-12345678", b"\x05\x01\x00\x00\x00\x00\x00\x00")
    crc = crc16_calc(payload)
    stream = payload + struct.pack("<H", crc)
    can_id = can_make_id(JmCmd.READ_DEV_INFO, 5) | CAN_MULTI_FLAG
    chunks = [stream[i:i + 7] for i in range(0, len(stream), 7)]

    for seq, chunk in enumerate(chunks):
        ctrl = seq | (CAN_SEG_LAST if seq == len(chunks) - 1 else 0)
        msg = can.Message(
            arbitration_id=can_id,
            data=bytes([ctrl]) + chunk,
            is_extended_id=True,
        )
        tp._handle_rx_msg(msg)

    assert addressed == [(5, JmCmd.READ_DEV_INFO, payload)]
    assert normal == []


def test_discovery_mode_keeps_selected_node_dev_info_available():
    tp = CanTransport()
    tp._motor_id = 5
    tp.set_discovery_mode(True)
    normal = []
    tp.frame_received.connect(lambda cmd, payload: normal.append((cmd, payload)))
    payload = struct.pack("<II12sB7s", 1, 2, b"uid-12345678", 5, b"\x00" * 7)
    crc = crc16_calc(payload)
    stream = payload + struct.pack("<H", crc)
    can_id = can_make_id(JmCmd.READ_DEV_INFO, 5) | CAN_MULTI_FLAG

    for seq, offset in enumerate(range(0, len(stream), 7)):
        chunk = stream[offset:offset + 7]
        ctrl = seq | (CAN_SEG_LAST if offset + 7 >= len(stream) else 0)
        tp._handle_rx_msg(can.Message(
            arbitration_id=can_id,
            data=bytes([ctrl]) + chunk,
            is_extended_id=True,
        ))

    assert normal == [(JmCmd.READ_DEV_INFO, payload)]


def test_set_can_id_ack_exposes_restart_requirement():
    client = JmClient(Transport())
    results = []
    acks = []
    client.can_id_set_result.connect(lambda node_id, restart: results.append((node_id, restart)))
    client.ack_received.connect(acks.append)

    client._on_frame(JmCmd.SET_CAN_ID, bytes([23, 1]))

    assert results == [(23, True)]
    assert acks == [JmCmd.SET_CAN_ID]


def test_dev_info_reports_source_and_stored_node_id():
    tp = Transport()
    client = JmClient(tp)
    found = []
    client.can_node_discovered.connect(
        lambda source_id, hw, fw, uid, reported_id:
        found.append((source_id, hw, fw, uid, reported_id)))
    payload = struct.pack("<II12sB7s", 1, 2, b"uid-12345678", 42, b"\x00" * 7)

    tp.addressed_frame_received.emit(42, JmCmd.READ_DEV_INFO, payload)

    assert found == [(42, 1, 2, b"uid-12345678", 42)]
