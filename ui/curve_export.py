"""曲线历史 CSV 导出，快照后在后台写文件，避免阻塞遥测与绘图。"""

import csv
from datetime import datetime
import heapq
import math
import os
from pathlib import Path
import tempfile

import numpy as np
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox


def _number(value):
    value = float(value)
    return format(value, '.9g') if math.isfinite(value) else ''


def write_curve_csv(path, series, x_name='data_point'):
    """写全部缓存；每通道一列，缺失/无效值留空，时间戳不对齐时按 X 合并。"""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8-sig', newline='',
                dir=path.parent, prefix=path.name + '.', suffix='.tmp',
                delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow([x_name] + [name for name, _ in series])
            arrays = [data for _, data in series]
            if arrays and all(np.array_equal(ts, arrays[0][0]) for ts, _ in arrays):
                writer.writerows(
                    [_number(value) for value in row]
                    for row in zip(arrays[0][0], *(ys for _, ys in arrays)))
            else:
                heap = [(float(ts[0]), col, 0) for col, (ts, _) in enumerate(arrays) if len(ts)]
                heapq.heapify(heap)
                while heap:
                    x = heap[0][0]
                    row = [''] * len(arrays)
                    while heap and heap[0][0] == x:
                        _, col, index = heapq.heappop(heap)
                        ts, ys = arrays[col]
                        row[col] = _number(ys[index])
                        index += 1
                        if index < len(ts):
                            heapq.heappush(heap, (float(ts[index]), col, index))
                    writer.writerow([_number(x)] + row)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class _CsvTask(QThread):
    def __init__(self, path, series, x_name, parent):
        super().__init__(parent)
        self.path, self.series, self.x_name = path, series, x_name
        self.error = ''

    def run(self):
        try:
            write_curve_csv(self.path, self.series, self.x_name)
        except Exception as exc:
            self.error = str(exc)


_exports = set()  # 窗口关闭后仍保留工作线程，退出应用前等待当前文件写完。


def export_curve_csv(parent, button, prefix, snapshot, x_name='data_point'):
    name = f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    path, _ = QFileDialog.getSaveFileName(parent, '导出全部曲线历史', name, 'CSV 文件 (*.csv)')
    if not path:
        return
    if not Path(path).suffix:
        path += '.csv'
    try:
        series = snapshot()
    except Exception as exc:
        QMessageBox.warning(parent, '导出失败', str(exc))
        return
    app = QApplication.instance()
    task = _CsvTask(path, series, x_name, app)
    _exports.add(task)
    text = button.text()
    button.setEnabled(False)
    button.setText('导出中…')
    app.aboutToQuit.connect(task.wait)

    def finished():
        button.setText(text)
        button.setEnabled(True)
        app.aboutToQuit.disconnect(task.wait)
        if task.error:
            QMessageBox.warning(parent, '导出失败', task.error)
        else:
            QMessageBox.information(parent, 'CSV 导出完成', f'已导出全部缓存历史：\n{path}')
        _exports.discard(task)
        task.deleteLater()

    task.finished.connect(finished)
    task.start()
