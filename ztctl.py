"""
ZeroTier och rutter — samma logik som v1.
"""

import subprocess
import sys
from pathlib import Path

from config import log

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000

ZEROTIER_CLI = r"C:\ProgramData\ZeroTier\One\zerotier-one_x64.exe"
ZEROTIER_CLI_ALT = "zerotier-cli"


def run(args, timeout=30):
    log("CMD: " + " ".join(args))
    kw = dict(creationflags=CREATE_NO_WINDOW) if IS_WIN else {}
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout,
                           encoding="oem" if IS_WIN else "utf-8", errors="replace", **kw)
        log(f"  -> rc={r.returncode} {(r.stdout + r.stderr).strip()[:200]}")
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        log(f"  -> EXCEPTION: {e}")
        return -1, "", str(e)


def installed():
    return Path(ZEROTIER_CLI).exists()


def zt_cli(*args):
    if Path(ZEROTIER_CLI).exists():
        return run([ZEROTIER_CLI, "-q", *args])
    return run([ZEROTIER_CLI_ALT, *args])


def join(network_id):
    return zt_cli("join", network_id)[0] == 0


def leave(network_id):
    return zt_cli("leave", network_id)[0] == 0


# Statusord från zerotier-cli listnetworks. Namnet kan innehålla mellanslag,
# så status kan inte antas ligga på en fast kolumn.
_NET_STATUSES = {
    "REQUESTING_CONFIGURATION", "OK", "ACCESS_DENIED", "NOT_FOUND",
    "PORT_ERROR", "CLIENT_TOO_OLD",
}


def parse_listnetworks_line(line):
    """En rad från `zerotier-cli listnetworks`, eller None."""
    parts = line.split()
    if len(parts) < 6 or parts[0] != "200" or parts[1] != "listnetworks":
        return None
    status_i = next((i for i, p in enumerate(parts) if p in _NET_STATUSES), None)
    if status_i is None or status_i < 4:
        return None
    ips = []
    dev = ""
    for p in parts[status_i + 1:]:
        if "/" in p and p[0].isdigit():
            ips.extend(x for x in p.split(",") if x)
        elif not dev:
            dev = p
    return {
        "nwid": parts[2],
        "name": " ".join(parts[3:status_i - 1]),
        "mac": parts[status_i - 1],
        "status": parts[status_i],
        "dev": dev,
        "ips": ips,
    }


def list_networks_full():
    rc, out, _ = zt_cli("listnetworks")
    if rc != 0:
        return []
    return [r for r in (parse_listnetworks_line(ln) for ln in out.splitlines()) if r]


def list_networks():
    return [(r["nwid"], r["status"]) for r in list_networks_full()]


def my_zt_ips():
    """IPv4 som ZeroTier har lagt på den här datorn, som '10.1.2.3/24'."""
    found = []
    for net in list_networks_full():
        if net["status"] != "OK":
            continue
        for ippx in net["ips"]:
            if ":" not in ippx.split("/")[0]:
                found.append(ippx)
    return found


def is_network_ok(network_id):
    for nwid, status in list_networks():
        if nwid.lower() == network_id.lower():
            return status == "OK"
    return False


def route_add(subnet, mask, gateway):
    return run(["route", "add", subnet, "mask", mask, gateway])[0] == 0


def route_delete(subnet):
    run(["route", "delete", subnet])
    return True
