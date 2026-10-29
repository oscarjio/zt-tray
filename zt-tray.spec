# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

datas = []
hiddenimports = ['PIL._tkinter_finder', 'modbus_tab', 'modbus_monitor', 'packet_view', 'linecheck', 'modbus_master', 'master_tab', 'ai_agent', 'ai_tab', 'cursor_backend', 'agent_mode', 'agent_console', 'start_tab', 'addrclash', 'winsound', 'serial_tab', 'serial', 'serial.tools', 'serial.tools.list_ports', 'serial.tools.list_ports_windows']
datas += collect_data_files('sv_ttk')
hiddenimports += collect_submodules('scapy')
# cursor-sdk hÃ¤mtas vid behov frÃ¥n PyPI; dess beroenden buntas hÃ¤r
for _m in ('httpx', 'httpcore', 'anyio', 'h11', 'certifi', 'idna', 'sniffio', 'typing_extensions'):
    try:
        hiddenimports += collect_submodules(_m)
    except Exception:
        pass


a = Analysis(
    ['zt_tray.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='zt-tray',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    uac_admin=True,
    icon=['app.ico'],
)
