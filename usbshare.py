"""
Dela en USB-enhet till en annan ZT-Tray över ZeroTier.

Uttagssidan (där kabeln sitter) kör usbipd-win och binder enheten.
Verktygssidan (TIA/GX) kör usbip-win2 och kopplar in den, så den syns i
Enhetshanteraren där. Drivrutinen installeras bara på verktygssidan.

usbipd-win har ingen Windows-klient. Klienten är usbip-win2 (usbip.exe).
Den drivrutinen är testsignerad. ZT-Tray slår inte på testsignering själv.
"""

import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import ztctl
from config import log

USBIP_PORT = 3240
CLIENT_URL = "https://github.com/vadimgrn/usbip-win2/releases"
_STATES = ("Not shared", "Shared", "Attached", "Unplugged")
_BUSID = re.compile(r"^\d+-\d+(?:\.\d+)*$")


def find_usbipd():
    found = shutil.which("usbipd")
    if found:
        return found
    for p in (r"C:\Program Files\usbipd-win\usbipd.exe",
              r"C:\Program Files (x86)\usbipd-win\usbipd.exe"):
        if Path(p).exists():
            return p
    return ""


def find_usbip():
    found = shutil.which("usbip")
    if found and "usbipd" not in Path(found).name.lower():
        return found
    roots = [Path(r"C:\Program Files\usbip-win2"), Path(r"C:\Program Files (x86)\usbip-win2"),
             Path.home() / "usbip-win2"]
    for root in roots:
        hit = root / "usbip.exe"
        if hit.exists():
            return str(hit)
    return ""


def parse_usbipd_list(text):
    """Rader från `usbipd list` till {busid, vidpid, device, state}."""
    out = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or not _BUSID.match(parts[0]) or ":" not in parts[1]:
            continue
        tail = line.split(None, 2)[2].strip()
        state, device = "", tail
        for s in _STATES:
            if tail.endswith(s):
                state = s
                device = tail[: -len(s)].strip()
                break
        out.append({"busid": parts[0], "vidpid": parts[1], "device": device, "state": state})
    return out


def parse_usbip_remote(text):
    """Rader från `usbip list -r <ip>`."""
    out = []
    for line in (text or "").splitlines():
        raw = line.strip()
        m = re.match(r"(\d+-\d+(?:\.\d+)*)\s*:\s*(.+)", raw)
        if not m or raw.startswith(":"):
            continue
        out.append({"busid": m.group(1), "device": m.group(2).strip(), "vidpid": "", "state": "Shared"})
    return out


def parse_usbip_ports(text):
    """Portnummer från `usbip port`."""
    return [int(n) for n in re.findall(r"(?i)\bport\s+(\d+)", text or "")]


def bus_state(busid):
    """'Attached', 'Shared' eller tomt. Utan loggrad, anropas ofta från ikonen."""
    exe = find_usbipd()
    if not exe or not busid:
        return ""
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    try:
        r = subprocess.run([exe, "list"], capture_output=True, timeout=4,
                           encoding="oem" if sys.platform == "win32" else "utf-8",
                           errors="replace", **kw)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for row in parse_usbipd_list(r.stdout or ""):
        if row["busid"] == busid:
            return row["state"]
    return ""


def allow_firewall():
    ztctl.run(["netsh", "advfirewall", "firewall", "add", "rule",
                "name=ZT-Tray-usbip", "dir=in", "action=allow",
                "protocol=TCP", f"localport={USBIP_PORT}"])


def list_local():
    exe = find_usbipd()
    if not exe:
        raise RuntimeError("usbipd saknas. Installera med: winget install --exact dorssel.usbipd-win")
    rc, out, err = ztctl.run([exe, "list"])
    if rc != 0:
        raise RuntimeError((err or out or "usbipd list misslyckades").strip()[:300])
    return parse_usbipd_list(out)


def bind(busid):
    exe = find_usbipd()
    if not exe:
        raise RuntimeError("usbipd saknas på den här datorn.")
    allow_firewall()
    rc, out, err = ztctl.run([exe, "bind", f"--busid={busid}"])
    text = (out or err or "").strip()
    if rc != 0 and "already" not in text.lower() and "redan" not in text.lower():
        raise RuntimeError(text[:300] or f"usbipd bind rc={rc}")
    log(f"usb bind {busid}: {text[:160]}")
    return text or "Delad"


def unbind(busid):
    exe = find_usbipd()
    if not exe or not busid:
        return ""
    rc, out, err = ztctl.run([exe, "unbind", f"--busid={busid}"])
    text = (out or err or "").strip()
    log(f"usb unbind {busid}: rc={rc} {text[:160]}")
    return text


