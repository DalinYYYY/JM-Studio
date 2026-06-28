"""
数据导出: CSV / PNG

CSV 格式: 第一列时间 (s), 后续列各可见通道
PNG: 通过 pyqtgraph exporters 导出当前波形图
"""
from __future__ import annotations

import csv
from typing import Dict

import numpy as np

from PyQt6.QtWidgets import QFileDialog, QMessageBox

import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter

from .waveform_plot import Curve


def export_dialog(parent, curves: Dict[str, Curve]):
    """弹出导出选择对话框, 用户选 CSV 或 PNG"""
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, \
        QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QCheckBox, QComboBox

    dlg = QDialog(parent)
    dlg.setWindowTitle("导出")
    layout = QVBoxLayout(dlg)

    # 格式选择
    fmt_layout = QHBoxLayout()
    fmt_layout.addWidget(QLabel("格式:"))
    cmb_fmt = QComboBox()
    cmb_fmt.addItem("CSV (所有可见通道)", 'csv')
    cmb_fmt.addItem("PNG (当前波形图)", 'png')
    fmt_layout.addWidget(cmb_fmt)
    layout.addLayout(fmt_layout)

    hint = QLabel("CSV: 所有可见通道数据导出到一个文件, 第一列为时间 (s)\n"
                  "PNG: 当前波形图截图")
    hint.setStyleSheet("color: gray; font-size: 11px;")
    layout.addWidget(hint)

    btns = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    layout.addWidget(btns)

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return

    fmt = cmb_fmt.currentData()
    if fmt == 'csv':
        _export_csv(parent, curves)
    else:
        _export_png(parent, curves)


def _export_csv(parent, curves: Dict[str, Curve]):
    # 选文件
    path, _ = QFileDialog.getSaveFileName(
        parent, "导出 CSV", "waveform.csv", "CSV Files (*.csv);;All Files (*)"
    )
    if not path:
        return

    # 收集可见通道
    visible = [(key, c) for key, c in curves.items() if c.meta.visible and c.buf.count > 0]
    if not visible:
        QMessageBox.warning(parent, "无数据", "没有可见通道有数据")
        return

    # 以时间最长的通道为基准, 其他通道用最近值填
    max_count = max(c.buf.count for _, c in visible)
    base_key, base_curve = max(visible, key=lambda x: x[1].buf.count)
    base_ts = base_curve.buf.ts_view[:max_count]

    # 构建列
    columns = [('time_s', base_ts)]
    for key, curve in visible:
        ys = curve.buf.ys_view[:curve.buf.count]
        ts = curve.buf.ts_view[:curve.buf.count]
        # 对齐到 base_ts
        aligned = np.interp(base_ts, ts, ys, left=np.nan, right=np.nan)
        columns.append((key, aligned))

    # 写 CSV
    try:
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            # 表头
            writer.writerow([name for name, _ in columns])
            # 单位行
            units = ['s']
            for key, _ in columns[1:]:
                meta = curves[key].meta
                units.append(f"{meta.label}({meta.unit})")
            writer.writerow(units)
            # 数据
            n = len(base_ts)
            for i in range(n):
                row = []
                for _, arr in columns:
                    v = arr[i]
                    row.append('' if np.isnan(v) else f"{v:.6f}")
                writer.writerow(row)
        QMessageBox.information(parent, "导出成功", f"已导出 {n} 行到:\n{path}")
    except Exception as e:
        QMessageBox.critical(parent, "导出失败", str(e))


def _export_png(parent, curves: Dict[str, Curve]):
    # 找到 waveform_plot 的 GraphicsLayoutWidget
    # 通过 parent 向上找
    from .waveform_plot import WaveformPlot
    wp = None
    p = parent
    while p is not None:
        if isinstance(p, WaveformPlot):
            wp = p
            break
        p = p.parent()
    if wp is None:
        QMessageBox.warning(parent, "无法导出", "找不到波形图组件")
        return

    path, _ = QFileDialog.getSaveFileName(
        parent, "导出 PNG", "waveform.png", "PNG Files (*.png);;All Files (*)"
    )
    if not path:
        return

    try:
        exporter = ImageExporter(wp._gfx.scene())
        exporter.export(path)
        QMessageBox.information(parent, "导出成功", f"已导出到:\n{path}")
    except Exception as e:
        # 备用方法
        try:
            exporter = pg.exporters.ImageExporter(wp._gfx)
            exporter.export(path)
            QMessageBox.information(parent, "导出成功", f"已导出到:\n{path}")
        except Exception as e2:
            QMessageBox.critical(parent, "导出失败", f"{e}\n备用方法: {e2}")
