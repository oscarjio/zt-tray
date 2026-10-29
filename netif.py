"""
Nätverkskort: lista adaptrar (snabbt via GetAdaptersAddresses) och byt IP via netsh.
"""

import ctypes
import socket
import subprocess
import sys
import time

from config import log

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000

# Ord i beskrivning/namn som betyder virtuellt kort (sorteras sist)
VIRTUAL_HINTS = ("virtual", "zerotier", "hyper-v", "vmware", "virtualbox", "tap-",
                 "wintun", "wireguard", "loopback", "bluetooth", "vethernet",
                 "teredo", "isatap", "npcap", "wan miniport", "tailscale")

IF_TYPE_ETHERNET = 6
IF_TYPE_WIFI = 71
IF_TYPE_LOOPBACK = 24


class Adapter:
    def __init__(self, name, description, guid, index, up, dhcp, ips, mac, iftype):
        self.name = name                # "Ethernet" — det netsh vill ha
        self.description = description  # "Intel(R) Ethernet Connection ..."
        self.guid = guid
        self.index = index
        self.up = up                    # kabel i / länk uppe
        self.dhcp = dhcp                # DHCP aktiverat på kortet
        self.ips = ips                  # [(ip, prefix)]
        self.mac = mac
        self.iftype = iftype

    @property
    def virtual(self):
        t = f"{self.name} {self.description}".lower()
        return any(h in t for h in VIRTUAL_HINTS)

    @property
    def kind(self):
        if self.iftype == IF_TYPE_WIFI:
            return "WiFi"
        if self.iftype == IF_TYPE_ETHERNET:
            return "Ethernet"
        return "Annat"

    @property
    def primary_ip(self):
        """Första 'riktiga' IPv4 (inte APIPA om det finns annat)."""
        if not self.ips:
            return None
        for ip, p in self.ips:
            if not ip.startswith("169.254."):
                return ip, p
        return self.ips[0]

    @property
    def mode(self):
        """'down' | 'dhcp' | 'apipa' | 'static' | 'none'."""
        if not self.up:
            return "down"
        pip = self.primary_ip
        if self.dhcp:
            if pip is None or pip[0].startswith("169.254."):
                return "apipa"
            return "dhcp"
        if pip is None:
            return "none"
        return "static"

    def __repr__(self):
        return f"<Adapter {self.name} up={self.up} dhcp={self.dhcp} ips={self.ips}>"


# ---------------------------------------------------------------- ctypes

