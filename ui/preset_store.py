"""用户自定义电机预设存储。

独立于内置 motor_presets.json（只读，随软件发布），用户预设可读写。
启动时与内置预设合并显示在 ComboBox，用户预设带 [用户] 前缀。

文件路径: resources/user_motor_presets.json
格式: 与 motor_presets.json 一致
{
  "version": 1,
  "presets": {
    "我的自定义电机": {
      "label": "我的自定义电机 (用户自定义)",
      "pole_pairs": 5,
      "phase_resistance": 2.5,
      "phase_inductance_d": 0.003,
      "phase_inductance_q": 0.003,
      "flux_linkage": 0.015,
      "torque_constant": 0.15,
      "rotor_inertia": 0.000008,
      "peak_current": 4.0,
      "max_speed": 180.0,
      "protect_over_current": 5.2
    }
  }
}

容错: 文件不存在/损坏返回 {"presets": {}}, 不崩溃。
"""

import os
import json
from runtime_paths import user_resource_path

# Linux 安装版的用户预设与只读的内置预设分开存放。
_PATH = user_resource_path("user_motor_presets.json")
_RES_DIR = os.path.dirname(_PATH)

_VERSION = 1

# 预设必填字段（10 个：9 电气 + 1 保护阈值）
PRESET_FIELDS = [
    "pole_pairs",              # uint32, 1~128
    "phase_resistance",        # float, 0.001~50 Ω
    "phase_inductance_d",      # float, 1e-6~0.1 H
    "phase_inductance_q",      # float, 1e-6~0.1 H
    "flux_linkage",            # float, 1e-5~1 Wb
    "torque_constant",         # float, 1e-5~50 Nm/A
    "rotor_inertia",           # float, 1e-7~1 kg·m²
    "peak_current",            # float, 0.1~200 A
    "max_speed",               # float, 10~1000 rad/s
    "protect_over_current",    # float, 0.1~1000 A
]

# 字段范围（与 motor_info.csv 一致，用于校验）
PRESET_RANGES = {
    "pole_pairs":           (1, 128),
    "phase_resistance":     (0.001, 50.0),
    "phase_inductance_d":   (1e-6, 0.1),
    "phase_inductance_q":   (1e-6, 0.1),
    "flux_linkage":         (1e-5, 1.0),
    "torque_constant":      (1e-5, 50.0),
    "rotor_inertia":        (1e-7, 1.0),
    "peak_current":         (0.1, 200.0),
    "max_speed":            (10.0, 1000.0),
    "protect_over_current": (0.1, 1000.0),
}


def path() -> str:
    """返回用户预设文件路径"""
    return _PATH


def load() -> dict:
    """读取整份用户预设; 不存在或损坏返回 {"presets": {}}。"""
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "presets" in data:
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {"version": _VERSION, "presets": {}}


def list_presets() -> dict:
    """返回 presets 字典 {name: preset_data}"""
    return load().get("presets", {})


def get(name: str) -> dict:
    """按名称获取单个预设; 不存在返回 None"""
    return load().get("presets", {}).get(name)


def add(name: str, preset: dict) -> bool:
    """新增预设; 名称已存在返回 False"""
    data = load()
    if name in data.get("presets", {}):
        return False
    data.setdefault("presets", {})[name] = preset
    return _write(data)


def update(name: str, preset: dict) -> bool:
    """更新已存在的预设; 不存在返回 False"""
    data = load()
    if name not in data.get("presets", {}):
        return False
    data["presets"][name] = preset
    return _write(data)


def rename(old_name: str, new_name: str) -> bool:
    """重命名预设; 新名称已存在或旧名称不存在返回 False"""
    data = load()
    presets = data.get("presets", {})
    if old_name not in presets or new_name in presets:
        return False
    presets[new_name] = presets.pop(old_name)
    return _write(data)


def delete(name: str) -> bool:
    """删除预设; 不存在返回 False"""
    data = load()
    if name not in data.get("presets", {}):
        return False
    del data["presets"][name]
    return _write(data)


def validate(preset: dict) -> list:
    """校验预设字段完整性 + 数值范围; 返回错误消息列表（空列表=通过）"""
    errors = []
    for field in PRESET_FIELDS:
        if field not in preset:
            errors.append(f"缺失字段: {field}")
            continue
        val = preset[field]
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            errors.append(f"{field}: 类型错误(应为数值)")
            continue
        lo, hi = PRESET_RANGES[field]
        if val < lo or val > hi:
            errors.append(f"{field}: {val} 超出范围 [{lo}, {hi}]")
    return errors


def _write(whole: dict) -> bool:
    """写入 JSON 文件（保留缩进和中文可读）"""
    try:
        os.makedirs(_RES_DIR, exist_ok=True)
        whole["version"] = _VERSION
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(whole, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
