# -*- mode: python ; coding: utf-8 -*-
# 关节电机上位机 单文件打包 spec
# 在 PyInstaller 默认收集基础上, 对 binaries/datas 做二次过滤:
#   1. 剔除未使用的 Qt 模块 DLL 及其插件(opengl32sw 软件GL回退、Qt6Pdf/Network/Test...)
#   2. 精简 Qt 翻译, 仅保留 zh_CN / zh_TW / en
# 主程序仅用 QtWidgets/Core/Gui/OpenGLWidgets, 且 pyqtgraph 走 QPainter 渲染,
# 不依赖软件 OpenGL, 故这些剔除项不构成硬依赖(均已核对导入表)。
import glob
import os
import sys
from PyInstaller.utils.hooks import collect_submodules

# 以 spec 所在目录定位工程根, 避免相对路径随运行目录漂移
_HERE = os.path.abspath(SPECPATH)          # SPECPATH 即 spec 所在目录
_ROOT = os.path.abspath(os.path.join(_HERE, '..'))
_RES = os.path.join(_ROOT, 'resources')

# 打包裁剪: build_exe.ps1 经 prepare_build.py 生成 staging 资源并通过
# JM_STAGING_RES 注入; 未设变量时保持默认工程 resources(直接跑 spec 不受影响)。
_STAGING = os.environ.get('JM_STAGING_RES', '')
if _STAGING:
    if not os.path.isdir(_STAGING):
        raise SystemExit('JM_STAGING_RES is set but directory missing: %s' % _STAGING)
    _RES_USE = _STAGING
else:
    _RES_USE = _RES
print('[spec] resources source: %s' % _RES_USE)

# 收集 libexpat.dll: conda 的 pyexpat.pyd 依赖它, 但 PyInstaller 不会自动收集
# (该 DLL 位于 <python>\Library\bin, 不在 python 安装根目录)。缺失会导致
# "DLL load failed while importing pyexpat" 启动崩溃。
_EXPAT_SEARCH = [
    os.path.join(os.path.dirname(sys.executable), 'Library', 'bin'),
    os.path.join(os.path.dirname(sys.executable), 'DLLs'),
    os.path.join(os.path.dirname(sys.executable), '.'),
]
_extra_binaries = []
for _d in _EXPAT_SEARCH:
    for _f in glob.glob(os.path.join(_d, 'libexpat*.dll')):
        _extra_binaries.append((_f, '.'))
        break

# ---- 收集 ----
# python-can 按需动态导入后端模块(can/interface.py 的 importlib.import_module),
# PyInstaller 静态分析不可见, 必须显式收集; 缺失时 CAN 通道扫描为空且无法打开总线。
# 当前仅用 PCAN 硬件; 换其他硬件(socketcan/vector/...)时需同步追加对应后端。
hiddenimports = ['serial.tools.list_ports', 'PyQt6.sip', 'PyQt6.QtOpenGLWidgets']
hiddenimports += collect_submodules('pyqtgraph')
hiddenimports += collect_submodules('serial')
hiddenimports += collect_submodules('transport.virtual_engine')
hiddenimports += collect_submodules('can.interfaces.pcan')

# 未使用的 Qt 模块(排除 .pyd 包装), 与 CLI 一致
excludes = [
    'PyQt6.QtPdf', 'PyQt6.QtNetwork', 'PyQt6.QtTest', 'PyQt6.QtQml',
    'PyQt6.QtQuick', 'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets',
    'PyQt6.QtSql', 'PyQt6.QtXml', 'PyQt6.QtMultimedia', 'PyQt6.QtPrintSupport',
    'PyQt6.QtConcurrent', 'PyQt6.QtBluetooth', 'PyQt6.QtNfc',
]

# 需要剔除的 Qt 二进制(按文件名), 均为可选运行时文件, 无硬依赖
_BIN_EXCLUDE = {
    # Qt 在 Windows 使用系统 ICU。PATH 上 Poppler 的同名 ICU 会被
    # PyInstaller 误收集，覆盖系统版本并使 QtWidgets 导入失败。
    'icuuc.dll',
    # 软件 OpenGL 渲染器(体积最大), 无 GPU 时才动态加载; 本应用用 QPainter 渲染
    'opengl32sw.dll',
    # 未使用的 Qt 模块库
    'Qt6Pdf.dll', 'Qt6Network.dll', 'Qt6Test.dll',
    # 相关插件: PDF 图片格式、TLS/网络后端、触摸输入
    'qpdf.dll',
    'qnetworklistmanager.dll',
    'qopensslbackend.dll', 'qschannelbackend.dll', 'qcertonlybackend.dll',
    'qtuiotouchplugin.dll', 'qtposition_nmea.dll',
}

# 保留的 Qt 翻译语言
_KEEP_LOCALE = ('zh_CN', 'zh_TW', 'en')


def _is_excluded_bin(src):
    return os.path.basename(src) in _BIN_EXCLUDE


def _keep_translation(src):
    # 非翻译文件一律保留
    norm = src.replace('\\', '/')
    if '/translations/' not in norm:
        return True
    base = os.path.basename(src)
    return any(base.endswith('_%s.qm' % loc) for loc in _KEEP_LOCALE)


a = Analysis(
    [os.path.join(_ROOT, 'main.py')],
    pathex=[_ROOT],
    binaries=_extra_binaries,
    datas=[(_RES_USE, 'resources')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# 二次过滤 binaries 与 datas
a.binaries = [b for b in a.binaries if not _is_excluded_bin(b[0])]
a.datas = [d for d in a.datas if _keep_translation(d[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='JointMotorController',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX 对 Qt/Python 压缩收益小且拖慢启动, 关闭
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 应用, 不显示控制台窗口(排障时临时改为 True 捕获错误)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
