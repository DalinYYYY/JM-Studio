用户问如何运行打包命令。给出简洁明了的说明。当前是通过 packaging\build_exe.ps1 和 build_exe.bat。给出命令。
打包入口有两个，都在 `packaging` 目录下：

## 方式一：bat 脚本（推荐，双击即用）
```bat
packaging\build_exe.bat
```

## 方式二：PowerShell 脚本
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "packaging\build_exe.ps1" -OneFile
```

## 常用参数
| 参数 | 作用 |
|------|------|
| `-OneFile` | 打包为单个 EXE（走 spec 精简逻辑，体积最小，42MB） |
| `-Python <路径>` | 指定 Python，如 `-Python "C:\Users\dal12\miniconda3\python.exe"` |
| `-SkipInstall` | 跳过依赖安装（已装过时加快打包） |
| `-Clean` | 清理缓存后重新构建 |

## 推荐命令
```bat
packaging\build_exe.bat -OneFile -Python "C:\Users\dal12\miniconda3\python.exe"
```

> 默认（不带 `-OneFile`）是目录模式（`_internal`）；要单文件必须加 `-OneFile`。产物输出到 `dist\JointMotorController.exe`。

注意：脚本默认会尝试自动找 Python，若环境变量没配好可能找到错误的解释器，建议显式加 `-Python` 参数。

## 打包功能选择（build_config.json）

编辑 `packaging\build_config.json` 可选择哪些功能打包进 EXE，未选择的功能在 EXE 中不出现：

```json
{
  "version": 1,
  "features": {
    "pid_tuning": true,
    "bode_sweep": false,
    "virtual_twin": false
  }
}
```

语义规则：

| 情形 | 行为 |
|------|------|
| 键值为 `true` | 功能打包并启用 |
| 键值为 `false` 或**未列出** | 功能禁用（EXE 中无对应 Tab/选项） |
| 未知的键（拼写错误等） | **报错终止构建**，并列出全部合法键 |
| `build_config.json` 文件缺失 | 打印警告，全功能构建（与旧行为一致） |

说明：

- 合法键集合自动从 `ui/feature_flags.py` 与开发版 `resources/feature_flags.json` 推导（新增功能键后无需改打包脚本）；构建日志会打印最终的 ENABLED/DISABLED 清单，新增功能键若忘加进配置，打包版默认禁用。
- 该配置**只影响打包产物**，与开发运行 `python main.py` 所用的 `resources/feature_flags.json` 完全独立，互不影响。
- `build_tester.bat`（测试员版）同样受此配置约束。
- 工作机制：构建前由 `prepare_build.py` 生成 `build\staging\resources`（完整 resources 镜像 + 按配置覆盖的 feature_flags.json），onefile 经环境变量、onedir 经 `--add-data` 注入。

单独校验配置（不改任何文件，可用于提交前自查）：

```bat
python packaging\prepare_build.py --check
```

排查 EXE 功能与预期不符时，按序检查：构建日志的 `Features` 行 → `build\staging\resources\feature_flags.json`（最终生效的开关表）→ 开发版 `resources\feature_flags.json`（仅影响 `python main.py`）。