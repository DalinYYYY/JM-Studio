"""Linux SocketCAN must not receive PCAN-only settings or FD name checks."""

import sys
from unittest.mock import Mock, patch

from transport.can_transport import CanTransport


def test_default_backend_matches_platform():
    assert CanTransport._INTERFACE == ("socketcan" if sys.platform.startswith("linux")
                                       else "pcan")


def test_socketcan_open_uses_kernel_configuration():
    for fd in (False, True):
        transport = CanTransport()
        transport._INTERFACE = "socketcan"
        bus = Mock(channel_info="socketcan channel can0")
        with patch("transport.can_transport.can.Bus", return_value=bus) as factory, \
                patch.object(transport, "_start_tx_thread"):
            assert transport.open(channel="can0", bitrate=1000000, fd=fd,
                                  data_bitrate=5000000)
        factory.assert_called_once_with(interface="socketcan", channel="can0", fd=fd)
        assert transport._fd_capable is fd
        transport.close()


def test_pcan_open_preserves_windows_parameters():
    transport = CanTransport()
    transport._INTERFACE = "pcan"
    bus = Mock(channel_info="PCAN-USB FD")
    with patch("transport.can_transport.can.Bus", return_value=bus) as factory, \
            patch.object(transport, "_start_tx_thread"):
        assert transport.open(channel="PCAN_USBBUS1", bitrate=1000000, fd=True,
                              data_bitrate=5000000)
    factory.assert_called_once_with(interface="pcan", channel="PCAN_USBBUS1",
                                   bitrate=1000000, data_bitrate=5000000, fd=True)
    transport.close()


def test_socketcan_failure_explains_interface_setup():
    transport = CanTransport()
    transport._INTERFACE = "socketcan"
    errors = []
    transport.error_occurred.connect(errors.append)
    with patch("transport.can_transport.can.Bus", side_effect=OSError("Network is down")):
        assert not transport.open(channel="can0")
    assert "ip link" in errors[-1]
