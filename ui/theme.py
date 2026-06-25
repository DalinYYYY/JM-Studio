"""全局主题: 深色/浅色双色板 + 单例 + 全局 QSS 生成。

唯一配色真相源。三类消费者:
  1) 常规组件 — main.py/main_window 用 theme.qss() 一次性铺底, 切换时重铺。
  2) 自绘图   — feedback_panel._T / state_machine_panel._Theme 转发到 theme.c(key)。
  3) 曲线     — plot_panel 用 theme.hex(key) 设 pyqtgraph 背景/画笔/轴色。
切换主题: theme.set('light'/'dark') -> 持久化 -> emit changed。订阅者重铺/重绘。
"""

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor


# ============================ 色板 ============================
# 键为语义名(扁平命名空间), 同名键在两套色板中一一对应。
DARK = {
    # 通用结构
    "app_bg": "#16181F",
    "panel_bg": "#1E222B",
    "card_top": "#2A2F3D",
    "card_bottom": "#232733",
    "border": "#383E4E",
    "text": "#D8DCE6",
    "text_strong": "#F0F2F8",
    "muted": "#7C8294",
    "title": "#9FB6DD",
    # 输入/按钮
    "input_bg": "#2A2E3A",
    "input_text": "#E4E7EF",
    "input_border": "#444A5A",
    "btn_bg": "#2A2A2A",
    "btn_text": "#DDDDDD",
    "btn_border": "#555555",
    "btn_hover": "#3A3A3A",
    # 表格
    "table_bg": "#1E222B",
    "table_alt": "#23272F",
    "table_grid": "#333845",
    "table_header": "#2A2F3D",
    "table_text": "#D8DCE6",
    "sel_bg": "#2E5C8A",
    "sel_text": "#FFFFFF",
    # 日志/等宽
    "log_bg": "#14161C",
    "log_text": "#C8CCD8",
    # 滚动条
    "scroll_bg": "#1A1D24",
    "scroll_handle": "#3A4150",
    # 语义状态色
    "accent": "#5FE6AC",
    "value": "#7FD4FF",
    "value_hot": "#FFB454",
    "ok": "#4CAF50",
    "ok_text": "#FFFFFF",
    "danger": "#F44336",
    "danger_text": "#FFFFFF",
    "warn": "#C8963C",
    # ---- 画布/自绘图 ----
    "bg_top": "#232733",
    "bg_bottom": "#191C24",
    # FOC 框图块
    "block_top": "#3A4150",
    "block_bottom": "#2C313D",
    "block_border": "#515872",
    "block_text": "#E6E9F2",
    "hi_border": "#5FE6AC",
    "edge": "#7C8398",
    "edge_fb": "#C9923F",
    "unit": "#8890A4",
    # 状态机节点
    "node_top": "#3A3F4E",
    "node_bottom": "#2C303C",
    "node_border": "#4D5365",
    "node_text": "#E4E7EF",
    "active_top": "#2FB37A",
    "active_bottom": "#1E8E63",
    "active_border": "#5FE6AC",
    "active_glow": "95,230,172",
    "fault_top": "#9E4B4B",
    "fault_bottom": "#7E3838",
    "fault_border": "#C06A6A",
    "afault_top": "#E0524F",
    "afault_bottom": "#C13B38",
    "afault_border": "#FF8C88",
    "afault_glow": "255,110,105",
    "group_border": "#5A7BB5",
    "group_border_active": "#5FE6AC",
    "group_title": "#9FB6DD",
    "edge_label": "#9298AC",
    "grid": "255,255,255,16",          # 编辑网格(rgba)
    "dot_grid": "255,255,255,10",       # 状态机点阵
    "chip_bg": "20,23,30,225",          # 标注胶囊底(rgba)
    # 转子图
    "rotor_shell": "#444B5E",
    "rotor_bore": "#20242E",
    "rotor_tooth": "#333A49",
    "rotor_tooth_border": "#475064",
    "rotor_n": "#C8554F",
    "rotor_s": "#4F7FD0",
    "rotor_hub": "#11141B",
    # 曲线(pyqtgraph)
    "plot_bg": "#202020",
    "plot_title": "#D8D8D8",
    "plot_pos": "#00BCD4",
    "plot_vel": "#4CAF50",
    "plot_id": "#26C6DA",
    "plot_iq": "#FF9800",
    "plot_ibus": "#AB47BC",
    "plot_ia": "#66BB6A",
    "plot_ib": "#29B6F6",
    "plot_ic": "#FFEE58",
    "plot_angle": "#FFC107",
    "plot_cursor": "180,180,180,160",
    "plot_label_bg": "0,0,0,160",
    "plot_label_fg": "#F5F5F5",
}

