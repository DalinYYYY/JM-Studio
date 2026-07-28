"""功能开关配置加载。

resources/feature_flags.json 控制功能模块是否加载。
缺失/损坏/缺键默认全功能(true), 确保默认为完整版。
修改 JSON 中对应布尔值即可在编译/打包后移除功能, 无需改代码。
"""
import os
import json

# 路径定位仿 layout_store.py: 相对本模块上溯到包根的 resources/
_RES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources")
_PATH = os.path.join(_RES_DIR, "feature_flags.json")

# 默认全功能开关表(缺键时回退到这里的值)
_DEFAULTS = {
    "motor_config": True,
    "motor_params": True,
    "calibration": True,
    "pid_tuning": True,
    "feedback_chart": True,
    "state_machine": True,
    "waveform_plot": True,
    "fault_info": True,
    "virtual_twin": True,
    "log_panel": True,
    "can_transport": True,
}

_cache = None


def _load() -> dict:
    """读取 features 节(单例缓存); 损坏/缺失返回 {}。"""
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _cache = data.get("features", {}) if isinstance(data, dict) else {}
    except Exception:
        _cache = {}
    if not isinstance(_cache, dict):
        _cache = {}
    return _cache


def is_enabled(key: str) -> bool:
    """返回某功能是否启用。缺失/非bool默认True(全功能)。"""
    v = _load().get(key, _DEFAULTS.get(key, True))
    return bool(v) if isinstance(v, bool) else True


def get_enabled_features() -> dict:
    """返回所有开关状态(含默认值), 供日志输出。"""
    out = dict(_DEFAULTS)
    out.update(_load())
    # 过滤非 bool 值, 统一返回 bool
    return {k: bool(v) if isinstance(v, bool) else True for k, v in out.items()}


def path() -> str:
    """返回配置文件路径(供调试/显示)。"""
    return _PATH
