"""
Konfig fÃƒÂ¶r ZT-Tray v2.

Samma JSON-format som v1 ("sites" ligger kvar ofÃƒÂ¶rÃƒÂ¤ndrat). Nya nycklar lÃƒÂ¤ggs till
med standardvÃƒÂ¤rden nÃƒÂ¤r de saknas, sÃƒÂ¥ en gammal config.json frÃƒÂ¥n v1 fungerar direkt.
"""

import copy
import ipaddress
import json
import os
import sys
import time
from pathlib import Path

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parent

CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "zt-tray.log"
APP_NAME = "ZT-Tray"
APP_VERSION = "2.5.1"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- standard

def _p(name, network, host=200, prefix=24, gateway=""):
    return {"name": name, "network": network, "host": host,
            "prefix": prefix, "gateway": gateway}


DEFAULT_CONFIG = {
    "sites": [
        {
            "name": "Exempelkund",
            "network_id": "0123456789abcdef",
            "router_zt_ip": "10.244.20.5",
            "remote_subnet": "192.168.10.0",
            "subnet_mask": "255.255.255.0",
        }
    ],
    # IP-profiler. "?" i network eller host = frÃƒÂ¥gas vid varje byte.
    "ip_presets": [
        _p("192.168.5.x", "192.168.5"),
        _p("192.168.0.x", "192.168.0"),
        _p("192.168.1.x", "192.168.1"),
        _p("192.168.3.x", "192.168.3"),
        _p("192.168.10.x", "192.168.10"),
        _p("10.20.?.x", "10.20.?"),
    ],
    "settings": {
        "interface": "",          # tomt = vÃƒÂ¤ljs automatiskt (fÃƒÂ¶rsta fysiska Ethernet)
        "notifications": True,
        "pin_tray": True,
    },
    "discovery": {
        "duration": 5,
        # NÃƒÂ¤t som ARP-svepas helt (utÃƒÂ¶ver de som finns i IP-profilerna)
        "extra_subnets": ["192.168.2", "192.168.100", "192.168.178",
                          "192.168.250", "10.0.0", "10.10.10", "172.16.0"],
    },
    "last_values": {},            # senast inmatade vÃƒÂ¤rde per "?"-fÃƒÂ¤lt
    # Emulerat uttag mot en annan ZT-Tray. IP sÃƒÂ¤tts bara nÃƒÂ¤r role ÃƒÂ¤r "verktyg".
    "uttag": {
        "role": "uttag",          # "uttag" = kabeln sitter hÃƒÂ¤r, "verktyg" = TIA/GX kÃƒÂ¶rs hÃƒÂ¤r
        "interface": "",
        "peer_ip": "",
        "tool_ip": "",
        "tool_prefix": 24,
        "port": 47921,
        "mode": "net",             # net | usb | com
        "usb_busid": "",
        "com_port": "",
        "com_baud": 9600,
        "com_parity": "N",
        "com_data": 8,
        "com_stop": 1,
        "com_app_port": "",
        "com_bridge_port": "",
        "pin": "",
    },
}


def _merge(defaults, data):
    """Fyll i saknade nycklar frÃƒÂ¥n defaults (rekursivt fÃƒÂ¶r dictar)."""
    out = copy.deepcopy(data)
    for k, v in defaults.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(out[k], dict) and k != "last_values":
            out[k] = _merge(v, out[k])
    return out


def load_config():
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"Kunde inte lÃƒÂ¤sa config ({e}) Ã¢â‚¬â€ anvÃƒÂ¤nder standard")
        return copy.deepcopy(DEFAULT_CONFIG)
    return _merge(DEFAULT_CONFIG, data)


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- IP-hjÃƒÂ¤lp

def prefix_to_mask(prefix):
    return str(ipaddress.IPv4Network(f"0.0.0.0/{int(prefix)}").netmask)


def mask_to_prefix(mask):
    return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen


def preset_parts(preset):
    """['192','168','5','200'] Ã¢â‚¬â€ kan innehÃƒÂ¥lla '?'."""
    net = [p.strip() for p in str(preset.get("network", "")).split(".") if p.strip() != ""]
    host = [p.strip() for p in str(preset.get("host", "")).split(".") if p.strip() != ""]
    return net + host


def preset_questions(preset):
    """Index fÃƒÂ¶r de oktetter som ska frÃƒÂ¥gas."""
    return [i for i, p in enumerate(preset_parts(preset)) if p == "?"]


def preset_label(preset):
    parts = preset_parts(preset)
    return f"{'.'.join(parts)} /{preset.get('prefix', 24)}"


def valid_octet(s):
    s = str(s).strip()
    return s.isdigit() and len(s) <= 3 and 0 <= int(s) <= 255


def resolve_preset(preset, answers=None):
    """
    Returnera (ip, mask, gateway). answers = {index: "45"} fÃƒÂ¶r '?'-fÃƒÂ¤lt.
    Gateway fÃƒÂ¥r ocksÃƒÂ¥ innehÃƒÂ¥lla '?', dÃƒÂ¥ ÃƒÂ¥teranvÃƒÂ¤nds svaren pÃƒÂ¥ samma position.
    Kastar ValueError om nÃƒÂ¥got inte stÃƒÂ¤mmer.
    """
    answers = answers or {}
    parts = preset_parts(preset)
    if len(parts) != 4:
        raise ValueError(f"Profilen '{preset.get('name')}' ger inte fyra oktetter: {'.'.join(parts)}")
    filled = []
    for i, p in enumerate(parts):
        v = answers.get(i, p) if p == "?" else p
        if not valid_octet(v):
            raise ValueError(f"Ogiltig oktett: '{v}'")
        filled.append(str(int(v)))
    ip = ".".join(filled)
    prefix = int(preset.get("prefix", 24))
    mask = prefix_to_mask(prefix)
    gw = str(preset.get("gateway", "") or "").strip()
    if gw:
        gparts = gw.split(".")
        gparts = [answers.get(i, filled[i]) if g == "?" else g for i, g in enumerate(gparts)]
        gw = ".".join(gparts)
        ipaddress.IPv4Address(gw)
    iface = ipaddress.IPv4Interface(f"{ip}/{prefix}")
    if iface.ip in (iface.network.network_address, iface.network.broadcast_address) and prefix < 31:
        raise ValueError(f"{ip} ÃƒÂ¤r nÃƒÂ¤t- eller broadcastadress fÃƒÂ¶r /{prefix}")
    return ip, mask, gw


def preset_matches(preset, ip, prefix):
    """StÃƒÂ¤mmer nuvarande ip/prefix med profilen? '?' matchar allt."""
    parts = preset_parts(preset)
    if len(parts) != 4 or int(preset.get("prefix", 24)) != int(prefix):
        return False
    for p, v in zip(parts, ip.split(".")):
        if p != "?" and p != v:
            return False
    return True


def preset_subnet(preset, last_values=None):
    """'192.168.5' om profilens nÃƒÂ¤t ÃƒÂ¤r kÃƒÂ¤nt (eller kÃƒÂ¤nt via senaste svar), annars None."""
    parts = preset_parts(preset)
    if len(parts) != 4 or int(preset.get("prefix", 24)) != 24:
        return None
    net = []
    for i, p in enumerate(parts[:3]):
        if p == "?":
            v = (last_values or {}).get(f"{preset.get('name')}#{i}")
            if v is None:
                return None
            p = str(v)
        net.append(p)
    return ".".join(net)
