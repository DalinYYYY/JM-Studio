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