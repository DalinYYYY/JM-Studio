"""打包功能裁剪: 读取 build_config.json, 生成 staging 资源目录。

build_exe.ps1 在打包前调用本脚本:
  1. 校验 build_config.json(未知键/非法值直接报错, 缺失键=false)
  2. 完整镜像 resources/ 到 build/staging/resources/
  3. 覆盖其中的 feature_flags.json 为按配置裁剪的全量显式开关表
     (全量写死所有键: feature_flags.is_enabled 对缺键 fail-open 回退 True,
      省略任何键都会导致该功能在 EXE 中静默回真)
  4. 在 staging 根写 feature_config_used.json 记录最终开关快照(不进 EXE)

staging 目录由 build_exe.ps1 注入两条打包路径:
  onefile(spec): 环境变量 JM_STAGING_RES
  onedir(命令行): --add-data "<staging>;resources"

用法:
  python packaging/prepare_build.py [--config packaging/build_config.json]
                                    [--out build/staging] [--check]
  --check 只校验配置并打印结果, 不写任何文件。
"""
import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEV_FLAGS_JSON = PROJECT_ROOT / "resources" / "feature_flags.json"
FF_MODULE_PATH = PROJECT_ROOT / "ui" / "feature_flags.py"


def _load_known_keys() -> set:
    """合法功能键 = feature_flags._DEFAULTS 键 ∪ 开发版 feature_flags.json 键。

    按文件路径加载 ui/feature_flags.py, 规避 ui/__init__.py 级联导入 PyQt6。
    新增功能键加入 _DEFAULTS 后自动进入本集合, 无需改打包脚本。
    """
    keys = set()
    spec = importlib.util.spec_from_file_location("jm_ff", FF_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    keys |= {k for k in mod._DEFAULTS if isinstance(k, str)}
    if DEV_FLAGS_JSON.is_file():
        data = json.loads(DEV_FLAGS_JSON.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("features"), dict):
            keys |= {k for k in data["features"] if isinstance(k, str)}
    return keys


def _fail(msg: str) -> "None":
    print("ERROR: %s" % msg, file=sys.stderr)
    sys.exit(1)


def resolve_features(config_path: Path, known_keys: set) -> dict:
    """解析并校验配置, 返回全量显式的功能开关表(未列出的键=false)。"""
    try:
        # utf-8-sig 容忍 Windows 记事本等编辑器写入的 BOM
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        _fail("failed to parse %s: %s" % (config_path, e))
    if not isinstance(data, dict) or not isinstance(data.get("features"), dict):
        _fail("%s must contain a 'features' object" % config_path)

    features = data["features"]
    unknown = sorted(set(features) - known_keys)
    if unknown:
        _fail("unknown feature keys: %s\n  valid keys: %s"
              % (", ".join(unknown), ", ".join(sorted(known_keys))))
    for k, v in features.items():
        if not isinstance(v, bool):  # bool 是 int 子类, 须显式排除 0/1/"true"
            _fail("feature '%s' must be a JSON boolean, got %r" % (k, v))

    # 允许清单语义: 配置里没列的键一律 false
    return {k: features.get(k, False) for k in sorted(known_keys)}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Prepare feature-trimmed staging resources")
    parser.add_argument("--config", default=str(Path(__file__).parent / "build_config.json"))
    parser.add_argument("--out", default=str(PROJECT_ROOT / "build" / "staging"))
    parser.add_argument("--check", action="store_true", help="validate only, write nothing")
    args = parser.parse_args()

    config_path = Path(args.config)
    out_dir = Path(args.out)
    if not config_path.is_file():
        _fail("build config not found: %s" % config_path)
    known_keys = _load_known_keys()
    if not known_keys:
        _fail("no known feature keys resolved (check ui/feature_flags.py)")
    features = resolve_features(config_path, known_keys)

    enabled = sorted(k for k, v in features.items() if v)
    disabled = sorted(k for k, v in features.items() if not v)
    print("Build config : %s" % config_path)
    print("Features     : %d enabled, %d disabled (of %d)"
          % (len(enabled), len(disabled), len(features)))
    if disabled:
        print("  DISABLED   : %s" % ", ".join(disabled))

    if args.check:
        print("Check OK (no files written)")
        return

    # 先删后建, 永不复用旧 staging
    if out_dir.exists():
        shutil.rmtree(out_dir)
    staging_res = out_dir / "resources"
    shutil.copytree(PROJECT_ROOT / "resources", staging_res)

    flags_out = staging_res / "feature_flags.json"
    flags_out.write_text(
        json.dumps({"version": 1, "features": features}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (out_dir / "feature_config_used.json").write_text(
        json.dumps({"config": str(config_path), "features": features},
                   indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print("Staging      : %s" % staging_res)


if __name__ == "__main__":
    main()
