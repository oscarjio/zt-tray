"""
Dela en COM-port till en annan ZT-Tray över ZeroTier.

Uttagssidan öppnar den fysiska porten och pratar RFC 2217 på TCP 47922.
Verktygssidan skapar ett com0com-par (COM som TIA öppnar, och en sida
bryggan läser) och skickar baud, paritet och stoppbitar till uttaget.
Bauden ställs alltså på verktygssidan. En USB-serieadapter kan i stället
delas som USB, då behövs inte det här.
"""

import socket
import threading
import time
from pathlib import Path

import ztctl
from config import log

PORT = 47922
IAC, SE, SB = 255, 240, 250
WILL, WONT, DO, DONT = 251, 252, 253, 254
CPO = 44
SET_BAUD, SET_DATA, SET_PARITY, SET_STOP = 1, 2, 3, 4
_PARITY_TO_RFC = {"N": 1, "O": 2, "E": 3, "M": 4, "S": 5}
_RFC_TO_PARITY = {v: k for k, v in _PARITY_TO_RFC.items()}
_STOP_TO_RFC = {1: 1, 2: 2, 1.5: 3}
_RFC_TO_STOP = {1: 1, 2: 2, 3: 1.5}


def list_com_ports():
    """COM-portar som Windows redan känner till."""
    import sys
    if sys.platform != "win32":
        return []
    import winreg
    out = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return []
    try:
        i = 0
        while True:
            _name, value, _typ = winreg.EnumValue(key, i)
            if isinstance(value, str) and value.upper().startswith("COM"):
                out.append(value.upper())
            i += 1
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)
    return sorted(set(out), key=lambda s: int(s[3:] or 0) if s[3:].isdigit() else 0)


def find_setupc():
    for p in (r"C:\Program Files (x86)\com0com\setupc.exe",
              r"C:\Program Files\com0com\setupc.exe"):
        if Path(p).exists():
            return p
    return ""


class Rfc2217:
    """Tolka en RFC 2217-ström. Databytes kommer ut, inställningar sparas."""

    def __init__(self):
        self.buf = bytearray()
        self.settings = {"baud": 9600, "bytesize": 8, "parity": "N", "stopbits": 1}
        self.changed = False
        self.replies = bytearray()

    def greeting(self):
        return bytes([IAC, WILL, CPO, IAC, WILL, 0, IAC, DO, 0])

    def command(self, baud, bytesize, parity, stopbits):
        return b"".join((
            self._sub(SET_BAUD, int(baud).to_bytes(4, "big")),
            self._sub(SET_DATA, bytes([int(bytesize)])),
            self._sub(SET_PARITY, bytes([_PARITY_TO_RFC.get(parity, 1)])),
            self._sub(SET_STOP, bytes([_STOP_TO_RFC.get(stopbits, 1)])),
        ))

    def feed(self, data):
        self.buf += data
        out = bytearray()
        i = 0
        b = self.buf
        while i < len(b):
            if b[i] != IAC:
                out.append(b[i])
                i += 1
                continue
            if i + 1 >= len(b):
                break
            cmd = b[i + 1]
            if cmd == IAC:
                out.append(IAC)
                i += 2
                continue
            if cmd in (WILL, WONT, DO, DONT):
                if i + 2 >= len(b):
                    break
                i += 3
                continue
            if cmd == SB:
                end = _find_se(b, i + 2)
                if end < 0:
                    break
                self._on_sub(bytes(b[i + 2:end]))
                i = end + 2
                continue
            i += 2
        del self.buf[:i]
        return bytes(out)

    def escape(self, data):
        return data.replace(bytes([IAC]), bytes([IAC, IAC]))

    def _sub(self, code, payload):
        return bytes([IAC, SB, CPO, code]) + payload + bytes([IAC, SE])

    def _on_sub(self, body):
        if len(body) < 2 or body[0] != CPO:
            return
        code, rest = body[1], body[2:]
        if code > 100:
            return
        if code == SET_BAUD and len(rest) >= 4:
            self.settings["baud"] = int.from_bytes(rest[:4], "big")
        elif code == SET_DATA and rest:
            self.settings["bytesize"] = int(rest[0])
        elif code == SET_PARITY and rest:
            self.settings["parity"] = _RFC_TO_PARITY.get(int(rest[0]), "N")
        elif code == SET_STOP and rest:
            self.settings["stopbits"] = _RFC_TO_STOP.get(int(rest[0]), 1)
        else:
            return
        self.changed = True
        self.replies += self._sub(code + 100, rest)