def list_remote(ip):
    exe = find_usbip()
    if not exe:
        raise RuntimeError("usbip.exe saknas. Hämta usbip-win2 (Windows-klienten) och lägg den i PATH.")
    rc, out, err = ztctl.run([exe, "list", "-r", ip])
    if rc != 0:
        rc, out, err = ztctl.run([exe, "list", "--remote", ip])
    if rc != 0:
        raise RuntimeError((err or out or "usbip list misslyckades").strip()[:300])
    return parse_usbip_remote(out)


def attach(ip, busid):
    exe = find_usbip()
    if not exe:
        raise RuntimeError("usbip.exe saknas på verktygssidan. Hämta usbip-win2.")
    rc, out, err = ztctl.run([exe, "attach", "-r", ip, "-b", busid], timeout=40)
    text = (out or err or "").strip()
    if rc != 0:
        rc, out, err = ztctl.run([exe, "attach", "--remote", ip, "--busid", busid], timeout=40)
        text = (out or err or "").strip()
    if rc != 0:
        raise RuntimeError(text[:300] or f"usbip attach rc={rc}")
    log(f"usb attach {ip} {busid}: {text[:160]}")
    return text or "Inkopplad"


def detach_all():
    exe = find_usbip()
    if not exe:
        return ""
    rc, out, err = ztctl.run([exe, "port"])
    ports = parse_usbip_ports(out)
    notes = []
    for port in ports:
        rc, out, err = ztctl.run([exe, "detach", "-p", str(port)])
        notes.append((out or err or "").strip())
    if not ports:
        ztctl.run([exe, "detach", "--all"])
    log("usb detach: " + " ".join(notes)[:160])
    return "Frånkopplad"


class UsbLink:
    """Tillstånd för en pågående USB-delning. Samma snapshot-form som nätbryggan."""

    def __init__(self, role, busid, peer_ip="", on_change=None):
        self.role = role
        self.busid = (busid or "").strip()
        self.peer_ip = (peer_ip or "").strip()
        self.on_change = on_change
        self.running = False
        self.live = False
        self.status = "Stoppad"
        self.tx = 0
        self.rx = 0
        self._watch_stop = threading.Event()
        self._watch = None

    def start(self):
        if self.role == "uttag":
            if not self.busid:
                raise RuntimeError("Välj USB-enheten som ska delas.")
            bind(self.busid)
            self.live = False
            self.status = f"Delar USB {self.busid}. Verktygssidan kopplar in den."
        else:
            if not self.peer_ip:
                raise RuntimeError("Skriv uttagsdatorns ZeroTier-IP.")
            if not self.busid:
                raise RuntimeError("Välj enheten som uttagsdatorn delar.")
            attach(self.peer_ip, self.busid)
            self.live = True
            self.status = f"USB {self.busid} från {self.peer_ip} är inkopplad här."
        self.running = True
        if self.role == "uttag":
            self._watch_stop.clear()
            self._watch = threading.Thread(target=self._watch_attach, name="usb-live", daemon=True)
            self._watch.start()
        self._emit()

    def stop(self):
        self._watch_stop.set()
        try:
            if self.role == "uttag":
                unbind(self.busid)
            else:
                detach_all()
        finally:
            self.running = False
            self.live = False
            self.status = "Stoppad"
            if self._watch:
                self._watch.join(timeout=0.2)
                self._watch = None
            self._emit()

    def _watch_attach(self):
        while not self._watch_stop.is_set():
            live = bus_state(self.busid) == "Attached"
            if live != self.live and self.running:
                self.live = live
                if live:
                    self.status = f"USB {self.busid} är inkopplad på verktygssidan."
                else:
                    self.status = f"Delar USB {self.busid}. Verktygssidan kopplar in den."
                self._emit()
            if self._watch_stop.wait(2):
                return

    def snapshot(self):
        short = ""
        live = bool(self.running and self.live)
        if self.running:
            short = "usb igång" if live else "usb väntar"
        return {"running": self.running, "role": self.role, "iface": self.busid,
                "status": self.status, "peer_ip": self.peer_ip, "peer_name": "",
                "tx": self.tx, "rx": self.rx, "dropped": 0, "peers": [],
                "short": short, "live": live}

    def set_manual_peer(self, ip):
        self.peer_ip = ip or ""

    def _emit(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass


def selftest():
    sample = """
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
2-2    0403:6001  FTDI USB Serial Converter                                     Not shared
2-10   1234:abcd  Some PLC adapter                                              Shared
"""
    rows = parse_usbipd_list(sample)
    assert rows[0]["busid"] == "2-2" and rows[0]["state"] == "Not shared"
    assert "FTDI" in rows[0]["device"]
    assert rows[1]["state"] == "Shared"
    remote = parse_usbip_remote("        3-2: FTDI : USB Serial (0403:6001)\n           : /sys/devices\n")
    assert remote[0]["busid"] == "3-2"
    assert parse_usbip_ports("Port 00: <Port in Use>\nPort 01: idle\n") == [0, 1]
    log("usbshare selftest OK")


if __name__ == "__main__":
    selftest()
