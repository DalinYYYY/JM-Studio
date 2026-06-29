"""数字孪生独立上位机主窗口。

布局:
┌────────────────┬───────────────────────────────────────┐
│ 引擎启停工具栏  │  Tab: 孪生参数 | 实时反馈 | 实时曲线   │
├────────────────┤                                       │
│ 系统控制       │                                       │
│ 运动控制       │                                       │
│ 运行状态       │                                       │
└────────────────┴───────────────────────────────────────┘

直接驱动 DigitalTwinEngine (经 TwinEngineBridge), 不经过协议层。
复用 ui.panels.twin_param_panel 的四类参数面板(给定/控制/推导/保护)。
"""

import json
import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTabWidget, QVBoxLayout, QWidget, QToolBar, QLabel,
)

from ui.panels.twin_param_panel import TwinParamPanel
from ui.theme import theme

from .engine_bridge import TwinEngineBridge
from .control_panel import TwinControlPanel
from .feedback_panel import TwinFeedbackPanel
from .plot_panel import TwinPlotPanel
from .log_panel import TwinLogPanel
from .fault_history_panel import TwinFaultHistoryPanel
from .motor_view_panel import MotorViewPanel


_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twin_layout.json")


class TwinMainWindow(QMainWindow):
    """数字孪生独立上位机主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("数字孪生独立上位机 (Standalone Twin)")
        self.setMinimumSize(1200, 800)

        # 引擎桥接
        self._bridge = TwinEngineBridge(self)

        # 虚拟电机模式待应用状态 (引擎就绪前缓存配置中的开关值)
        self._pending_virtual_mode = False

        self._build_ui()
        self._connect_signals()

        # 把孪生引擎句柄传给参数面板 + 反馈面板 + 控制面板 + 曲线面板
        self._twin_param_panel.set_engine(self._bridge.engine)
        self._feedback_panel.set_engine(self._bridge.engine)
        self._control_panel.set_engine(self._bridge.engine)
        self._plot_panel.set_engine(self._bridge.engine)
        self._motor_view_panel.set_engine(self._bridge.engine)

        # 加载已保存配置
        self._apply_config(self._load_config())

        # 自动启动仿真
        self._bridge.start()
        self._log_panel.log("OK", "数字孪生独立上位机初始化完成")
        self._log_panel.log("SYS", f"引擎已启动, 仿真周期 {TwinEngineBridge.SIM_PERIOD_MS}ms")
        self._log("数字孪生引擎已启动, 状态机独立运行中")

    # ==================== UI 组装 ====================
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        # 左侧控制面板
        self._control_panel = TwinControlPanel()
        self._control_panel.setMinimumWidth(340)
        self._control_panel.setMaximumWidth(420)
        splitter.addWidget(self._control_panel)

        # 右侧 Tab 区
        right = QWidget()
        right_v = QVBoxLayout(right)
        right_v.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        self._twin_param_panel = TwinParamPanel()
        self._feedback_panel = TwinFeedbackPanel()
        self._plot_panel = TwinPlotPanel()
        self._log_panel = TwinLogPanel()
        self._fault_history_panel = TwinFaultHistoryPanel()
        self._motor_view_panel = MotorViewPanel()
        # 电机可视化作为首页 (打开软件即可见)
        self._tabs.addTab(self._motor_view_panel, "电机可视化")
        self._tabs.addTab(self._twin_param_panel, "孪生参数")
        self._tabs.addTab(self._feedback_panel, "实时反馈")
        self._tabs.addTab(self._plot_panel, "实时曲线")
        self._tabs.addTab(self._fault_history_panel, "故障历史")
        self._tabs.addTab(self._log_panel, "事件日志")
        self._tabs.setCurrentWidget(self._motor_view_panel)
        # 虚拟电机模式默认关闭: 初始隐藏孪生参数与故障历史 Tab
        # (由工具栏"虚拟电机模式"开关在引擎运行后控制显隐)
        self._tabs.setTabVisible(self._tabs.indexOf(self._twin_param_panel), False)
        self._tabs.setTabVisible(self._tabs.indexOf(self._fault_history_panel), False)
        right_v.addWidget(self._tabs)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 840])
        root.addWidget(splitter)

        self._build_toolbar()
        self._build_statusbar()

    def _build_toolbar(self):
        tb = QToolBar("引擎控制")
        tb.setMovable(False)
        self.addToolBar(tb)

        self._act_start = QAction("启动仿真", self)
        self._act_start.triggered.connect(self._on_start)
        tb.addAction(self._act_start)

        self._act_stop = QAction("停止仿真", self)
        self._act_stop.triggered.connect(self._on_stop)
        tb.addAction(self._act_stop)

        tb.addSeparator()

        self._act_inject = QAction("注入故障…", self)
        self._act_inject.triggered.connect(self._on_inject_fault)
        tb.addAction(self._act_inject)

        self._act_clear_inj = QAction("清除注入", self)
        self._act_clear_inj.triggered.connect(self._on_clear_injected)
        tb.addAction(self._act_clear_inj)

        tb.addSeparator()

        # 参数导入导出 (Round 9)
        self._act_export = QAction("导出参数…", self)
        self._act_export.triggered.connect(self._on_export_params)
        tb.addAction(self._act_export)

        self._act_import = QAction("导入参数…", self)
        self._act_import.triggered.connect(self._on_import_params)
        tb.addAction(self._act_import)

        tb.addSeparator()

        # 场景预设 (Round 9)
        self._act_scn_high_inertia = QAction("高惯量场景", self)
        self._act_scn_high_inertia.triggered.connect(lambda: self._on_apply_scenario("high_inertia"))
        tb.addAction(self._act_scn_high_inertia)

        self._act_scn_low_inertia = QAction("低惯量场景", self)
        self._act_scn_low_inertia.triggered.connect(lambda: self._on_apply_scenario("low_inertia"))
        tb.addAction(self._act_scn_low_inertia)

        self._act_scn_heavy_load = QAction("重载场景", self)
        self._act_scn_heavy_load.triggered.connect(lambda: self._on_apply_scenario("heavy_load"))
        tb.addAction(self._act_scn_heavy_load)

        tb.addSeparator()

        # 虚拟电机模式开关: 仅在虚拟引擎运行时可用, 控制孪生参数/故障历史等虚拟电机专属视图的显隐
        self._act_virtual_mode = QAction("虚拟电机模式", self)
        self._act_virtual_mode.setCheckable(True)
        self._act_virtual_mode.setChecked(False)
        self._act_virtual_mode.setEnabled(False)   # 引擎未运行前禁用
        self._act_virtual_mode.setToolTip(
            "仅在连接虚拟引擎时可用。开启后显示孪生参数、虚拟电机故障注入与错误历史表。")
        self._act_virtual_mode.toggled.connect(self._on_virtual_mode_toggled)
        tb.addAction(self._act_virtual_mode)

        tb.addSeparator()

        act_theme = QAction("切换主题", self)
        act_theme.triggered.connect(self._on_toggle_theme)
        tb.addAction(act_theme)

    def _build_statusbar(self):
        sb = self.statusBar()
        self._sb_state = QLabel("● IDLE")
        self._sb_state.setStyleSheet(f"color: {theme.hex('muted')}; padding: 0 8px;")
        self._sb_log = QLabel("")
        self._sb_log.setStyleSheet(f"color: {theme.hex('muted')}; padding: 0 8px;")
        sb.addWidget(self._sb_state)
        sb.addPermanentWidget(self._sb_log)

    # ==================== 信号连接 ====================
    def _connect_signals(self):
        self._bridge.telemetry_ready.connect(self._on_telemetry)
        self._bridge.state_changed.connect(self._on_state_changed)
        self._bridge.fault_occurred.connect(self._on_fault)
        self._bridge.running_changed.connect(self._on_running_changed)
        # 命令日志: bridge 是单一命令日志源 (UI/代码/快捷按钮统一)
        self._bridge.command_sent.connect(self._log_panel.log)
        # 故障历史面板: fault_occurred 信号 + 遥测 (检测故障清除)
        self._bridge.fault_occurred.connect(
            lambda f, d: self._fault_history_panel.on_fault_occurred(f, d))
        # 故障注入按钮
        self._fault_history_panel.inject_requested.connect(self._on_quick_inject)
        self._fault_history_panel.clear_requested.connect(self._on_clear_injected)

        self._control_panel.system_command.connect(self._on_system_command)
        self._control_panel.motion_command.connect(self._on_motion_command)

        theme.changed.connect(self._on_theme_changed)

    # ==================== 遥测/状态回调 ====================
    def _on_telemetry(self, t: dict):
        self._control_panel.update_telemetry(t)
        self._feedback_panel.update_telemetry(t)
        self._plot_panel.feed(t)
        self._motor_view_panel.feed(t)
        self._fault_history_panel.on_telemetry(t)
        self._sb_state.setText(f"● {t.get('sys_state_name', '-')} / "
                               f"{t.get('run_state_name', '-')} / "
                               f"{t.get('control_mode_name', '-')}")

    def _on_state_changed(self, old: str, new: str):
        msg = f"状态迁移: {old} -> {new}"
        self._log_panel.log("STATE", msg)
        self._log(msg)

    def _on_fault(self, flags: int, desc: str):
        msg = f"故障触发: 0x{flags:04X} {desc}"
        self._log_panel.log("FAULT", msg)
        self._log(msg)
        QMessageBox.warning(self, "故障触发", f"故障码: 0x{flags:04X}\n{desc}")

    def _on_running_changed(self, running: bool):
        self._act_start.setEnabled(not running)
        self._act_stop.setEnabled(running)
        # 虚拟电机模式开关: 仅在虚拟引擎运行时可用
        if running:
            self._act_virtual_mode.setEnabled(True)
            # 引擎就绪后, 若配置要求开启则自动勾选
            if self._pending_virtual_mode and not self._act_virtual_mode.isChecked():
                self._act_virtual_mode.setChecked(True)
            self._pending_virtual_mode = False
        else:
            # 引擎停止: 禁用开关并强制取消勾选 (同步隐藏虚拟电机专属视图)
            self._act_virtual_mode.setEnabled(False)
            if self._act_virtual_mode.isChecked():
                self._act_virtual_mode.setChecked(False)
        self._log_panel.log("SYS", f"仿真{'启动' if running else '停止'}")

    def _on_virtual_mode_toggled(self, on: bool):
        """虚拟电机模式开关: 控制孪生参数 / 故障历史 (虚拟电机故障注入与错误表) 显隐。

        开关仅在虚拟引擎运行时可勾选 (由 _on_running_changed 联动 enable);
        关闭时隐藏孪生参数 Tab 和故障历史 Tab。
        """
        # 守卫: 引擎未运行时拒绝开启 (开关应已 disabled, 此为程序化调用兜底)
        if on and not self._bridge.is_running():
            self._act_virtual_mode.blockSignals(True)
            self._act_virtual_mode.setChecked(False)
            self._act_virtual_mode.blockSignals(False)
            return
        idx_param = self._tabs.indexOf(self._twin_param_panel)
        idx_fault = self._tabs.indexOf(self._fault_history_panel)
        self._tabs.setTabVisible(idx_param, on)
        self._tabs.setTabVisible(idx_fault, on)
        # 当前选中的 Tab 被隐藏时, 切回首页 (电机可视化)
        if not on and self._tabs.currentWidget() in (self._twin_param_panel,
                                                     self._fault_history_panel):
            self._tabs.setCurrentWidget(self._motor_view_panel)
        self._log_panel.log("SYS",
                            f"虚拟电机模式: {'开启' if on else '关闭'}")
        self._log(f"虚拟电机模式: {'开启' if on else '关闭'}")

    # ==================== 命令处理 ====================
    def _on_system_command(self, name: str):
        m = self._bridge
        if name == "enable":
            m.enable()
        elif name == "disable":
            m.disable()
        elif name == "stop":
            m.stop_motion()
        elif name == "idle":
            m.idle()
        elif name == "estop":
            m.e_stop()
        elif name == "clear":
            m.clear_fault()
        elif name == "reset":
            m.reset()
        self._log(f"系统命令: {name}")

    def _on_motion_command(self, mc):
        self._bridge.send_cmd(mc)
        self._log(f"运动指令: mode={mc.set_mode.name} start={mc.start}")

    def _on_start(self):
        self._bridge.start()
        self._log("仿真已启动")

    def _on_stop(self):
        self._bridge.stop()
        self._log("仿真已停止")

    def _on_inject_fault(self):
        from PyQt6.QtWidgets import QInputDialog
        from transport.virtual_engine import Fault
        items = ["OVER_CURRENT", "OVER_VOLTAGE", "UNDER_VOLTAGE", "OVER_TEMP_FET",
                 "OVER_TEMP_MOTOR", "POS_LIMIT", "FOLLOW_ERROR", "COMM_LOST",
                 "OVER_SPEED", "INJECTED"]
        item, ok = QInputDialog.getItem(self, "注入故障", "选择故障类型:", items, 0, False)
        if not ok:
            return
        bit = getattr(Fault, item, 0)
        if bit:
            self._bridge.inject_fault(bit)
            self._log(f"已注入故障: {item}")

    def _on_quick_inject(self, bit: int):
        """故障历史面板一键注入按钮回调 (Round 8)。"""
        from transport.virtual_engine import Fault
        # 反查故障名
        name = "未知"
        for attr in dir(Fault):
            if not attr.startswith('_') and getattr(Fault, attr, 0) == bit:
                name = attr
                break
        self._bridge.inject_fault(bit)
        self._log_panel.log("SYS", f"一键注入故障: {name} (0x{bit:04X})")
        self._log(f"已注入故障: {name}")

    def _on_clear_injected(self):
        self._bridge.clear_injected_fault()
        self._log("已清除注入故障")

    # ---------- 参数导入导出 (Round 9) ----------
    def _on_export_params(self):
        """导出当前引擎参数到 JSON 文件。"""
        try:
            from transport.virtual_engine.twin_config import mp_to_dict
            snap = mp_to_dict(self._bridge.engine.mp, section='all')
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"收集参数失败: {e}")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出参数到文件", "twin_params.json",
            "JSON 文件 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
            self._log_panel.log("OK", f"参数已导出: {path}")
            self._log(f"参数已导出: {path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"写入文件失败: {e}")

    def _on_import_params(self):
        """从 JSON 文件导入参数到引擎。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "从文件导入参数", "",
            "JSON 文件 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "导入失败", f"读取文件失败: {e}")
            return
        try:
            from transport.virtual_engine.twin_config import mp_from_dict
            if mp_from_dict(self._bridge.engine.mp, snap, section='all'):
                self._bridge.engine.rebuild()
                # 同步参数面板的勾选/数值状态
                self._twin_param_panel.set_engine(self._bridge.engine)
                self._motor_view_panel.set_engine(self._bridge.engine)
                self._log_panel.log("OK", f"参数已导入: {path}")
                self._log(f"参数已导入: {path}")
                QMessageBox.information(self, "导入成功",
                                        f"参数已从文件加载并应用:\n{path}")
            else:
                QMessageBox.warning(self, "导入失败", "参数文件格式不匹配")
        except Exception as e:
            QMessageBox.critical(self, "导入失败", f"应用参数失败: {e}")

    # ---------- 场景预设 (Round 9) ----------
    _SCENARIOS = {
        "high_inertia": {
            "label": "高惯量场景",
            "motor_base": {"inertia": 1e-3, "rated_torque": 5.0, "peak_torque": 15.0,
                           "rated_current": 10.0, "peak_current": 30.0, "max_speed": 100.0},
            "gearbox_param": {"gear_ratio": 50.0},
        },
        "low_inertia": {
            "label": "低惯量场景",
            "motor_base": {"inertia": 1e-6, "rated_torque": 0.5, "peak_torque": 1.5,
                           "rated_current": 2.0, "peak_current": 6.0, "max_speed": 1000.0},
            "gearbox_param": {"gear_ratio": 10.0},
        },
        "heavy_load": {
            "label": "重载场景",
            "motor_base": {"inertia": 5e-4, "rated_torque": 10.0, "peak_torque": 30.0,
                           "rated_current": 20.0, "peak_current": 60.0, "max_speed": 50.0},
            "gearbox_param": {"gear_ratio": 200.0},
        },
    }

    def _on_apply_scenario(self, name: str):
        """应用预设场景到引擎。"""
        scn = self._SCENARIOS.get(name)
        if scn is None:
            return
        try:
            mp = self._bridge.engine.mp
            # motor_base
            for attr, val in scn.get("motor_base", {}).items():
                if hasattr(mp.motor_base, attr):
                    setattr(mp.motor_base, attr, val)
            # gearbox_param
            for attr, val in scn.get("gearbox_param", {}).items():
                if hasattr(mp.gearbox_param, attr):
                    setattr(mp.gearbox_param, attr, val)
            self._bridge.engine.rebuild()
            # 同步参数面板
            self._twin_param_panel.set_engine(self._bridge.engine)
            self._motor_view_panel.set_engine(self._bridge.engine)
            self._log_panel.log("OK", f"已应用场景: {scn['label']}")
            self._log(f"场景已切换: {scn['label']}")
        except Exception as e:
            QMessageBox.critical(self, "场景切换失败", str(e))

    def _on_toggle_theme(self):
        theme.set("light" if theme.is_dark else "dark")

    def _on_theme_changed(self, name: str):
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.qss())
        for panel in (self._control_panel, self._twin_param_panel,
                      self._feedback_panel, self._plot_panel, self._log_panel,
                      self._fault_history_panel, self._motor_view_panel):
            fn = getattr(panel, "apply_theme", None)
            if callable(fn):
                fn()
        self._sb_state.setStyleSheet(f"color: {theme.hex('muted')}; padding: 0 8px;")
        self._sb_log.setStyleSheet(f"color: {theme.hex('muted')}; padding: 0 8px;")

    # ==================== 日志/状态栏 ====================
    def _log(self, msg: str):
        self._sb_log.setText(msg)

    # ==================== 配置持久化 ====================
    def _load_config(self) -> dict:
        try:
            if os.path.exists(_CONFIG_FILE):
                with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _apply_config(self, d: dict):
        if not isinstance(d, dict):
            return
        # 孪生参数面板 UI 状态 (勾选/tab)
        opts = d.get("twin_param")
        if isinstance(opts, dict):
            try:
                self._twin_param_panel.set_opts(opts)
            except Exception:
                pass
        # 引擎参数快照 (mp 完整参数树)
        mp_snap = d.get("engine_mp")
        if isinstance(mp_snap, dict):
            try:
                from transport.virtual_engine.twin_config import mp_from_dict
                if mp_from_dict(self._bridge.engine.mp, mp_snap, section='all'):
                    # 启动阶段(未运行)用 rebuild 让物理模型/控制器/fsm 全部用新参数生效
                    self._bridge.engine.rebuild()
                    self._log_panel.log("OK", "已加载引擎参数快照")
            except Exception as e:
                self._log_panel.log("WARN", f"引擎参数快照加载失败: {e}")
        # 窗口尺寸
        mw = d.get("main_window")
        if isinstance(mw, dict):
            try:
                self.resize(int(mw.get("width", self.width())),
                            int(mw.get("height", self.height())))
            except Exception:
                pass
        # 虚拟电机模式开关 (引擎就绪后由 _on_running_changed 应用)
        vm = d.get("virtual_motor_mode")
        if isinstance(vm, bool):
            self._pending_virtual_mode = vm

    def _collect_config(self) -> dict:
        cfg = {"main_window": {"width": int(self.width()), "height": int(self.height())}}
        try:
            cfg["twin_param"] = self._twin_param_panel.get_opts()
        except Exception:
            pass
        # 虚拟电机模式开关状态
        cfg["virtual_motor_mode"] = bool(self._act_virtual_mode.isChecked())
        # 引擎参数完整快照 (满足"所有参数持久化"硬约束)
        try:
            from transport.virtual_engine.twin_config import mp_to_dict
            cfg["engine_mp"] = mp_to_dict(self._bridge.engine.mp, section='all')
        except Exception as e:
            self._log_panel.log("WARN", f"引擎参数快照收集失败: {e}")
        return cfg

    def _save_config(self):
        try:
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._collect_config(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ==================== 退出 ====================
    def closeEvent(self, event):
        # 先保存配置 (此时虚拟电机模式开关 checked 仍是用户最后状态),
        # 再停止引擎 (stop 会触发 _on_running_changed 把 checked 重置为 False)
        self._save_config()
        self._bridge.stop()
        super().closeEvent(event)
