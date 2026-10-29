"""
Windows-integration: autostart (Schemaläggaren), fästa tray-ikonen, mörkt läge, Npcap.
"""

import os
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from config import APP_DIR, log
from ztctl import run

IS_WIN = sys.platform == "win32"
TASK_NAME = "ZT-Tray-Autostart"   # samma namn som v1:s install_autostart.bat


# ---------------------------------------------------------------- exe-sökväg

def launch_command():
    """(program, argument) för att starta appen."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else sys.executable)
    return exe, f'"{Path(__file__).resolve().parent / "zt_tray.py"}"'


# ---------------------------------------------------------------- autostart

def task_xml(user, command, arguments, workdir):
    # Egen XML i stället för "schtasks /SC ONLOGON" eftersom standarduppgiften
    #  - inte startar på batteri (laptop!) och
    #  - dödas efter 72 timmar (ExecutionTimeLimit).
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Startar ZT-Tray vid inloggning</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Delay>PT5S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>6</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def autostart_enabled():
    if not IS_WIN:
        return False
    rc, _, _ = run(["schtasks", "/Query", "/TN", TASK_NAME])
    return rc == 0


def set_autostart(enable):
    """Returnerar (ok, meddelande)."""
    if not IS_WIN:
        return False, "Endast Windows"
    if not enable:
        rc, out, err = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        return rc == 0 or not autostart_enabled(), (err or out).strip()
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    cmd, args = launch_command()
    xml = task_xml(user, cmd, args, str(APP_DIR))
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write(xml)
        rc, out, err = run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"])
        return rc == 0, (err or out).strip()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------- fäst ikon

NOTIFY_KEY = r"Control Panel\NotifyIconSettings"
KNOWN_FOLDERS = {
    "{6D809377-6AF0-444B-8957-A3773F02200E}": os.environ.get("ProgramW6432", r"C:\Program Files"),
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": os.environ.get("SystemRoot", r"C:\Windows"),
}


def _expand(path):
    for guid, real in KNOWN_FOLDERS.items():
        if path.upper().startswith(guid):
            return real + path[len(guid):]
    return path


def pin_supported():
    """Windows 11 22H2+ har NotifyIconSettings med IsPromoted."""
    if not IS_WIN:
        return False
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_KEY))
        return True
    except OSError:
        return False


def set_pinned(enable):
    """
    Sätt IsPromoted på appens egen post. Returnerar True om posten hittades.
    Posten skapas av Windows först när ikonen har visats en gång.
    """
    if not IS_WIN:
        return False
    import winreg
    exe = os.path.normcase(os.path.abspath(launch_command()[0]))
    found = False
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_KEY)
    except OSError:
        return False
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            k = winreg.OpenKey(root, sub, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
            try:
                path, _ = winreg.QueryValueEx(k, "ExecutablePath")
            except OSError:
                winreg.CloseKey(k)
                continue
            if os.path.normcase(os.path.abspath(_expand(path))) == exe:
                winreg.SetValueEx(k, "IsPromoted", 0, winreg.REG_DWORD, 1 if enable else 0)
                found = True
            winreg.CloseKey(k)
        except OSError:
            continue
    winreg.CloseKey(root)
    return found


# ---------------------------------------------------------------- övrigt

def is_dark_mode():
    if not IS_WIN:
        return False
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        winreg.CloseKey(k)
        return v == 0
    except OSError:
        return False


def taskbar_is_dark():
    """Aktivitetsfältets tema (SystemUsesLightTheme) — styr ikonens kontrast."""
    if not IS_WIN:
        return True
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "SystemUsesLightTheme")
        winreg.CloseKey(k)
        return v == 0
    except OSError:
        return True


def npcap_installed():
    if not IS_WIN:
        return True   # för test på Linux (libpcap)
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    return (Path(sysroot) / "System32" / "Npcap" / "wpcap.dll").exists()


def scapy_available():
    import importlib.util
    return importlib.util.find_spec("scapy") is not None


def open_path(p):
    try:
        os.startfile(str(p))
    except Exception as e:
        log(f"Kunde inte öppna {p}: {e}")