LIGHT = {
    "app_bg": "#EDEFF3",
    "panel_bg": "#F6F8FB",
    "card_top": "#FFFFFF",
    "card_bottom": "#EEF1F6",
    "border": "#D4DAE6",
    "text": "#1E2430",
    "text_strong": "#101521",
    "muted": "#5A6275",
    "title": "#34568B",
    "input_bg": "#FFFFFF",
    "input_text": "#1E2430",
    "input_border": "#C2CAD8",
    "btn_bg": "#FFFFFF",
    "btn_text": "#28303E",
    "btn_border": "#C2CAD8",
    "btn_hover": "#EAEEF5",
    "table_bg": "#FFFFFF",
    "table_alt": "#F2F5FA",
    "table_grid": "#DCE2EC",
    "table_header": "#E6EBF3",
    "table_text": "#1E2430",
    "sel_bg": "#BBD6F5",
    "sel_text": "#10243A",
    "log_bg": "#FAFBFD",
    "log_text": "#2A3140",
    "scroll_bg": "#E6EAF1",
    "scroll_handle": "#C2CAD8",
    "accent": "#1E9E6A",
    "value": "#1565C0",
    "value_hot": "#C77A14",
    "ok": "#2E7D32",
    "ok_text": "#FFFFFF",
    "danger": "#D32F2F",
    "danger_text": "#FFFFFF",
    "warn": "#B7791F",
    # 画布/自绘图(浅)
    "bg_top": "#FAFBFE",
    "bg_bottom": "#ECEFF5",
    "block_top": "#FFFFFF",
    "block_bottom": "#EAEEF5",
    "block_border": "#C2CAD8",
    "block_text": "#28303E",
    "hi_border": "#1E9E6A",
    "edge": "#7A8294",
    "edge_fb": "#C77A14",
    "unit": "#6B7385",
    "node_top": "#FFFFFF",
    "node_bottom": "#E9EDF4",
    "node_border": "#C2CAD8",
    "node_text": "#28303E",
    "active_top": "#3FBF86",
    "active_bottom": "#1E9E6A",
    "active_border": "#149063",
    "active_glow": "30,158,106",
    "fault_top": "#E06A66",
    "fault_bottom": "#C5443E",
    "fault_border": "#B23A35",
    "afault_top": "#F26561",
    "afault_bottom": "#D32F2F",
    "afault_border": "#B71C1C",
    "afault_glow": "211,47,47",
    "group_border": "#7C97C4",
    "group_border_active": "#1E9E6A",
    "group_title": "#34568B",
    "edge_label": "#5A6275",
    "grid": "0,0,0,18",
    "dot_grid": "0,0,0,12",
    "chip_bg": "255,255,255,230",
    "rotor_shell": "#AEB6C6",
    "rotor_bore": "#E4E8F0",
    "rotor_tooth": "#D2D9E5",
    "rotor_tooth_border": "#B6BFCE",
    "rotor_n": "#D14A44",
    "rotor_s": "#3A6FC0",
    "rotor_hub": "#C2CAD8",
    "plot_bg": "#FFFFFF",
    "plot_title": "#28303E",
    "plot_pos": "#0097A7",
    "plot_vel": "#2E7D32",
    "plot_id": "#0288A0",
    "plot_iq": "#E65100",
    "plot_ibus": "#7B2FA0",
    "plot_ia": "#388E3C",
    "plot_ib": "#0277BD",
    "plot_ic": "#C7A006",
    "plot_angle": "#E08600",
    "plot_cursor": "80,80,80,150",
    "plot_label_bg": "255,255,255,200",
    "plot_label_fg": "#10243A",
}

_PALETTES = {"dark": DARK, "light": LIGHT}


