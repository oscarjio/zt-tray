"""
Hitta IP på enheten i andra änden av kabeln (kräver Npcap + scapy).

Tre metoder samtidigt medan vi lyssnar på kortet:
  1. Passivt: all ARP/IP-trafik från andra MAC-adresser avslöjar deras IP.
  2. Broadcast-ping (255.255.255.255 och x.x.x.255) med avsändare i varje kandidatnät.
  3. ARP-svep: "who-has" för alla adresser i kandidatnäten, plus typiska
     PLC/HMI-adresser (.1 .2 .10 .39 .100 .250 .254) i alla 192.168.0–255.
     ARP svaras av nästan alla enheter, även de som ignorerar ping.

Allt skickas på lager 2, så det fungerar oavsett vilken IP datorn har just nu.
"""

import threading
import time

from config import log

COMMON_HOSTS = (1, 2, 10, 39, 100, 250, 254)
_scapy_lock = threading.Lock()
_scapy = None

# IEEE-prefix (hex utan skiljetecken). Längst prefix först vid matchning.
# Siemens-blocken är hela företagets, så en Scalance-switch får samma träff som en S7.
# Mitsubishi-blocken används av FX5 (handboken visar 00:26:92) och av andra MELSEC-enheter.
_KINDS = (
    ("siemens", "Siemens PLC", (
        "0001E3", "000BA3", "000E8C", "001106", "001B1B", "080006",
        "10DFFC", "208756", "20A8B9", "286336", "302F1E", "30B851",
        "384B24", "3CFFE4", "40ECF8", "74FC45", "883F99", "A86D04",
        "AC6417", "B87AC9", "D0EB1A", "D4F527", "EC1C5D",
        "F040AFA",          # 28-bitarsblock
        "70B3D5C87", "70B3D5CCA",
    )),
    ("fx5", "FX5 PLC", (
        "002692", "104B46", "28E98E", "30BE3B", "38E08E", "58528A", "94A4B5",
    )),
    ("beijer", "Beijer-skärm", (
        "001277",   # Beijer Electronics Corp
        "00506C",   # Beijer Electronics Products AB
    )),
)
_PREFIXES = tuple(sorted(
    ((p, kind, label) for kind, label, prefs in _KINDS for p in prefs),
    key=lambda row: len(row[0]), reverse=True))


def mac_hex(mac):
    return "".join(c for c in str(mac) if c in "0123456789abcdefABCDEF").upper()


def identify(mac):
    """Returnera (slag, etikett) från MAC. Tomt om prefixet inte är känt.

    Lokalt tilldelade adresser (andra tecknet 2, 6, A eller E) har inget tillverkarprefix.
    """
    h = mac_hex(mac)
    if len(h) != 12:
        return "", ""
    if int(h[1], 16) & 0x2:
        return "", ""
    for prefix, kind, label in _PREFIXES:
        if h.startswith(prefix):
            return kind, label
    return "", ""


def load_scapy():
    """Importera scapy (tar någon sekund första gången)."""
    global _scapy
    with _scapy_lock:
        if _scapy is None:
            import logging
            logging.getLogger("scapy").setLevel(logging.ERROR)
            from scapy import all as sc
            sc.conf.verb = 0
            _scapy = sc
    return _scapy


def warmup():
    try:
        load_scapy()
        log("scapy laddat")
    except Exception as e:
        log(f"scapy kunde inte laddas: {e}")


def resolve_iface(sc, name):
    try:
        sc.conf.ifaces.reload()
    except Exception:
        pass
    for attr in ("name", "description", "network_name"):
        for iface in sc.conf.ifaces.values():
            if getattr(iface, attr, None) == name:
                return iface
    try:
        return sc.conf.ifaces.dev_from_name(name)
    except Exception:
        raise RuntimeError(f"Hittar inte '{name}' bland Npcap-korten")


def manuf(sc, mac):
    try:
        r = sc.conf.manufdb._get_manuf(mac)
        return "" if not r or r == mac else r
    except Exception:
        return ""


class Result:
    def __init__(self, ip, mac):
        self.ip = ip
        self.mac = mac
        self.vendor = ""
        self.kind = ""
        self.label = ""
        self.how = set()
        self.count = 0
        self.first = time.time()

    @property
    def subnet24(self):
        return ".".join(self.ip.split(".")[:3])


