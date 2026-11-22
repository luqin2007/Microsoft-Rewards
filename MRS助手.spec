# -*- mode: python ; coding: utf-8 -*-
"""MRS 助手 PyInstaller 打包配置（上游没有此文件，本项目新增）。

打包成与 VM 现有布局一致的 onefile：
    MRS助手.exe            <- 本 spec 产出
    node.exe               <- 不打进 exe，留在同目录（90MB）
    node_modules/          <- 不打进 exe，留在同目录（56MB）
    dist/                  <- 随 exe 一起发，运行时由 runner.py 以 cwd=exe目录 加载
    config.json / .env / sessions / logs  <- 用户数据，不进包

datas 里带 dist 与 scripts 是为决策 3.A（随 exe 打包）；注意 runner.py 运行时
实际加载的是 exe 同目录的 dist/，所以部署时 dist/ 必须与 exe 一同替换。
scripts/ 用于控制 API，解包在 sys._MEIPASS 下；由于 getProjectRoot() 需要
package.json，API 更稳妥的启动方式是同目录另放一份 scripts/（见部署脚本）。
"""

a = Analysis(
    ['gui/app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('dist', 'dist'),
        ('scripts', 'scripts'),
    ],
    hiddenimports=[
        'customtkinter',
        'PIL._tkinter_finder',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'pytest',
        'IPython',
        'numpy',
        'pandas',
        'matplotlib',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MRS助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)