class _Theme(QObject):
    """全局主题单例。"""

    changed = pyqtSignal(str)   # 新主题名

    def __init__(self):
        super().__init__()
        self._name = "dark"

    @property
    def name(self):
        return self._name

    @property
    def is_dark(self):
        return self._name == "dark"

    def _pal(self):
        return _PALETTES.get(self._name, DARK)

    def hex(self, key: str) -> str:
        """返回某语义色的字符串(十六进制或 rgba 数字串)。缺失回退品红以便发现。"""
        return self._pal().get(key, DARK.get(key, "#FF00FF"))

    def c(self, key: str) -> QColor:
        """返回 QColor。支持 '#RRGGBB' 与 'r,g,b[,a]' 两种形式。"""
        v = self.hex(key)
        if v.startswith("#"):
            return QColor(v)
        parts = [int(x) for x in v.split(",")]
        return QColor(*parts)

    def set(self, name: str):
        if name not in _PALETTES or name == self._name:
            if name == self._name:
                return
            name = "dark"
        self._name = name
        self.changed.emit(name)

    # ---------------- 全局 QSS ----------------
    def qss(self) -> str:
        p = self._pal()
        g = lambda k: p.get(k, DARK[k])
        return f"""
        QWidget {{ background: {g('app_bg')}; color: {g('text')}; }}
        QToolTip {{ background: {g('card_top')}; color: {g('text')}; border: 1px solid {g('border')}; }}
        QGroupBox {{
            background: {g('panel_bg')}; border: 1px solid {g('border')};
            border-radius: 6px; margin-top: 10px; padding-top: 6px; font-weight: bold;
        }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {g('title')}; }}
        QLabel {{ background: transparent; color: {g('text')}; }}
        QCheckBox {{ background: transparent; color: {g('text')}; }}
        QPushButton {{
            background: {g('btn_bg')}; color: {g('btn_text')};
            border: 1px solid {g('btn_border')}; border-radius: 4px; padding: 3px 8px;
        }}
        QPushButton:hover {{ background: {g('btn_hover')}; border-color: {g('muted')}; }}
        QPushButton:disabled {{ color: {g('muted')}; }}
        QComboBox, QSpinBox, QLineEdit, QDoubleSpinBox {{
            background: {g('input_bg')}; color: {g('input_text')};
            border: 1px solid {g('input_border')}; border-radius: 4px; padding: 2px 4px;
        }}
        QComboBox QAbstractItemView {{
            background: {g('input_bg')}; color: {g('input_text')};
            selection-background-color: {g('sel_bg')}; selection-color: {g('sel_text')};
        }}
        QSpinBox:disabled, QComboBox:disabled {{ color: {g('muted')}; }}
        QTableWidget, QTableView {{
            background: {g('table_bg')}; alternate-background-color: {g('table_alt')};
            color: {g('table_text')}; gridline-color: {g('table_grid')};
            border: 1px solid {g('border')}; selection-background-color: {g('sel_bg')};
            selection-color: {g('sel_text')};
        }}
        QHeaderView::section {{
            background: {g('table_header')}; color: {g('title')};
            border: none; border-right: 1px solid {g('table_grid')};
            border-bottom: 1px solid {g('table_grid')}; padding: 4px;
        }}
        QTableCornerButton::section {{ background: {g('table_header')}; border: none; }}
        QTabWidget::pane {{ border: 1px solid {g('border')}; background: {g('panel_bg')}; }}
        QTabBar::tab {{
            background: {g('card_bottom')}; color: {g('muted')};
            border: 1px solid {g('border')}; border-bottom: none;
            padding: 5px 12px; border-top-left-radius: 5px; border-top-right-radius: 5px;
        }}
        QTabBar::tab:selected {{ background: {g('panel_bg')}; color: {g('text_strong')}; }}
        QScrollBar:vertical {{ background: {g('scroll_bg')}; width: 12px; margin: 0; }}
        QScrollBar::handle:vertical {{ background: {g('scroll_handle')}; border-radius: 5px; min-height: 24px; }}
        QScrollBar:horizontal {{ background: {g('scroll_bg')}; height: 12px; margin: 0; }}
        QScrollBar::handle:horizontal {{ background: {g('scroll_handle')}; border-radius: 5px; min-width: 24px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        QMenu {{ background: {g('card_top')}; color: {g('text')}; border: 1px solid {g('border')}; }}
        QMenu::item:selected {{ background: {g('sel_bg')}; color: {g('sel_text')}; }}
        QStatusBar {{ background: {g('panel_bg')}; color: {g('text')}; }}
        QDialog {{ background: {g('panel_bg')}; color: {g('text')}; }}
        QPlainTextEdit, QTextEdit {{
            background: {g('log_bg')}; color: {g('log_text')}; border: 1px solid {g('border')};
        }}
        """


# 进程内单例
theme = _Theme()