def discover(iface_name, subnets, own_ips=(), src_host=200, duration=5.0,
             on_update=None, stop=None, full_network=False):
    """
    Söker på kortet iface_name. subnets = ['192.168.5', ...] som svepas.
    full_network sveper varje nät helt och väntar ut tiden, även efter första träff.
    on_update(dict ip->Result) anropas när något nytt hittas.
    Returnerar dict ip->Result.
    """
    sc = load_scapy()
    iface = resolve_iface(sc, iface_name)
    my_mac = (iface.mac or "").lower()
    own = set(own_ips)
    found = {}
    lock = threading.Lock()
    log(f"Söker enheter på {iface_name} ({my_mac}), nät: {subnets}")

    def handle(pkt):
        try:
            if sc.Ether not in pkt:
                return
            src = pkt[sc.Ether].src.lower()
            if src == my_mac:
                return
            ip, how = None, None
            if sc.ARP in pkt:
                a = pkt[sc.ARP]
                ip, src = a.psrc, a.hwsrc.lower()
                how = "ARP-svar" if a.op == 2 else "ARP"
            elif sc.IP in pkt:
                ip = pkt[sc.IP].src
                how = "ping-svar" if sc.ICMP in pkt and pkt[sc.ICMP].type == 0 else "trafik"
            if not ip or ip == "0.0.0.0" or ip in own or ip.startswith("255."):
                return
            new = False
            with lock:
                r = found.get(ip)
                if r is None:
                    r = found[ip] = Result(ip, src)
                    r.kind, r.label = identify(src)
                    r.vendor = r.label or manuf(sc, src)
                    new = True
                r.count += 1
                r.how.add(how)
            if new:
                log(f"  hittade {ip} {src} ({how})")
                if on_update:
                    on_update(dict(found))
        except Exception as e:
            log(f"  pkt-fel: {e}")

    sniffer = sc.AsyncSniffer(iface=iface, prn=handle, store=False, filter="arp or ip")
    sniffer.start()
    time.sleep(0.4)
    if getattr(sniffer, "exception", None) is not None or not sniffer.running:
        try:
            sniffer.stop()
        except Exception:
            pass
        # BPF-filter kunde inte kompileras — lyssna ofiltrerat (handle() sorterar ändå)
        log("  filter ej tillgängligt, lyssnar ofiltrerat")
        sniffer = sc.AsyncSniffer(iface=iface, prn=handle, store=False)
        sniffer.start()
        time.sleep(0.4)

    def stopped():
        return stop is not None and stop.is_set()

    try:
        bcast = "ff:ff:ff:ff:ff:ff"
        nets = list(dict.fromkeys(subnets))   # unika, behåll ordning

        def src_ip(net):
            return f"{net}.{src_host}"

        # 1. Broadcast-ping
        pings = [sc.Ether(src=my_mac, dst=bcast) / sc.IP(src="0.0.0.0", dst="255.255.255.255") / sc.ICMP()]
        for net in nets:
            pings.append(sc.Ether(src=my_mac, dst=bcast) / sc.IP(src=src_ip(net), dst="255.255.255.255") / sc.ICMP())
            pings.append(sc.Ether(src=my_mac, dst=bcast) / sc.IP(src=src_ip(net), dst=f"{net}.255") / sc.ICMP())
        sc.sendp(pings, iface=iface, verbose=0)

        # 2. ARP-svep i kandidatnäten (de vanligaste först)
        def arp(net, h):
            sip = src_ip(net) if h != src_host else f"{net}.{src_host + 1}"
            return sc.Ether(src=my_mac, dst=bcast) / sc.ARP(op=1, hwsrc=my_mac, psrc=sip, pdst=f"{net}.{h}")

        for net in nets:
            if stopped() or (found and not full_network):
                # Kabel direkt mot en enhet: när vi har svar räcker det med en kort extra väntan
                break
            sc.sendp([arp(net, h) for h in range(1, 255)], iface=iface, verbose=0)

        # 3. Typiska adresser i alla 192.168.x — bara när nätet är okänt
        if not full_network and not stopped() and not found:
            pk = [arp(f"192.168.{x}", h) for x in range(256) for h in COMMON_HOSTS]
            for i in range(0, len(pk), 256):
                if stopped() or found:
                    break
                sc.sendp(pk[i:i + 256], iface=iface, verbose=0)

        # Vänta kvar resten av tiden (eller kort tid om kabelsök redan hittat något)
        t_end = time.time() + (duration if full_network or not found else 1.0)
        while time.time() < t_end and not stopped():
            time.sleep(0.1)
    finally:
        try:
            sniffer.stop()
        except Exception:
            pass

    log(f"Sökning klar: {len(found)} enhet(er)")
    return found


def selftest():
    assert identify("00:1B:1B:10:20:30") == ("siemens", "Siemens PLC")
    assert identify("00-26-92-60-10-25") == ("fx5", "FX5 PLC")
    assert identify("001277010203") == ("beijer", "Beijer-skärm")
    assert identify("00:50:6C:AA:BB:CC") == ("beijer", "Beijer-skärm")
    assert identify("F0:40:AF:A1:00:01") == ("siemens", "Siemens PLC")
    assert identify("70:B3:D5:C8:70:01") == ("siemens", "Siemens PLC")
    assert identify("02:1B:1B:10:20:30") == ("", "")
    assert identify("00:11:22:33:44:55") == ("", "")
    log("discovery selftest OK")


def suggest_own_ip(device_ip, preferred_host=200):
    """Föreslå datorns IP i samma /24 som enheten."""
    parts = device_ip.split(".")
    host = preferred_host
    if int(parts[3]) == host:
        host = host + 1 if host < 254 else host - 1
    return ".".join(parts[:3] + [str(host)])