def _find_se(buf, start):
    i = start
    while i + 1 < len(buf):
        if buf[i] == IAC and buf[i + 1] == SE:
            return i
        i += 2 if buf[i] == IAC else 1
    return -1


class WinSerial:
    """COM-port via Win32, så pyserial inte behövs."""

    def __init__(self):
        self.h = None
        self.port = ""

    def open(self, port, baud=9600, bytesize=8, parity="N", stopbits=1):
        import ctypes
        from ctypes import wintypes
        self.close()
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        k.CreateFileW.restype = ctypes.c_void_p
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k.CreateFileW("\\\\.\\" + port, 0xC0000000, 0, None, 3, 0, None)
        invalid = ctypes.c_void_p(-1).value
        if not handle or handle == invalid:
            err = ctypes.get_last_error()
            raise OSError(err, f"Kan inte öppna {port} (fel {err})")
        self._k = k
        self.h = handle
        self.port = port
        self.configure(baud, bytesize, parity, stopbits)
        self._timeouts()

    def configure(self, baud, bytesize, parity, stopbits):
        import ctypes
        from ctypes import wintypes
        k = self._k

        class DCB(ctypes.Structure):
            _fields_ = [
                ("DCBlength", wintypes.DWORD), ("BaudRate", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("wReserved", wintypes.WORD),
                ("XonLim", wintypes.WORD), ("XoffLim", wintypes.WORD),
                ("ByteSize", wintypes.BYTE), ("Parity", wintypes.BYTE),
                ("StopBits", wintypes.BYTE), ("XonChar", ctypes.c_char),
                ("XoffChar", ctypes.c_char), ("ErrorChar", ctypes.c_char),
                ("EofChar", ctypes.c_char), ("EvtChar", ctypes.c_char),
                ("wReserved1", wintypes.WORD),
            ]

        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        k.GetCommState.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.GetCommState.restype = wintypes.BOOL
        if not k.GetCommState(self.h, ctypes.byref(dcb)):
            raise OSError(ctypes.get_last_error(), "GetCommState")
        dcb.BaudRate = int(baud)
        dcb.ByteSize = int(bytesize)
        dcb.Parity = {"N": 0, "O": 1, "E": 2, "M": 3, "S": 4}.get(parity, 0)
        dcb.StopBits = {1: 0, 1.5: 1, 2: 2}.get(stopbits, 0)
        dcb.flags = 1 | (2 if parity != "N" else 0)  # fBinary, och fParity när paritet används
        k.SetCommState.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.SetCommState.restype = wintypes.BOOL
        if not k.SetCommState(self.h, ctypes.byref(dcb)):
            raise OSError(ctypes.get_last_error(), "SetCommState")

    def _timeouts(self):
        import ctypes
        from ctypes import wintypes

        class TIMEOUTS(ctypes.Structure):
            _fields_ = [("ReadIntervalTimeout", wintypes.DWORD),
                        ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
                        ("ReadTotalTimeoutConstant", wintypes.DWORD),
                        ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
                        ("WriteTotalTimeoutConstant", wintypes.DWORD)]

        t = TIMEOUTS(20, 0, 200, 0, 1000)
        self._k.SetCommTimeouts.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._k.SetCommTimeouts.restype = wintypes.BOOL
        self._k.SetCommTimeouts(self.h, ctypes.byref(t))
        self._k.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self._k.ReadFile.restype = wintypes.BOOL
        self._k.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self._k.WriteFile.restype = wintypes.BOOL

    def read(self, n=4096):
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_string_buffer(n)
        got = wintypes.DWORD()
        ok = self._k.ReadFile(self.h, buf, n, ctypes.byref(got), None)
        if not ok or not got.value:
            return b""
        return buf.raw[:got.value]

    def write(self, data):
        import ctypes
        from ctypes import wintypes
        if not data:
            return
        buf = ctypes.create_string_buffer(data)
        wrote = wintypes.DWORD()
        ok = self._k.WriteFile(self.h, buf, len(data), ctypes.byref(wrote), None)
        if not ok:
            raise OSError(ctypes.get_last_error(), "WriteFile")

    def close(self):
        if self.h:
            try:
                self._k.CloseHandle(self.h)
            except Exception:
                pass
            self.h = None


def ensure_virtual_pair(prefer=()):
    """com0com-par. Returnerar (porten TIA öppnar, porten bryggan öppnar)."""
    setupc = find_setupc()
    if not setupc:
        raise RuntimeError(
            "com0com saknas. Installera com0com på verktygssidan så TIA får en lokal COM-port.")
    app, bridge = (prefer or ("", ""))
    rc, out, err = ztctl.run([setupc, "list"])
    text = (out or "") + (err or "")
    if app and bridge and app in text and bridge in text:
        return app, bridge
    used = set(list_com_ports())
    for n in (80, 82, 84, 90, 92, 100):
        a, b = f"COM{n}", f"COM{n + 1}"
        if a not in used and b not in used:
            app, bridge = a, b
            break
    else:
        raise RuntimeError("Hittar inga lediga COM-nummer för com0com.")
    rc, out, err = ztctl.run([
        setupc, "install", f"PortName={app},EmuBR=yes", f"PortName={bridge},EmuBR=yes"])
    if rc != 0:
        raise RuntimeError(((out or "") + (err or "")).strip()[:300] or "setupc install misslyckades")
    log(f"com0com: {app} (TIA) ↔ {bridge} (brygga)")
    return app, bridge


def allow_firewall(port=PORT):
    ztctl.run(["netsh", "advfirewall", "firewall", "add", "rule",
                f"name=ZT-Tray-com-{port}", "dir=in", "action=allow",
                "protocol=TCP", f"localport={port}"])


class ComLink:
    def __init__(self, role, port_name, peer_ip="", baud=9600, bytesize=8,
                 parity="N", stopbits=1, listen_port=PORT, on_change=None,
                 virtual_pair=()):
        self.role = role
        self.port_name = (port_name or "").strip().upper()
        self.peer_ip = (peer_ip or "").strip()
        self.baud = int(baud or 9600)
        self.bytesize = int(bytesize or 8)
        self.parity = (parity or "N").upper()[:1]
        self.stopbits = float(stopbits or 1)
        if self.stopbits == int(self.stopbits):
            self.stopbits = int(self.stopbits)
        self.listen_port = int(listen_port or PORT)
        self.on_change = on_change
        self.virtual_pair = tuple(virtual_pair or ())
        self.app_port = ""
        self.running = False
        self.live = False
        self.status = "Stoppad"
        self.tx = 0
        self.rx = 0
        self._stop = threading.Event()
        self._threads = []
        self._listen = None
        self._sock = None
        self._ser = None
        self._rfc = Rfc2217()

    def start(self):
        if self.role == "uttag":
            if not self.port_name:
                raise RuntimeError("Välj COM-porten som kabeln sitter i.")
            ser = WinSerial()
            ser.open(self.port_name, self.baud, self.bytesize, self.parity, self.stopbits)
            self._ser = ser
            allow_firewall(self.listen_port)
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind(("0.0.0.0", self.listen_port))
            except OSError as e:
                ser.close()
                srv.close()
                raise RuntimeError(f"Kan inte lyssna på TCP {self.listen_port}: {e}") from e
            srv.listen(1)
            srv.settimeout(0.5)
            self._listen = srv
            self.live = False
            self.status = f"Delar {self.port_name}. Väntar på verktygssidan."
        else:
            if not self.peer_ip:
                raise RuntimeError("Skriv uttagsdatorns ZeroTier-IP.")
            app, bridge = ensure_virtual_pair(self.virtual_pair)
            self.app_port = app
            self.virtual_pair = (app, bridge)
            ser = WinSerial()
            ser.open(bridge, self.baud, self.bytesize, self.parity, self.stopbits)
            self._ser = ser
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(8)
            try:
                sock.connect((self.peer_ip, self.listen_port))
            except OSError as e:
                ser.close()
                sock.close()
                raise RuntimeError(f"Når inte {self.peer_ip}:{self.listen_port}: {e}") from e
            sock.settimeout(0.3)
            self._sock = sock
            try:
                sock.sendall(self._rfc.greeting())
                sock.sendall(self._rfc.command(self.baud, self.bytesize, self.parity, self.stopbits))
            except OSError as e:
                self.stop()
                raise RuntimeError(f"Kunde inte skicka portinställning: {e}") from e
            self.live = True
            self.status = f"{app} här → {self.peer_ip} ({self.baud} {self.parity}{self.bytesize})"
        self._stop.clear()
        self.running = True
        self._threads = [
            threading.Thread(target=self._accept_loop if self.role == "uttag" else self._io_loop,
                             name="com-link", daemon=True)
        ]
        if self.role != "uttag":
            self._threads.append(threading.Thread(target=self._ser_loop, name="com-ser", daemon=True))
        for t in self._threads:
            t.start()
        self._emit()
        log(f"com start: roll={self.role} port={self.port_name or self.app_port}")

    def stop(self):
        self._stop.set()
        self.running = False
        for obj in (self._sock, self._listen):
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
        self._sock = None
        self._listen = None
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads = []
        self.live = False
        self.status = "Stoppad"
        self._emit()

    def snapshot(self):
        short = ""
        live = bool(self.running and self.live)
        if self.running:
            port = self.port_name if self.role == "uttag" else (self.app_port or self.port_name)
            short = f"com {port} igång" if live else f"com {port} väntar"
        return {"running": self.running, "role": self.role,
                "iface": self.port_name or self.app_port, "status": self.status,
                "peer_ip": self.peer_ip, "peer_name": "", "tx": self.tx, "rx": self.rx,
                "dropped": 0, "peers": [], "short": short, "live": live,
                "app_port": self.app_port, "virtual_pair": self.virtual_pair}

    def set_manual_peer(self, ip):
        self.peer_ip = ip or ""

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, addr = self._listen.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log(f"com: klient {addr[0]}")
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            client.settimeout(0.3)
            self._sock = client
            try:
                client.sendall(self._rfc.greeting())
            except OSError:
                continue
            self.live = True
            self.status = f"Delar {self.port_name} med {addr[0]}"
            self._emit()
            self._io_loop()
            if self.running and not self._stop.is_set():
                self.live = False
                self.status = f"Delar {self.port_name}. Väntar igen."
                self._emit()

    def _io_loop(self):
        """TCP → serieport. På uttagssidan körs den inuti accept."""
        sock = self._sock
        while not self._stop.is_set() and sock is self._sock:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                if self.role == "uttag":
                    self._flush_serial()
                continue
            except OSError:
                break
            if not data:
                break
            payload = self._rfc.feed(data)
            if self._rfc.replies:
                try:
                    sock.sendall(bytes(self._rfc.replies))
                except OSError:
                    break
                self._rfc.replies.clear()
            if self._rfc.changed and self._ser and self.role == "uttag":
                self._rfc.changed = False
                s = self._rfc.settings
                try:
                    self._ser.configure(s["baud"], s["bytesize"], s["parity"], s["stopbits"])
                    self.baud = s["baud"]
                    self.status = f"Delar {self.port_name} med {self.peer_ip or 'verktyg'} ({s['baud']} {s['parity']}{s['bytesize']})"
                    self._emit()
                except OSError as e:
                    log(f"com configure: {e}")
            if payload and self._ser:
                try:
                    self._ser.write(payload)
                    self.rx += len(payload)
                except OSError as e:
                    log(f"com write: {e}")
                    break
            if self.role == "uttag":
                self._flush_serial()
        if self.role != "uttag" and not self._stop.is_set():
            self.live = False
            self.status = "Förbindelsen bröts"
            self._emit()

    def _ser_loop(self):
        while not self._stop.is_set():
            self._flush_serial()
            time.sleep(0.02)

    def _flush_serial(self):
        if not self._ser or not self._sock:
            return
        try:
            data = self._ser.read(4096)
        except OSError:
            return
        if not data:
            return
        try:
            self._sock.sendall(self._rfc.escape(data))
            self.tx += len(data)
        except OSError:
            pass

    def _emit(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass


def selftest():
    r = Rfc2217()
    payload = r.feed(r.greeting() + r.command(19200, 8, "E", 1) + b"hej" + bytes([IAC, IAC]) + b"!")
    assert payload == b"hej" + bytes([IAC]) + b"!"
    assert r.settings["baud"] == 19200
    assert r.settings["parity"] == "E"
    assert r.settings["stopbits"] == 1
    assert r.changed
    assert r.replies[0] == IAC and SET_BAUD + 100 in r.replies
    assert Rfc2217().escape(bytes([IAC, 1])) == bytes([IAC, IAC, 1])
    log("serialshare selftest OK")


if __name__ == "__main__":
    selftest()
