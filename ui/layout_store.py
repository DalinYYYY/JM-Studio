"""统一 UI 布局配置读写。

所有自绘图(FOC 框图 / 主状态机 / 运行模式状态机)的布局(方框坐标+大小、文字标注/
连线标签坐标)集中存到 resources/ui_layout.json, 软件启动时加载覆盖代码内置默认。

文件结构(坐标全部归一化 0~1):
{
  "version": 1,
  "foc":      {"blocks": {key:[x,y,w,h]}, "annots": {key:[x,y]}},
  "top_fsm":  {"nodes":  {key:[x,y,w,h]}, "labels": {"from>to":[x,y]}},
  "run_mode": {"nodes":  {key:[x,y,w,h]}, "labels": {"from>to":[x,y]}}
}
缺某节则该图用内置默认; JSON 损坏忽略并返回 {}, 不崩溃。
"""

import os
import json

# 路径定位仿 jmproto/registry.py: 相对本模块上溯到包根的 resources/
_RES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources")
_PATH = os.path.join(_RES_DIR, "ui_layout.json")

_VERSION = 1


def path() -> str:
    return _PATH


def load() -> dict:
    """读取整份布局配置; 不存在或损坏返回 {}。"""
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        # 损坏: 忽略, 用默认布局
        pass
    return {}


def load_section(section: str) -> dict:
    """读取某图的布局节(foc/top_fsm/run_mode); 缺失返回 {}。"""
    data = load()
    sec = data.get(section)
    return sec if isinstance(sec, dict) else {}


def save(section: str, data: dict) -> bool:
    """更新某图的布局节并写回, 保留其它图已存布局。成功返回 True。"""
    whole = load()
    whole["version"] = _VERSION
    whole[section] = data
    try:
        os.makedirs(_RES_DIR, exist_ok=True)
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(whole, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
