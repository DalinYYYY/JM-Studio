"""Frozen release entry for Windows/Linux; diagnostics run only on request."""

import json
import hashlib
import os
from pathlib import Path
import sys
import traceback
import time

import main as application


if os.environ.get("JM_PACKAGE_SMOKE_TEST") == "1":
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QMainWindow

    def verify_can(app):
        import can
        from can.interface import _get_class_for_interface
        from jmproto.cmd_def import JmCmd
        from transport.can_transport import CanTransport, can_make_id

        backend = "pcan" if sys.platform == "win32" else "socketcan"
        assert CanTransport._INTERFACE == backend
        backend_class = _get_class_for_interface(backend)
        result = {"backend": backend, "class": backend_class.__name__}
        if sys.platform == "win32":
            import ctypes
            from can.interfaces.pcan.basic import PCANBasic
            dll = Path(sys._MEIPASS) / "PCANBasic.dll"
            assert dll.is_file(), "PCANBasic.dll was not bundled"
            ctypes.WinDLL(str(dll))
            basic = PCANBasic()  # Load the bundled SDK; never initialize real hardware.
            result["pcan_dll_loaded"] = bool(basic)
            result["pcan_dll_bundled"] = True
            result["pcan_dll_sha256"] = hashlib.sha256(dll.read_bytes()).hexdigest()
        channel = os.environ.get("JM_PACKAGE_CAN_CHANNEL")
        transport = CanTransport()
        if channel:
            assert backend == "socketcan"
            interface = backend
        else:
            interface = "virtual"
            channel = "jm-package-smoke"
            transport._INTERFACE = interface
        peer = can.Bus(interface=interface, channel=channel)
        received = []
        transport.frame_received.connect(lambda cmd, data: received.append((cmd, data)))
        try:
            transport.start()
            assert transport.open(channel=channel, motor_id=1)
            command = int(JmCmd.READ_STATE)
            assert transport.send(command, b"")
            outgoing = peer.recv(timeout=3)
            assert outgoing is not None and outgoing.is_extended_id
            assert outgoing.arbitration_id == can_make_id(command, 1)
            payload = bytes((1, 2, 3, 0))
            if interface == "socketcan" and channel.startswith("vcan"):
                # vcan marks all local-host traffic as TX. Model this test peer as
                # an external motor without changing production echo filtering.
                native_recv = transport._bus.recv

                def receive_external_peer(timeout=None):
                    message = native_recv(timeout=timeout)
                    if (message is not None and message.arbitration_id == can_make_id(command, 1)
                            and bytes(message.data) == payload):
                        message.is_rx = True
                    return message

                transport._bus.recv = receive_external_peer
                result["vcan_peer_origin_normalized"] = True
            peer.send(can.Message(arbitration_id=can_make_id(command, 1), data=payload,
                                  is_extended_id=True))
            deadline = time.monotonic() + 3
            while not received and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
            assert (command, payload) in received, "CAN response was not decoded"
            result.update(tx_verified=True, rx_verified=True, test_interface=interface)
        finally:
            transport.stop()
            peer.shutdown()
        return result

    class SmokeApplication(QApplication):
        def exec(self):
            QTimer.singleShot(750, self.verify_package)
            return super().exec()

        def verify_package(self):
            report = {"result": "failed", "version": application.APP_VERSION,
                      "platform": sys.platform}
            try:
                from jmproto import get_registry
                from ui import layout_store, preset_store

                windows = [w for w in self.topLevelWidgets()
                           if isinstance(w, QMainWindow) and w.isVisible()]
                assert windows, "Main window was not created"
                registry = get_registry()
                assert registry.commands and registry.params, "Protocol CSVs missing"
                assert getattr(sys, "frozen", False), "Must test the frozen executable"
                from ui.panels.connection_panel import ConnectionPanel
                panels = windows[0].findChildren(ConnectionPanel)
                assert panels and panels[0]._combo_transport.findData("can") >= 0
                report["can"] = verify_can(self)
                config = (Path(os.environ["APPDATA"]) / "JMStudio" if sys.platform == "win32"
                          else Path(os.environ["XDG_CONFIG_HOME"]) / "jm-studio")
                assert Path(layout_store.path()).parent == config
                assert Path(preset_store.path()).parent == config
                if os.environ.get("JM_PACKAGE_SMOKE_RESTART") == "1":
                    assert layout_store.get_meta("package_smoke") == "persisted"
                    assert preset_store.get("smoke")["label"] == "smoke"
                assert layout_store.set_meta("package_smoke", "persisted")
                assert layout_store.get_meta("package_smoke") == "persisted"
                assert preset_store._write({"presets": {"smoke": {"label": "smoke"}}})
                assert preset_store.get("smoke")["label"] == "smoke"
                screenshot = os.environ.get("JM_PACKAGE_SMOKE_SCREENSHOT")
                if screenshot:
                    assert windows[0].grab().save(screenshot), "Screenshot failed"
                report.update(result="passed", restart=os.environ.get(
                    "JM_PACKAGE_SMOKE_RESTART") == "1", commands=len(registry.commands),
                    parameters=len(registry.params), window_title=windows[0].windowTitle())
                if sys.stdout is not None:
                    print("JM_PACKAGE_SMOKE_OK: GUI, CSVs, CAN TX/RX, settings, presets",
                          flush=True)
                for window in windows:
                    window.close()
                self.exit(0)
            except Exception:
                report["error"] = traceback.format_exc()
                if sys.stderr is not None:
                    traceback.print_exc()
                self.exit(1)
            finally:
                report_path = os.environ.get("JM_PACKAGE_SMOKE_REPORT")
                if report_path:
                    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")

    application.QApplication = SmokeApplication


if __name__ == "__main__":
    application.main()
