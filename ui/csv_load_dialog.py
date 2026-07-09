"""CSV 参数表加载对话框: 选择 motor_info.csv / param_index.csv 路径并刷新。

主窗口调用流程:
    dlg = CsvLoadDialog(reg.motor_config_csv_path, reg.params_csv_path, self)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        chosen = dlg.chosen_paths()
        # 按 chosen['motor_config'] / chosen['motor_param'] reload registry + 刷新面板
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QPushButton,
    QLabel, QLineEdit, QFileDialog,
)

from jmproto.registry import DEFAULT_MOTOR_CONFIG_CSV, DEFAULT_PARAMS_CSV
from ui.theme import theme


class CsvLoadDialog(QDialog):
    """选择参数表 CSV 路径。accept 后通过 chosen_paths() 取结果。"""

    def __init__(self, current_motor_info: str, current_param_index: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("加载参数表 CSV")
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        info = QLabel(
            "选择新的参数表 CSV 后, 电机配置 / PID参数 / 标定结果 / 运行时参数表格"
            " 将立即按新 CSV 重建(数据与分组格式均来自CSV)。路径会记住, 下次启动自动加载。")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.hex('muted')}; font-size:12px;")
        v.addWidget(info)

        form = QFormLayout()
        form.setSpacing(8)

        self._mc_edit = QLineEdit(current_motor_info or "")
        self._mc_edit.setReadOnly(True)
        self._reset_mc = QPushButton("恢复默认")
        self._reset_mc.clicked.connect(
            lambda: self._mc_edit.setText(DEFAULT_MOTOR_CONFIG_CSV))
        mc_row = self._make_row(self._mc_edit, self._reset_mc)
        form.addRow("电机配置表\n(motor_info.csv):", mc_row)

        self._pi_edit = QLineEdit(current_param_index or "")
        self._pi_edit.setReadOnly(True)
        self._reset_pi = QPushButton("恢复默认")
        self._reset_pi.clicked.connect(
            lambda: self._pi_edit.setText(DEFAULT_PARAMS_CSV))
        pi_row = self._make_row(self._pi_edit, self._reset_pi)
        form.addRow("运行时参数表\n(param_index.csv):", pi_row)

        v.addLayout(form)

        btns = QHBoxLayout()
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_apply = QPushButton("应用并刷新")
        btn_apply.setStyleSheet(self._ok_qss())
        btn_apply.clicked.connect(self.accept)
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_apply)
        v.addLayout(btns)

    def _make_row(self, edit: QLineEdit, reset_btn: QPushButton) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        browse = QPushButton("浏览...")
        browse.clicked.connect(lambda: self._pick(edit))
        row.addWidget(edit, 1)
        row.addWidget(browse)
        row.addWidget(reset_btn)
        return row

    def _pick(self, edit: QLineEdit):
        cur = edit.text().strip()
        start_dir = str(Path(cur).parent) if cur else str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 CSV 文件", start_dir, "CSV 文件 (*.csv);;所有文件 (*)")
        if path:
            edit.setText(path)

    def chosen_paths(self) -> dict:
        """返回 {'motor_config': str|None, 'motor_param': str|None}。空串视为 None。"""
        mc = self._mc_edit.text().strip()
        pi = self._pi_edit.text().strip()
        return {
            "motor_config": mc or None,
            "motor_param": pi or None,
        }

    def _ok_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('ok')};"
                f"color:{theme.hex('ok_text')};border:none;padding:6px 14px;"
                f"border-radius:4px;font-weight:bold;}}"
                f"QPushButton:hover{{background:{theme.hex('accent')};}}")