if IS_WIN:
    from ctypes import wintypes

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]

    class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
        pass

    IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
        ("Length", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("Next", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
        ("Address", SOCKET_ADDRESS),
        ("PrefixOrigin", ctypes.c_int),
        ("SuffixOrigin", ctypes.c_int),
        ("DadState", ctypes.c_int),
        ("ValidLifetime", ctypes.c_ulong),
        ("PreferredLifetime", ctypes.c_ulong),
        ("LeaseLifetime", ctypes.c_ulong),
        ("OnLinkPrefixLength", ctypes.c_ubyte),
    ]

    class IP_ADAPTER_ADDRESSES(ctypes.Structure):
        pass

    IP_ADAPTER_ADDRESSES._fields_ = [
        ("Length", ctypes.c_ulong),
        ("IfIndex", ctypes.c_ulong),
        ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("Mtu", ctypes.c_ulong),
        ("IfType", ctypes.c_ulong),
        ("OperStatus", ctypes.c_int),
    ]

    _GetAdaptersAddresses = ctypes.windll.iphlpapi.GetAdaptersAddresses
    _GetAdaptersAddresses.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    _GetAdaptersAddresses.restype = ctypes.c_ulong

AF_INET = 2
GAA_FLAG_SKIP_ANYCAST = 0x2
GAA_FLAG_SKIP_MULTICAST = 0x4
GAA_FLAG_SKIP_DNS_SERVER = 0x8
GAA_FLAG_INCLUDE_ALL_INTERFACES = 0x100
IP_ADAPTER_DHCP_ENABLED = 0x4
ERROR_BUFFER_OVERFLOW = 111


def list_adapters():
    """Alla IPv4-adaptrar utom loopback. Tar normalt några millisekunder."""
    if not IS_WIN:
        return []
    size = ctypes.c_ulong(16 * 1024)
    flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        rc = _GetAdaptersAddresses(AF_INET, flags, None, buf, ctypes.byref(size))
        if rc == 0:
            break
        if rc != ERROR_BUFFER_OVERFLOW:
            log(f"GetAdaptersAddresses rc={rc}")
            return []
    else:
        return []

    out = []
    p = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while p:
        a = p.contents
        if a.IfType != IF_TYPE_LOOPBACK:
            ips = []
            u = a.FirstUnicastAddress
            while u:
                ua = u.contents
                sa = ua.Address.lpSockaddr
                if sa:
                    raw = ctypes.string_at(sa, 8)
                    family = int.from_bytes(raw[0:2], "little")
                    if family == AF_INET:
                        ips.append((socket.inet_ntoa(raw[4:8]), int(ua.OnLinkPrefixLength)))
                u = ua.Next
            mac = ":".join(f"{a.PhysicalAddress[i]:02x}" for i in range(min(a.PhysicalAddressLength, 8)))
            out.append(Adapter(
                name=a.FriendlyName or "",
                description=a.Description or "",
                guid=(a.AdapterName or b"").decode(errors="replace"),
                index=a.IfIndex,
                up=(a.OperStatus == 1),
                dhcp=bool(a.Flags & IP_ADAPTER_DHCP_ENABLED),
                ips=ips,
                mac=mac,
                iftype=a.IfType,
            ))
        p = a.Next
    out.sort(key=lambda x: (x.virtual, x.iftype != IF_TYPE_ETHERNET, not x.up, x.name.lower()))
    return out


def find_adapter(name, adapters=None):
    for a in (adapters if adapters is not None else list_adapters()):
        if a.name == name:
            return a
    return None


def default_interface(adapters):
    """Första fysiska Ethernet, annars första icke-virtuella."""
    for a in adapters:
        if a.iftype == IF_TYPE_ETHERNET and not a.virtual:
            return a.name
    for a in adapters:
        if not a.virtual:
            return a.name
    return adapters[0].name if adapters else ""


# ---------------------------------------------------------------- netsh

def _run(args, wait=True):
    log("CMD: " + " ".join(f'"{a}"' if " " in a else a for a in args))
    kw = dict(creationflags=CREATE_NO_WINDOW) if IS_WIN else {}
    if not wait:
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        except Exception as e:
            log(f"  -> EXCEPTION: {e}")
        return 0, ""
    try:
        r = subprocess.run(args, capture_output=True, timeout=30,
                           encoding="oem" if IS_WIN else "utf-8", errors="replace", **kw)
        out = (r.stdout + r.stderr).strip()
        log(f"  -> rc={r.returncode} {out[:300]}")
        return r.returncode, out
    except Exception as e:
        log(f"  -> EXCEPTION: {e}")
        return -1, str(e)


# Positionella argument: namnet blir ett eget argument som Windows citerar korrekt
# även när det innehåller mellanslag ("Ethernet 2").

def build_static_cmd(name, ip, mask, gateway=""):
    return ["netsh", "interface", "ipv4", "set", "address",
            name, "static", ip, mask, gateway or "none"]


def build_dhcp_cmds(name):
    return (["netsh", "interface", "ipv4", "set", "address", name, "dhcp"],
            ["netsh", "interface", "ipv4", "set", "dnsservers", name, "dhcp"])


def set_static(name, ip, mask, gateway=""):
    """Sätt statisk IP. Returnerar (ok, meddelande, sekunder)."""
    t0 = time.perf_counter()
    cur = find_adapter(name)
    if cur and not cur.dhcp and any(i == ip for i, _ in cur.ips):
        # Samma adress redan satt — netsh skulle klaga, och det är redan klart
        if len(cur.ips) == 1:
            return True, "Redan satt", time.perf_counter() - t0
    rc, out = _run(build_static_cmd(name, ip, mask, gateway))
    dt = time.perf_counter() - t0
    if rc == 0:
        return True, "OK", dt
    return False, out or f"netsh rc={rc}", dt


def set_dhcp(name):
    t0 = time.perf_counter()
    addr_cmd, dns_cmd = build_dhcp_cmds(name)
    rc, out = _run(addr_cmd)
    # DNS i bakgrunden — påverkar inte adressen och ska inte fördröja bytet
    _run(dns_cmd, wait=False)
    dt = time.perf_counter() - t0
    if rc == 0:
        return True, "OK", dt
    low = out.lower()
    # "DHCP is already enabled" / "DHCP är redan aktiverat" — räknas som lyckat
    if "dhcp" in low and ("already" in low or "redan" in low):
        return True, "Redan DHCP", dt
    return False, out or f"netsh rc={rc}", dt


def open_adapter_settings():
    if IS_WIN:
        _run(["control.exe", "ncpa.cpl"], wait=False)


def open_hardware_wizard():
    """Guiden där man lägger till Microsoft KM-TEST Loopback Adapter."""
    if IS_WIN:
        _run(["hdwwiz.exe"], wait=False)


TRANSPORT_HINTS = ("zerotier", "tailscale", "wintun", "wireguard")


def is_transport_adapter(adapter):
    """ZeroTier/Tailscale är vägen tunneln går. Den får inte vara själva uttaget."""
    text = f"{adapter.name} {adapter.description}".lower()
    return any(h in text for h in TRANSPORT_HINTS)


def outlet_adapters(adapters=None):
    ads = adapters if adapters is not None else list_adapters()
    return [a for a in ads if not is_transport_adapter(a)]


# ---------------------------------------------------------------- IP-släpp (uttag hos kund)

def _restore_path():
    from config import APP_DIR
    return APP_DIR / "uttag-ip-restore.json"


def snapshot_ip(name):
    a = find_adapter(name)
    if a is None:
        return None
    return {"interface": name, "dhcp": bool(a.dhcp), "ips": [[ip, p] for ip, p in a.ips]}


def save_ip_restore(snap):
    """Skriv innan adressen tas bort, så en krasch kan återställa vid nästa start."""
    if not snap:
        return
    import json
    path = _restore_path()
    path.write_text(json.dumps(snap), encoding="utf-8")


def clear_ipv4(name):
    """Ta bort IPv4 men lämna länken uppe. Returnerar (ok, text)."""
    alias = name.replace("'", "''")
    ps = (
        f"$e='SilentlyContinue'; "
        f"Set-NetIPInterface -InterfaceAlias '{alias}' -AddressFamily IPv4 -Dhcp Disabled -ErrorAction $e; "
        f"Get-NetIPAddress -InterfaceAlias '{alias}' -AddressFamily IPv4 -ErrorAction $e | "
        f"Remove-NetIPAddress -Confirm:$false -ErrorAction $e"
    )
    rc, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    return rc == 0, out


def restore_pending_ipv4():
    """Lägg tillbaka adressen om en tidigare delning inte hann återställa. Namn eller ''."""
    import json
    path = _restore_path()
    if not path.exists():
        return ""
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"Kunde inte läsa IP-återställning: {e}")
        return ""
    name = snap.get("interface") or ""
    if not name:
        return ""
    if snap.get("dhcp"):
        ok, _msg, _dt = set_dhcp(name)
    elif snap.get("ips"):
        from config import prefix_to_mask
        ip, prefix = snap["ips"][0]
        ok, _msg, _dt = set_static(name, ip, prefix_to_mask(prefix), "")
    else:
        ok = True
    if ok:
        try:
            path.unlink()
        except OSError:
            pass
        return name
    log(f"IP-återställning kvar på disk för {name}")
    return ""
