@echo off
REM Bygger zt-tray.exe (v2) med PyInstaller. Kräver Python 3.10+ på Windows.
cd /d "%~dp0"

echo === Installerar beroenden ===
python -m pip install --upgrade pyinstaller pystray Pillow sv-ttk
if errorlevel 1 goto fel

echo.
echo === scapy (for "Hitta PLC" - valfritt) ===
python -m pip install --upgrade scapy
set SCAPY=--collect-submodules scapy
if errorlevel 1 (
  echo scapy kunde inte installeras - bygger utan "Hitta PLC".
  set SCAPY=
)

echo.
echo === Bygger exe ===
python -m PyInstaller ^
  --noconfirm ^
  --onefile ^
  --windowed ^
  --uac-admin ^
  --name "zt-tray" ^
  --icon app.ico ^
  --version-file version_info.txt ^
  --collect-data sv_ttk ^
  --hidden-import=PIL._tkinter_finder ^
  --hidden-import=modbus_tab --hidden-import=modbus_monitor ^
  %SCAPY% ^
  zt_tray.py
if errorlevel 1 goto fel

echo.
echo === Klart ===
echo dist\zt-tray.exe
echo Lagg exe:n i en egen mapp (t.ex. C:\Tools\zt-tray\). config.json skapas vid forsta start.
echo Har du en config.json fran v1 - lagg den bredvid exe:n, den fungerar direkt.
pause
exit /b 0

:fel
echo.
echo Bygget misslyckades - se meddelandena ovan.
pause
exit /b 1
