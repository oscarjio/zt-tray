"""
Passiv Modbus RTU-busmonitor för ZT-Tray.

Lyssnar på en RS485-buss via en USB-RS485-adapter och SÄNDER ALDRIG NÅGOT.
Delar upp trafiken i ramar, avkodar dem, parar ihop fråga->svar och flaggar fel
(timeout, CRC, exception, fel adress, dubbelsvar/adresskrock, skräp, för kort tystnad,
master som sänder innan svaret är klart). Statistik per slav, loggfil (rå hex med
tidsstämplar) som kan spelas upp igen, CSV-export och felfälla.

Fristående:
    python modbus_monitor.py --port COM5 --baud 19200 --parity E
    python modbus_monitor.py --port COM5 --auto
    python modbus_monitor.py --replay logg.zmlog --only-errors
    python modbus_monitor.py --simulate            (syntetisk buss, ingen hårdvara)
    python modbus_monitor.py --port rfc2217://10.244.17.5:47922 --baud 19200 --parity E

Begränsning: USB-adaptrar levererar bytes i block (FTDI: latency timer, standard 16 ms).
Tystnaden mellan ramar kan då inte mätas exakt. Monitorn delar därför ramar både på
tystnad OCH genom att tolka längd per funktionskod + CRC. Svarstider blir ungefärliga
(±latency). Sätt FTDI latency timer till 1 ms (Enhetshanteraren -> porten ->
Port Settings -> Advanced -> Latency Timer) för bästa upplösning.
"""

import argparse
import collections
import csv
import datetime as _dt
import os
import socket
import sys
import threading
import time

FC_NAMES = {
    0x01: "Läs coils", 0x02: "Läs diskreta in", 0x03: "Läs holding-reg",
    0x04: "Läs input-reg", 0x05: "Skriv coil", 0x06: "Skriv register",
    0x07: "Läs exception-status", 0x08: "Diagnostik", 0x0B: "Händelseräknare",
    0x0F: "Skriv flera coils", 0x10: "Skriv flera reg", 0x11: "Rapportera slav-ID",
    0x16: "Maska register", 0x17: "Läs/skriv flera reg", 0x2B: "Enhets-ID (MEI)",
}
EXC_NAMES = {
    1: "Otillåten funktion", 2: "Otillåten dataadress", 3: "Otillåtet datavärde",
    4: "Slavfel", 5: "Kvittens (bearbetar)", 6: "Slav upptagen",
    8: "Minnesparitetsfel", 10: "Gateway: väg saknas", 11: "Gateway: målet svarar inte",
}
BAUDS = (9600, 19200, 38400, 57600, 115200, 4800, 2400, 1200)
PARITIES = ("N", "E", "O")
STOPS = (1, 2)


# ------------------------------------------------------------------ CRC / byggare

def _mk_table():
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        t.append(c)
    return t


_CRC_T = _mk_table()


def crc16(data):
    c = 0xFFFF
    for b in data:
        c = (c >> 8) ^ _CRC_T[(c ^ b) & 0xFF]
    return c


def with_crc(body):
    c = crc16(body)
    return bytes(body) + bytes((c & 0xFF, c >> 8))


def crc_ok(frame):
    return len(frame) >= 4 and crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8))


def char_time(baud, parity="N", stop=1):
    return (1 + 8 + (0 if parity == "N" else 1) + stop) / float(baud)


def t35(baud, parity="N", stop=1):
    """Tystnad som avslutar en ram. Över 19200 baud anger specen fast 1,75 ms."""
    if baud > 19200:
        return 0.00175
    return 3.5 * char_time(baud, parity, stop)


# ------------------------------------------------------------------ ramlängder

def req_len(b, i):
    """Förväntad längd om ramen vid b[i] är en FRÅGA, eller None."""
    n = len(b) - i
    if n < 2:
        return None
    fc = b[i + 1]
    if fc in (1, 2, 3, 4, 5, 6):
        return 8
    if fc in (0x0F, 0x10):
        return 9 + b[i + 6] if n > 6 else -1
    if fc == 0x17:
        return 13 + b[i + 10] if n > 10 else -1
    if fc in (7, 0x0B, 0x11):
        return 4
    if fc == 0x08:
        return 8
    if fc == 0x16:
        return 10
    if fc == 0x2B:
        return 7
    return None


def resp_len(b, i):
    """Förväntad längd om ramen vid b[i] är ett SVAR, eller None."""
    n = len(b) - i
    if n < 2:
        return None
    fc = b[i + 1]
    if fc & 0x80:
        return 5
    if fc in (1, 2, 3, 4, 0x17, 0x11):
        return 5 + b[i + 2] if n > 2 else -1
    if fc in (5, 6, 0x0F, 0x10, 0x08):
        return 8
    if fc == 7:
        return 5
    if fc == 0x0B:
        return 8
    if fc == 0x16:
        return 10
    if fc == 0x2B:
        return 0   # variabel, sök CRC
    return None


def match_at(b, i, prefer_resp=False):
    """Längd på en giltig ram (CRC ok) som börjar vid b[i], annars 0."""
    n = len(b) - i
    if n < 4 or b[i] > 247:
        return 0
    cands = []
    order = (resp_len, req_len) if prefer_resp else (req_len, resp_len)
    for f in order:
        L = f(b, i)
        if L is None:
            continue
        if L == 0:   # variabel längd: sök
            for k in range(5, min(n, 256) + 1):
                if crc_ok(b[i:i + k]):
                    return k
            continue
        if 4 <= L <= n and L not in cands:
            cands.append(L)
    for L in cands:
        if crc_ok(b[i:i + L]):
            return L
    if not cands and (b[i + 1] & 0x7F) not in FC_NAMES:
        return 0
    return 0


def is_resp_shape(f):
    L = resp_len(f, 0)
    return L == len(f) or (L == 0 and len(f) >= 5)


def is_req_shape(f):
    return req_len(f, 0) == len(f)


# ------------------------------------------------------------------ avkodning

def _regs(data):
    return [(data[k] << 8) | data[k + 1] for k in range(0, len(data) - 1, 2)]


def _short(vals, n=8):
    s = ", ".join(str(v) for v in vals[:n])
    return s + (" …" if len(vals) > n else "")


def describe(f, is_resp):
    a, fc = f[0], f[1]
    if fc & 0x80:
        code = f[2] if len(f) > 2 else 0
        return f"EXCEPTION fc {fc & 0x7F:02X}: {code} {EXC_NAMES.get(code, 'okänd kod')}"
    name = FC_NAMES.get(fc, f"fc {fc:02X}")
    try:
        if not is_resp:
            if fc in (1, 2, 3, 4):
                return f"{name} adr {(f[2] << 8) | f[3]} antal {(f[4] << 8) | f[5]}"
            if fc == 5:
                v = (f[4] << 8) | f[5]
                return f"{name} adr {(f[2] << 8) | f[3]} = {'PÅ' if v == 0xFF00 else 'AV' if v == 0 else hex(v)}"
            if fc == 6:
                return f"{name} adr {(f[2] << 8) | f[3]} = {(f[4] << 8) | f[5]}"
            if fc == 0x10:
                return f"{name} adr {(f[2] << 8) | f[3]} antal {(f[4] << 8) | f[5]}: {_short(_regs(f[7:-2]))}"
            if fc == 0x0F:
                return f"{name} adr {(f[2] << 8) | f[3]} antal {(f[4] << 8) | f[5]}"
            if fc == 0x17:
                return (f"{name} läs {(f[2] << 8) | f[3]}x{(f[4] << 8) | f[5]} "
                        f"skriv {(f[6] << 8) | f[7]}x{(f[8] << 8) | f[9]}: {_short(_regs(f[11:-2]))}")
            if fc == 0x2B:
                return f"{name} typ {f[2]:02X} kod {f[3]} objekt {f[4]}"
            return name
        if fc in (3, 4, 0x17):
            return f"svar {f[2] // 2} reg: {_short(_regs(f[3:-2]))}"
        if fc in (1, 2):
            return f"svar {f[2]} byte bitar: {f[3:-2].hex(' ')}"
        if fc in (5, 6):
            return f"kvittens adr {(f[2] << 8) | f[3]} = {(f[4] << 8) | f[5]}"
        if fc in (0x0F, 0x10):
            return f"kvittens adr {(f[2] << 8) | f[3]} antal {(f[4] << 8) | f[5]}"
        if fc == 0x2B:
            txt = bytes(c for c in f[8:-2] if 32 <= c < 127).decode(errors="ignore")
            return f"svar enhets-ID {txt[:40]}"
    except IndexError:
        pass
    return f"{'svar ' if is_resp else ''}{name}"


# ------------------------------------------------------------------ händelser och statistik

class Event:
    __slots__ = ("t", "kind", "level", "slave", "fc", "text", "raw", "rtt", "asked", "req")

    def __init__(self, t, kind, level, slave, fc, text, raw=b"", rtt=None, asked=None, req=None):
        self.t, self.kind, self.level, self.slave = t, kind, level, slave
        self.fc, self.text, self.raw, self.rtt = fc, text, bytes(raw), rtt
        self.asked = slave if asked is None else asked
        self.req = req

    @property
    def status(self):
        return ev_status(self)

    @property
    def responder(self):
        return ev_responder(self)

    def line(self, t0=0.0):
        s = "-" if self.slave is None else str(self.slave)
        fc = "--" if self.fc is None else f"{self.fc:02X}"
        rtt = f" {self.rtt * 1000:6.1f}ms" if self.rtt is not None else ""
        if self.asked is not None and self.asked != self.slave:
            s += f" (fr\u00e5gad {self.asked})"
        return f"{self.t - t0:10.4f} {KIND_TEXT.get(self.kind, self.kind):<10} slav {s:>3} fc {fc}{rtt}  {self.text}  [{self.raw.hex(' ')}]"


KIND_TEXT = {
    "req": "FRÅGA", "resp": "SVAR", "exc": "EXCEPTION", "timeout": "TIMEOUT", "crc": "CRC-FEL",
    "junk": "SKRÄP", "wrongaddr": "FEL ADR", "dup": "DUBBELSVAR", "early": "TIDIG",
    "gap": "KORT PAUS", "bcast": "BROADCAST", "info": "INFO",
    "late": "SENT SVAR", "collision": "KROCK",
}
ERROR_KINDS = ("timeout", "crc", "junk", "wrongaddr", "dup", "early", "exc", "late", "collision")

STATUS_OF = {"req": "ok", "resp": "ok", "bcast": "ok", "info": "ok", "timeout": "timeout", "early": "timeout",
             "late": "late", "collision": "krock", "crc": "crc", "exc": "exc", "wrongaddr": "feladr",
             "dup": "dubbel", "junk": "skrap", "gap": "gap"}
STATUS_TEXT = {"ok": "OK", "timeout": "INGET SVAR", "late": "SENT SVAR", "krock": "KROCK", "crc": "CRC-FEL",
               "exc": "EXCEPTION", "feladr": "FEL ADRESS", "dubbel": "DUBBELSVAR", "skrap": "SKR\u00c4P",
               "gap": "KORT PAUS"}
RESPONDER_KINDS = ("resp", "exc", "late", "wrongaddr", "dup", "crc")


def ev_status(ev):
    return STATUS_OF.get(ev.kind, ev.kind)


def ev_responder(ev):
    return ev.slave if ev.kind in RESPONDER_KINDS else None


def req_reg(raw):
    """(startadress, antal) ur en fr\u00e5ga (fc 1-6, 15, 16)."""
    if raw and len(raw) >= 6 and raw[1] in (1, 2, 3, 4, 5, 6, 15, 16):
        start = (raw[2] << 8) | raw[3]
        n = 1 if raw[1] in (5, 6) else (raw[4] << 8) | raw[5]
        return start, n
    return None


def ev_regs(ev):
    return req_reg(ev.req if ev.req else (ev.raw if ev.kind in ("req", "bcast") else b""))


_STATUS_ALIASES = {"timeout": "timeout", "ingetsvar": "timeout", "late": "late", "sent": "late", "krock": "krock",
                   "collision": "krock", "crc": "crc", "exc": "exc", "exception": "exc", "feladr": "feladr",
                   "wrongaddr": "feladr", "ok": "ok", "dubbel": "dubbel", "dup": "dubbel", "skrap": "skrap",
                   "junk": "skrap", "gap": "gap"}
_FIELDS = ("addr", "adr", "slav", "slave", "asked", "fragad", "resp", "svarande", "fc", "ms", "rtt",
           "status", "st", "kind", "reg")


def _field(ev, name):
    if name in ("addr", "adr", "slav", "slave"):
        return {ev.asked, ev.slave} - {None}
    if name in ("asked", "fragad"):
        return {ev.asked}
    if name in ("resp", "svarande"):
        return {ev_responder(ev)}
    if name == "fc":
        return {ev.fc}
    if name in ("ms", "rtt"):
        return {None if ev.rtt is None else ev.rtt * 1000}
    if name in ("status", "st"):
        return {ev_status(ev)}
    if name == "kind":
        return {ev.kind}
    if name == "reg":
        r = ev_regs(ev)
        return set(range(r[0], r[0] + max(1, min(r[1], 2000)))) if r else {None}
    raise ValueError(name)


def _term(t):
    import re as _re
    tl = t.lower()
    neg = tl.startswith("!") and not tl.startswith("!=")
    if neg:
        tl = tl[1:]
    mo = _re.fullmatch(r"([a-z_]+)(==|!=|>=|<=|>|<|=)(.+)", tl)
    if not mo:
        if tl in ("fel", "err", "error"):
            f = lambda ev: ev_status(ev) != "ok"
        elif tl in _STATUS_ALIASES:
            f = lambda ev, s=_STATUS_ALIASES[tl]: ev_status(ev) == s
        elif _re.fullmatch(r"\d+", tl):
            f = lambda ev, v=int(tl): v in (ev.asked, ev.slave)
        else:
            raise ValueError(f"f\u00f6rst\u00e5r inte '{t}'")
        return (lambda ev: not f(ev)) if neg else f
    name, op, val = mo.groups()
    if name not in _FIELDS:
        raise ValueError(f"ok\u00e4nt f\u00e4lt '{name}' (anv\u00e4nd addr, asked, resp, fc, reg, status, ms)")
    op = "==" if op == "=" else op
    if name in ("status", "st"):
        v = _STATUS_ALIASES.get(val, val)
    elif name == "kind":
        v = val
    elif val in ("asked", "fragad"):
        v = "asked"
    else:
        try:
            v = float(val) if "." in val else int(val, 0)
        except ValueError:
            raise ValueError(f"ogiltigt v\u00e4rde '{val}'")

    def f(ev, name=name, op=op, v=v):
        vals = _field(ev, name)
        vv = ev.asked if v == "asked" else v
        if op == "==":
            return vv in vals
        if op == "!=":
            return vv not in vals
        nums = [x for x in vals if isinstance(x, (int, float))]
        return any((x > vv) if op == ">" else (x < vv) if op == "<" else (x >= vv) if op == ">=" else (x <= vv)
                   for x in nums)
    return (lambda ev: not f(ev)) if neg else f


def parse_filter(expr):
    """Enkel filtersyntax (Wireshark-lik): addr==5  fc==3  status!=ok  timeout  reg==100  ms>50  resp!=asked.
    Mellanslag/&&/and = OCH, ||/or = ELLER, !term = INTE. Ett tal = adress. 'fel' = status!=ok.
    Returnerar predikat(ev); kastar ValueError vid fel."""
    import re as _re
    expr = (expr or "").strip()
    if not expr:
        return lambda ev: True
    groups = []
    for g in _re.split(r"\|\||\bor\b|\beller\b", expr):
        g = _re.sub(r"&&|\band\b|\boch\b", " ", g)
        g = _re.sub(r"\s*(==|!=|>=|<=|>|<|=)\s*", r"\1", g)
        terms = [_term(t) for t in g.split()]
        if terms:
            groups.append(terms)
    return lambda ev: any(all(t(ev) for t in grp) for grp in groups)


def cause_text(r):
    """Orsak i klartext ur en stats-rad."""
    parts = []
    if r.get("timeout"):
        parts.append(f"timeout/inget svar {r['timeout']}")
    if r.get("sena"):
        parts.append(f"sent svar {r['sena']}")
    if r.get("krockar"):
        parts.append(f"krock {r['krockar']}")
    if r.get("crc"):
        parts.append(f"CRC-fel {r['crc']}")
    if r.get("exception"):
        codes = ", ".join(str(k) for k in sorted(r.get("exc_koder") or {}))
        parts.append(f"exception {codes} ({r['exception']} st)" if codes else f"exception {r['exception']}")
    if r.get("feladr"):
        parts.append(f"svar fr\u00e5n annan adress {r['feladr']}")
    other = (r.get("\u00f6vrigt") or 0) - (r.get("feladr") or 0)
    if other > 0:
        parts.append(f"dubbelsvar/skr\u00e4p {other}")
    if r.get("linjefel"):
        parts.append(f"UART-fel {r['linjefel']} (paritet/stoppbit/baud?)")
    return ", ".join(parts) if parts else "OK"


def find_stops(events, quiet=2.0, n=30, at_end=False):
    """'Vad h\u00e4nde innan stoppet': tystnad > quiet s efter fel, eller PLC slutar fr\u00e5ga en adress efter fel.
    Returnerar [{typ, t, addr, first_fail, before:[Event]}]."""
    evs = list(events)
    out = []
    last_req = {}
    last_err = {}
    flagged = set()

    def add(typ, idx, addr=None):
        win = evs[max(0, idx - n + 1):idx + 1]
        errs = [e for e in win if e.kind in ERROR_KINDS]
        if not errs:
            return
        ff = errs[0].asked if errs[0].asked is not None else errs[0].slave
        out.append({"typ": typ, "t": evs[idx].t, "addr": addr, "first_fail": ff, "before": win})

    for i, e in enumerate(evs):
        if i and e.t - evs[i - 1].t > quiet:
            add("trafiken upph\u00f6rde", i - 1)
        for a in list(last_req):
            if a in last_err and a not in flagged and e.t - last_req[a] > quiet:
                flagged.add(a)
                add(f"PLC slutade fr\u00e5ga adress {a}", last_err[a], a)
        if e.kind == "req":
            last_req[e.asked] = e.t
            flagged.discard(e.asked)
        elif e.kind == "resp":
            last_err.pop(e.slave, None)
        elif e.kind in ERROR_KINDS and e.asked is not None:
            last_err[e.asked] = i
    if at_end and evs:
        add("slut p\u00e5 data", len(evs) - 1)
    return out


def device_states(rows, known, now_t=None, quiet=5.0):
    """Status per k\u00e4nd enhet + ok\u00e4nda: [(addr, namn, f\u00e4rg, text)]. f\u00e4rg: green/yellow/red/grey."""
    by = {r["slav"]: r for r in rows}
    out = []
    for a in sorted(set(known) | set(by)):
        name = known.get(a, "OK\u00c4ND")
        r = by.get(a)
        if not r or not r["fr\u00e5gor"]:
            out.append((a, name, "grey", "fr\u00e5gas aldrig"))
            continue
        recent_ok = r.get("senaste_ok_t") is not None and (now_t is None or now_t - r["senaste_ok_t"] < quiet)
        if not r["svar"] or not recent_ok:
            out.append((a, name, "red", f"fr\u00e5gas ({r['fr\u00e5gor']}) men svarar EJ"
                        + (f" - {cause_text(r)}" if cause_text(r) != "OK" else "")))
        elif r.get("sena") or r.get("missade") or r.get("krockar"):
            out.append((a, name, "yellow", f"svarar, men {cause_text(r)}"))
        else:
            out.append((a, name, "green", f"svarar ({r['svar']} svar, max {r['max'] or 0:.1f} ms)"))
    return out


def link_state(bytes_per_s, ok_frames, bad_frames):
    """L\u00e4nkindikator: (f\u00e4rg, text)."""
    tot = ok_frames + bad_frames
    pct = 100.0 * ok_frames / tot if tot else 0.0
    if bytes_per_s <= 0 and tot == 0:
        return "grey", "Inga bytes - kolla kabel/adapter/COM-port och att bussen \u00e4r aktiv"
    if ok_frames == 0 or pct < 50:
        return "red", (f"Bytes men inga giltiga ramar ({bytes_per_s:.0f} B/s, CRC-ok {pct:.0f} %) - "
                       "prova byt A/B, kolla baud/paritet (Auto-s\u00f6k)")
    return "green", f"Giltiga Modbus-ramar  {bytes_per_s:.0f} B/s  CRC-ok {pct:.1f} %"


DEMO_FAULTS = {"timeout": {5: 0.05}, "crc": {5: 0.03}, "exception": {3: 0.02},
               "dup": {2: 0.01}, "noise": 0.02, "early": {6: 0.02},
               "illegal": {5: (300,)}, "regdiff": {5: 3}, "late": {4: 0.03}, "collide": {5: 0.02},
               "stop": {5: 24.0}}


class SlaveStats:
    def __init__(self, addr):
        self.addr = addr
        self.req = self.resp = self.timeouts = self.crc = self.exc = self.other = 0
        self.late = self.coll = self.wrong = 0
        self.line_err = self.bad_resp = 0
        self.exc_codes = {}
        self.max_any = None
        self.rtts = collections.deque(maxlen=5000)
        self.last_err = ""
        self.last_err_t = None
        self.last_ok_t = None

    def add_err(self, ev):
        self.last_err = f"{KIND_TEXT.get(ev.kind, ev.kind)}: {ev.text}"
        self.last_err_t = ev.t

    def summary(self):
        r = sorted(self.rtts)
        if r:
            mn, mx = r[0] * 1000, r[-1] * 1000
            avg = sum(r) / len(r) * 1000
            p99 = r[min(len(r) - 1, int(len(r) * 0.99))] * 1000
        else:
            mn = mx = avg = p99 = None
        errs = self.timeouts + self.crc + self.exc + self.other + self.late + self.coll
        return {"slav": self.addr, "frågor": self.req, "svar": self.resp, "timeout": self.timeouts,
                "crc": self.crc, "exception": self.exc, "övrigt": self.other, "fel": errs,
                "felprocent": (100.0 * errs / self.req) if self.req else 0.0,
                "min": mn, "medel": avg, "max": mx, "p99": p99, "senaste_fel": self.last_err,
                "senaste_fel_t": self.last_err_t, "sena": self.late, "krockar": self.coll, "feladr": self.wrong,
                "missade": self.timeouts, "missade_pct": (100.0 * self.timeouts / self.req) if self.req else 0.0,
                "max_alla": None if self.max_any is None else self.max_any * 1000,
                "exc_koder": dict(self.exc_codes), "senaste_ok_t": self.last_ok_t,
                "linjefel": self.line_err, "trasiga_svar": self.bad_resp}


# ------------------------------------------------------------------ själva analysen

# ------------------------------------------------------------------ registervy

TABLE_NAMES = {"coil": "Coil (0x)", "di": "Ingång (1x)", "ir": "Inputreg (3x)", "hr": "Holdingreg (4x)"}


def _bits(data, n):
    return [(data[i // 8] >> (i % 8)) & 1 for i in range(min(n, len(data) * 8))]


def req_items(f):
    """Vad en fråga rör: lista av (tabell, adress, 'r'/'w', skrivet värde eller None)."""
    fc = f[1]
    try:
        a, n = (f[2] << 8) | f[3], (f[4] << 8) | f[5]
        if fc in (1, 2, 3, 4):
            t = {1: "coil", 2: "di", 3: "hr", 4: "ir"}[fc]
            return [(t, a + i, "r", None) for i in range(min(n, 2000))]
        if fc == 5:
            return [("coil", a, "w", 1 if n == 0xFF00 else 0)]
        if fc == 6:
            return [("hr", a, "w", n)]
        if fc == 0x0F:
            return [("coil", a + i, "w", b) for i, b in enumerate(_bits(f[7:-2], n))]
        if fc == 0x10:
            return [("hr", a + i, "w", v) for i, v in enumerate(_regs(f[7:7 + 2 * n]))]
        if fc == 0x17:
            wa, wn = (f[6] << 8) | f[7], (f[8] << 8) | f[9]
            return ([("hr", a + i, "r", None) for i in range(n)] +
                    [("hr", wa + i, "w", v) for i, v in enumerate(_regs(f[11:11 + 2 * wn]))])
    except IndexError:
        pass
    return []


class RegInfo:
    __slots__ = ("value", "reads", "writes", "errors", "last_err", "last_t", "history", "ok")

    def __init__(self):
        self.value = None
        self.reads = self.writes = self.errors = 0
        self.last_err = ""
        self.last_t = None
        self.ok = False          # har någon gång svarat utan fel
        self.history = collections.deque(maxlen=50)   # (t, gammalt, nytt)

    def set(self, t, v):
        if v != self.value:
            self.history.append((t, self.value, v))
        self.value, self.last_t, self.ok = v, t, True


class RegisterMap:
    """Per slav: alla register/coils PLC:n läser och skriver, med värden och historik."""

    def __init__(self):
        self.regs = {}    # (slav, tabell, adr) -> RegInfo

    def _r(self, s, t, a):
        k = (s, t, a)
        r = self.regs.get(k)
        if r is None:
            r = self.regs[k] = RegInfo()
        return r

    def on_resp(self, req, resp, t):
        s, fc = req[0], req[1]
        items = req_items(req)
        reads = [i for i in items if i[2] == "r"]
        vals = []
        try:
            if fc in (3, 4, 0x17):
                vals = _regs(resp[3:3 + resp[2]])
            elif fc in (1, 2):
                vals = _bits(resp[3:3 + resp[2]], len(reads))
        except IndexError:
            pass
        for k, (tb, a, _, _) in enumerate(reads):
            r = self._r(s, tb, a)
            r.reads += 1
            if k < len(vals):
                r.set(t, vals[k])
        for tb, a, rw, v in items:
            if rw == "w":
                r = self._r(s, tb, a)
                r.writes += 1
                r.set(t, v)

    def on_fail(self, req, t, why):
        for tb, a, rw, _ in req_items(req):
            r = self._r(req[0], tb, a)
            if rw == "r":
                r.reads += 1
            else:
                r.writes += 1
            r.errors += 1
            r.last_err = why
            r.last_t = t

    def slaves(self):
        return sorted({k[0] for k in self.regs})

    def rows(self, slave=None):
        out = []
        for (s, tb, a), r in sorted(self.regs.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
            if slave is not None and s != slave:
                continue
            out.append({"slav": s, "tabell": tb, "adress": a, "värde": r.value,
                        "hex": "" if r.value is None else f"0x{r.value:04X}",
                        "läsningar": r.reads, "skrivningar": r.writes, "fel": r.errors,
                        "ändringar": max(0, len(r.history) - 1), "senaste_fel": r.last_err,
                        "historik": " | ".join("" if n is None else str(n) for _, _, n in list(r.history)[-10:])})
        return out

    def compare(self, a, b):
        """Rader för två slavar sida vid sida. status: lika/OLIKA/bara A/bara B/FEL A/FEL B."""
        keys = sorted({(k[1], k[2]) for k in self.regs if k[0] in (a, b)})
        out = []
        for tb, adr in keys:
            ra, rb = self.regs.get((a, tb, adr)), self.regs.get((b, tb, adr))
            ea = bool(ra and ra.errors and not ra.ok)
            eb = bool(rb and rb.errors and not rb.ok)
            if ra is None or (ra.value is None and not ea):
                st = "bara B"
            elif rb is None or (rb.value is None and not eb):
                st = "bara A"
            elif ea or eb:
                st = "FEL " + ("A+B" if ea and eb else "A" if ea else "B")
            elif ra.value != rb.value:
                st = "OLIKA"
            else:
                st = "lika"
            if st in ("bara A", "bara B") and (ea or eb):
                st = "FEL " + ("A" if ea else "B")
            out.append({"tabell": tb, "adress": adr,
                        "a_värde": None if ra is None else ra.value, "b_värde": None if rb is None else rb.value,
                        "a_fel": "" if ra is None else ra.last_err, "b_fel": "" if rb is None else rb.last_err,
                        "status": st})
        return out

    def export_csv(self, path):
        import csv
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["slav", "tabell", "adress", "värde_dec", "värde_hex", "läsningar", "skrivningar",
                        "fel", "ändringar", "senaste_fel", "historik"])
            for r in self.rows():
                w.writerow([r["slav"], TABLE_NAMES[r["tabell"]], r["adress"],
                            "" if r["värde"] is None else r["värde"], r["hex"], r["läsningar"],
                            r["skrivningar"], r["fel"], r["ändringar"], r["senaste_fel"], r["historik"]])

    def export_compare_csv(self, path, a, b):
        import csv
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["tabell", "adress", f"slav {a}", f"slav {b}", "status", f"fel {a}", f"fel {b}"])
            for r in self.compare(a, b):
                w.writerow([TABLE_NAMES[r["tabell"]], r["adress"], _v(r["a_värde"]), _v(r["b_värde"]),
                            r["status"], r["a_fel"], r["b_fel"]])


def _v(v):
    return "" if v is None else f"{v} (0x{v:04X})"


def compare_regs_table(rm, a, b, only_diff=False):
    lines = [f"{'tabell':<16}{'adr':>6}  {'slav ' + str(a):>16}  {'slav ' + str(b):>16}  status"]
    lines.append("-" * 70)
    for r in rm.compare(a, b):
        if only_diff and r["status"] == "lika":
            continue
        lines.append(f"{TABLE_NAMES[r['tabell']]:<16}{r['adress']:>6}  {_v(r['a_värde']):>16}  "
                     f"{_v(r['b_värde']):>16}  {r['status']}"
                     + ("" if r["status"] in ("lika", "OLIKA") else f"  {r['a_fel'] or r['b_fel']}"))
    return "\n".join(lines)


class Monitor:
    """
    Matas med (tid, bytes). Tiden är perf_counter-sekunder när bytes LÄSTES
    (dvs. ungefär när sista byten kom). Trådsäker via lås.
    """

    def __init__(self, baud=19200, parity="E", stop=1, resp_timeout=0.5, gap=None,
                 on_event=None, keep_events=20000, raw_keep_s=120.0):
        self.lock = threading.RLock()
        self.on_event = on_event
        self.resp_timeout = resp_timeout
        self.gap_override = gap
        self.set_line(baud, parity, stop)
        self.events = collections.deque(maxlen=keep_events)
        self.new_events = collections.deque(maxlen=keep_events)
        self.stats = {}
        self.raw = collections.deque()
        self.raw_keep_s = raw_keep_s
        self.seg = bytearray()
        self.seg_t0 = None
        self.last_byte_t = None
        self.seg_from_chunks = 0
        self.pending = None      # (addr, fc, t_end, raw)
        self.last_done = None    # (addr, fc, t_resp_end)
        self.last_frame_end = None
        self.last_lost = None    # senaste obesvarade fr\u00e5ga (SENT SVAR/KROCK)
        self._ctx_req = None
        self.crc_ok_frames = 0
        self.bad_frames = 0
        self.bytes_total = 0
        self.frames_total = 0
        self.t_first = None
        self.trap = None
        self.regmap = RegisterMap()
        self.line_errs = collections.deque(maxlen=5000)   # (t, typ) UART ram-/paritetsfel
        self.line_err_total = {"frame": 0, "parity": 0, "overrun": 0, "break": 0}

    def set_line(self, baud, parity, stop):
        self.baud, self.parity, self.stop = int(baud), parity, stop
        self.ct = char_time(self.baud, parity, stop)
        self.gap = self.gap_override if self.gap_override else t35(self.baud, parity, stop)
        # efter så här lång tystnad är en halv ram definitivt trasig (täcker USB-latens)
        self.hard_gap = max(self.gap * 4, 0.04)

    # ---- inmatning
    def feed(self, t, data):
        if not data:
            return
        with self.lock:
            if self.t_first is None:
                self.t_first = t
            self.bytes_total += len(data)
            self.raw.append((t, bytes(data)))
            while self.raw and t - self.raw[0][0] > self.raw_keep_s:
                self.raw.popleft()
            first_t = t - (len(data) - 1) * self.ct  # uppskattad tid för första byten
            if self.last_byte_t is not None:
                first_t = max(first_t, self.last_byte_t + self.ct)
                if first_t - self.last_byte_t > self.gap:
                    self._close_segment()
            if self.last_byte_t is not None and self.seg and first_t - self.last_byte_t > self.hard_gap:
                self._close_segment(final=True)
            if not self.seg:
                self.seg_t0 = first_t - self.ct
                self.seg_bounds = []
            else:
                self.seg_bounds.append(len(self.seg))
            self.seg += data
            self.last_byte_t = max(t, first_t + (len(data) - 1) * self.ct)
            self._tick_locked(t, from_feed=True)

    def line_error(self, t, kind, n=1):
        """UART-fel (ramfel/paritet/overrun) rapporterat av porten vid tid t."""
        with self.lock:
            self.line_err_total[kind] = self.line_err_total.get(kind, 0) + n
            for _ in range(min(n, 50)):
                self.line_errs.append((t, kind))

    def _line_errs_in(self, ts, te):
        if not self.line_errs:
            return 0
        lo, hi = ts - 2 * self.ct - 0.002, te + 0.02
        n = 0
        for t, _k in reversed(self.line_errs):
            if t < lo:
                break
            if t <= hi:
                n += 1
        return n

    def tick(self, now):
        with self.lock:
            self._tick_locked(now)

    def _tick_locked(self, now, from_feed=False):
        if not from_feed and self.seg and self.last_byte_t is not None:
            idle = now - self.last_byte_t
            if idle > self.hard_gap:
                self._close_segment(final=True)
            elif idle > self.gap:
                self._close_segment()
        p = self.pending
        if p and now - p[2] > self.resp_timeout and not self.seg:
            self.pending = None
            self._timeout(p, now, "inget svar inom %d ms" % (self.resp_timeout * 1000))
        if self.trap:
            self.trap.tick(self, now)

    def flush(self):
        with self.lock:
            if self.seg:
                self._close_segment(final=True)
            if self.pending:
                p = self.pending
                self.pending = None
                self._timeout(p, p[2] + self.resp_timeout, "inget svar (slut på data)")

    # ---- ramdelning
    def _could_be_prefix(self, b, i):
        """Kan b[i:] vara början på en ram som inte kommit klart än (USB-buffring)?"""
        n = len(b) - i
        if n >= 256 or b[i] > 247:
            return False
        if n < 2:
            return True
        fc = b[i + 1]
        if (fc & 0x7F) not in FC_NAMES:
            return False
        lens = [f(b, i) for f in (req_len, resp_len)]
        return any(L is not None and (L == -1 or L == 0 or L > n) for L in lens)

    def _close_segment(self, final=False):
        b = bytes(self.seg)
        t0 = self.seg_t0
        bounds = set(getattr(self, "seg_bounds", []))
        self.seg = bytearray()
        i = 0
        n = len(b)
        while i < n:
            prefer = bool(self.pending)
            L = match_at(b, i, prefer_resp=prefer)
            if L:
                gap_known = i in bounds or i == 0
                self._frame(b[i:i + L], t0 + i * self.ct, t0 + (i + L) * self.ct, measured_gap=gap_known)
                i += L
                continue
            if not final and self._could_be_prefix(b, i):
                # vänta på resten - adaptern har bara levererat en del
                self.seg = bytearray(b[i:])
                self.seg_t0 = t0 + i * self.ct
                self.seg_bounds = [k - i for k in bounds if k > i]
                return
            j = i + 1
            while j < n and not match_at(b, j, prefer_resp=prefer):
                j += 1
            self._garbage(b[i:j], t0 + i * self.ct, t0 + j * self.ct, whole=(i == 0 and j == n))
            i = j

    # ---- tolkning
    def _st(self, a):
        s = self.stats.get(a)
        if s is None:
            s = self.stats[a] = SlaveStats(a)
        return s

    def _emit(self, ev):
        if ev.kind in ("req", "bcast"):
            ev.req = ev.raw
        elif ev.req is None:
            ev.req = self._ctx_req
        self.events.append(ev)
        self.new_events.append(ev)
        if ev.kind in ERROR_KINDS and ev.slave is not None:
            self._st(ev.slave).add_err(ev)
        if self.trap and ev.kind in ERROR_KINDS:
            self.trap.on_error(self, ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def _timeout(self, p, t, why):
        a, fc, t_end, raw = p
        self._st(a).timeouts += 1
        self.last_lost = p
        self._ctx_req = raw
        self.regmap.on_fail(raw, t, "timeout")
        self._emit(Event(t, "timeout", "err", a, fc,
                         f"{describe(raw, False)} → INGET SVAR (timeout, {why})", raw))

    def _frame(self, f, ts, te, measured_gap):
        self.frames_total += 1
        self.crc_ok_frames += 1
        a, fc = f[0], f[1]
        gap = None if self.last_frame_end is None else ts - self.last_frame_end
        p = self.pending
        if p and ts - p[2] > self.resp_timeout:
            self.pending = None
            self._timeout(p, p[2] + self.resp_timeout, "inget svar inom %d ms" % (self.resp_timeout * 1000))
            p = None
        resp_shape = is_resp_shape(f)
        req_shape = is_req_shape(f)
        self._ctx_req = p[3] if p else None
        lt = self.last_lost
        if (lt and a == lt[0] and (fc == lt[1] or fc == (lt[1] | 0x80)) and (resp_shape or fc & 0x80)
                and not (p and a == p[0]) and ts - lt[2] < 10 * self.resp_timeout + 1.0
                and (not req_shape or bytes(f) == bytes(lt[3]))):
            self.last_lost = None
            rtt = max(0.0, ts - lt[2])
            st = self._st(a)
            st.late += 1
            st.max_any = rtt if st.max_any is None else max(st.max_any, rtt)
            self._ctx_req = lt[3]
            extra = f" - PLC hade redan fr\u00e5gat slav {p[0]}" if p else ""
            self._emit(Event(te, "late", "err", a, fc & 0x7F,
                             f"{describe(lt[3], False)}  SENT SVAR efter {rtt * 1000:.1f} ms "
                             f"(gr\u00e4ns {self.resp_timeout * 1000:.0f} ms){extra}", f, rtt, asked=lt[0]))
            self.last_frame_end = te
            return
        if p and a == p[0] and (fc == p[1] or fc == (p[1] | 0x80)) and (resp_shape or fc & 0x80):
            self.pending = None
            st = self._st(a)
            rtt = max(0.0, ts - p[2])
            st.resp += 1
            st.rtts.append(rtt)
            st.max_any = rtt if st.max_any is None else max(st.max_any, rtt)
            st.last_ok_t = te
            st.line_err += self._line_errs_in(ts, te)
            self.last_done = (a, p[1], te)
            if fc & 0x80:
                st.exc += 1
                if len(f) >= 3:
                    st.exc_codes[f[2]] = st.exc_codes.get(f[2], 0) + 1
                self.regmap.on_fail(p[3], te, describe(f, True))
                self._emit(Event(te, "exc", "warn", a, fc & 0x7F,
                                 f"{describe(p[3], False)} → {describe(f, True)} ({rtt * 1000:.1f} ms)", f, rtt))
            else:
                self.regmap.on_resp(p[3], f, te)
                self._emit(Event(te, "resp", "ok", a, fc,
                                 f"{describe(p[3], False)} → Svar ({rtt * 1000:.1f} ms): {describe(f, True)}", f, rtt))
        elif p and a != p[0] and (fc == p[1] or fc == (p[1] | 0x80)) and resp_shape and not req_shape:
            self.pending = None
            self._st(a).other += 1
            self._st(p[0]).other += 1
            self._st(p[0]).wrong += 1
            self._emit(Event(te, "wrongaddr", "err", a, fc, asked=p[0], text=
                             f"svar från slav {a} men frågan gick till slav {p[0]} (fel adress/adresskrock?)", raw=f))
        elif (not p and self.last_done and resp_shape and not req_shape
              and fc in (self.last_done[1], self.last_done[1] | 0x80)
              and ts - self.last_done[2] < self.resp_timeout):
            ld = self.last_done
            self._st(ld[0]).other += 1
            who = "samma adress" if a == ld[0] else f"slav {a}"
            self._ctx_req = None
            self._emit(Event(te, "dup", "err", a, fc, asked=ld[0], text=
                             f"extra svar efter att slav {ld[0]} redan svarat ({who}) - två enheter med samma adress?", raw=f))
        else:
            # en fråga
            if p:
                self.pending = None
                self._st(p[0]).timeouts += 1
                self.last_lost = p
                self.regmap.on_fail(p[3], ts, "inget svar (master frågade vidare)")
                self._emit(Event(ts, "early", "err", p[0], p[1],
                                 f"master sände ny fråga (till {a}) efter {(ts - p[2]) * 1000:.1f} ms utan svar från {p[0]}",
                                 p[3]))
            if measured_gap and gap is not None and gap < self.gap * 0.9 and self.frames_total > 1:
                self._emit(Event(ts, "gap", "warn", a, fc,
                                 f"bara {gap * 1000:.2f} ms tystnad före frågan (krav {self.gap * 1000:.2f} ms)", b""))
            if a == 0:
                self._emit(Event(te, "bcast", "ok", 0, fc, describe(f, False), f))
            else:
                self._st(a).req += 1
                self.pending = (a, fc, te, bytes(f))
                self._emit(Event(te, "req", "ok", a, fc, describe(f, False), f))
        self.last_frame_end = te

    def _garbage(self, g, ts, te, whole):
        p = self.pending
        self.bad_frames += 1
        _who = p[0] if p else (g[0] if whole and len(g) >= 4 and 1 <= g[0] <= 247 else None)
        if _who is not None:
            ne = self._line_errs_in(ts, te)
            st0 = self._st(_who)
            st0.line_err += ne
            st0.bad_resp += 1
        self._ctx_req = p[3] if p else None
        lt = self.last_lost
        recent = lt is not None and ts - lt[2] < 3 * self.resp_timeout + 0.1
        quick = p is not None and ts - p[2] < self.gap
        if (p and (recent or quick)) or (not p and recent):
            self.pending = None
            if not p:
                p = lt
            st = self._st(p[0])
            st.coll += 1
            self.regmap.on_fail(p[3], te, "krock")
            why = (f"sent svar fr\u00e5n slav {lt[0]} krockade troligen med fr\u00e5gan" if recent
                   else "svaret b\u00f6rjade innan fr\u00e5gan var klar")
            self.last_lost = None
            self._emit(Event(te, "collision", "err", p[0], p[1],
                             f"{describe(p[3], False)}  KROCK: {len(g)} trasiga byte - {why} (tv\u00e5 s\u00e4ndare samtidigt)",
                             g, asked=p[0]))
            self.last_frame_end = te
            return
        if whole and len(g) >= 4 and g[0] <= 247 and (g[1] & 0x7F) in FC_NAMES:
            a = g[0]
            if p and a == p[0]:
                self.pending = None
                self._st(a).crc += 1
                self.regmap.on_fail(p[3], te, "CRC-fel i svar")
                self._emit(Event(te, "crc", "err", a, g[1] & 0x7F, f"{describe(p[3], False)} → svar med CRC-fel (störning/kollision?)", g))
            else:
                self._st(a).crc += 1
                self._emit(Event(te, "crc", "err", a, g[1] & 0x7F,
                                 "ram med CRC-fel" + (f" medan slav {p[0]} skulle svara" if p else ""), g))
        else:
            slave = p[0] if p else None
            if slave is not None:
                self._st(slave).other += 1
            self._emit(Event(te, "junk", "err", slave, None,
                             f"{len(g)} byte som inte bildar giltig ram (brus/kollision/fel baud?)", g))
        self.last_frame_end = te

    # ---- utläsning
    def drain(self):
        with self.lock:
            out = list(self.new_events)
            self.new_events.clear()
        return out

    def stats_rows(self):
        with self.lock:
            return [self.stats[a].summary() for a in sorted(self.stats)]

    def reset_stats(self):
        with self.lock:
            self.stats.clear()
            self.regmap = RegisterMap()


# ------------------------------------------------------------------ felfälla

class ErrorTrap:
    """Sparar senaste N s före och M s efter ett fel för vald(a) slav(ar) till egen fil."""

    def __init__(self, slaves=(), pre=10.0, post=3.0, folder=".", header="", kinds=ERROR_KINDS,
                 on_saved=None, max_files=200):
        self.slaves = set(slaves)
        self.pre, self.post, self.folder, self.header = pre, post, folder, header
        self.kinds = kinds
        self.armed = []   # [(t_err, ev)]
        self.on_saved = on_saved
        self.saved = []
        self.max_files = max_files

    def on_error(self, mon, ev):
        if ev.kind not in self.kinds:
            return
        if self.slaves and ev.slave not in self.slaves:
            return
        if self.armed and ev.t - self.armed[-1][0] < self.post:
            return  # samma händelseförlopp
        self.armed.append((ev.t, ev))

    def tick(self, mon, now):
        while self.armed and now - self.armed[0][0] >= self.post:
            t_err, ev = self.armed.pop(0)
            if len(self.saved) >= self.max_files:
                continue
            chunks = [(t, d) for t, d in mon.raw if t_err - self.pre <= t <= t_err + self.post]
            evs = [e for e in mon.events if t_err - self.pre <= e.t <= t_err + self.post]
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            name = os.path.join(self.folder, f"felfalla_slav{ev.slave}_{stamp}_{len(self.saved) + 1}.zmlog")
            try:
                os.makedirs(self.folder, exist_ok=True)
                with open(name, "w", encoding="utf-8") as fh:
                    fh.write(self.header + "\n")
                    fh.write(f"# felfälla: {KIND_TEXT.get(ev.kind)} slav {ev.slave}: {ev.text}\n")
                    for e in evs:
                        fh.write("# " + e.line(t_err) + "\n")
                    for t, d in chunks:
                        fh.write(f"{t - t_err:.6f} {d.hex()}\n")
                self.saved.append(name)
                if self.on_saved:
                    self.on_saved(name)
            except OSError:
                pass


# ------------------------------------------------------------------ logg och uppspelning

class RawLog:
    def __init__(self, path, header):
        self.fh = open(path, "w", encoding="utf-8", buffering=1)
        self.fh.write(header + "\n")
        self.t0 = None
        self.path = path

    def write(self, t, data):
        if self.t0 is None:
            self.t0 = t
        self.fh.write(f"{t - self.t0:.6f} {data.hex()}\n")

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


def make_header(port, baud, parity, stop):
    return (f"# zt-tray modbus-monitor v1 port={port} baud={baud} parity={parity} stop={stop} "
            f"start={_dt.datetime.now().isoformat(timespec='seconds')}")


def read_log(path):
    """-> (settings, [(t, bytes)])"""
    settings = {}
    chunks = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if "modbus-monitor" in line:
                    for part in line.split():
                        if "=" in part:
                            k, v = part.split("=", 1)
                            settings[k] = v
                continue
            try:
                t, h = line.split(None, 1)
                chunks.append((float(t), bytes.fromhex(h)))
            except ValueError:
                continue
    return settings, chunks


def replay(chunks, mon):
    for t, d in chunks:
        mon.tick(t - 1e-6)
        mon.feed(t, d)
    if chunks:
        mon.tick(chunks[-1][0] + mon.resp_timeout + 1)
    mon.flush()
    return mon


def export_csv(events, path, t0=0.0):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["tid_s", "typ", "niva", "slav", "fc", "svarstid_ms", "text", "hex"])
        for e in events:
            w.writerow([f"{e.t - t0:.6f}", KIND_TEXT.get(e.kind, e.kind), e.level,
                        "" if e.slave is None else e.slave, "" if e.fc is None else f"{e.fc:02X}",
                        "" if e.rtt is None else f"{e.rtt * 1000:.2f}", e.text, e.raw.hex(" ")])


# ------------------------------------------------------------------ portar (bara läsning)

class WinPort:
    """Lokal COM-port via Win32 (ingen pyserial). Skriver bara via write() (Sändare-fliken, aktiverad)."""

    def __init__(self, port, baud, parity, stop):
        import ctypes
        from ctypes import wintypes
        import serialshare
        self.ws = serialshare.WinSerial()
        self.ws.open(port, baud, 8, parity, stop)
        k = self.ws._k
        k.SetupComm.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
        k.SetupComm(self.ws.h, 65536, 4096)

        class TIMEOUTS(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.DWORD), ("c", wintypes.DWORD),
                        ("d", wintypes.DWORD), ("e", wintypes.DWORD)]
        # MAXDWORD/MAXDWORD/x: returnera så fort någon byte finns, annars efter 5 ms
        t = TIMEOUTS(0xFFFFFFFF, 0xFFFFFFFF, 5, 0, 1000)
        k.SetCommTimeouts(self.ws.h, ctypes.byref(t))
        k.ClearCommError.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self._ct = ctypes
        self._wt = wintypes
        self.errs = {"frame": 0, "parity": 0, "overrun": 0, "break": 0}

    def read(self):
        d = self.ws.read(4096)
        if not d:
            self._poll_errors()
        return d

    def _poll_errors(self):
        e = self._wt.DWORD()
        if self.ws._k.ClearCommError(self.ws.h, self._ct.byref(e), None):
            v = e.value
            if v & 0x8:
                self.errs["frame"] += 1
            if v & 0x4:
                self.errs["parity"] += 1
            if v & 0x3:
                self.errs["overrun"] += 1
            if v & 0x10:
                self.errs["break"] += 1

    def errors(self):
        self._poll_errors()
        return dict(self.errs)

    def write(self, b):
        self.ws.write(b)

    def close(self):
        self.ws.close()


class PySerialPort:
    def __init__(self, port, baud, parity, stop):
        import serial
        self.s = serial.serial_for_url(port, baudrate=baud, parity=parity, stopbits=stop,
                                       bytesize=8, timeout=0.005)

    def read(self):
        return self.s.read(4096)

    def errors(self):
        return {}

    def write(self, b):
        self.s.write(b)

    def close(self):
        self.s.close()


class Rfc2217Port:
    """Lyssna på en COM-port som en annan ZT-Tray delar (Uttag -> COM, TCP 47922).
    Skickar bara RFC 2217-inställningar (baud/paritet), aldrig data till bussen.
    OBS: delningen tar en klient åt gången."""

    def __init__(self, host, port, baud, parity, stop):
        import serialshare
        self.p = serialshare.Rfc2217()
        self.s = socket.create_connection((host, int(port)), timeout=5)
        self.s.settimeout(0.01)
        self.s.sendall(self.p.greeting() + self.p.command(baud, 8, parity, stop))

    def read(self):
        try:
            d = self.s.recv(4096)
        except socket.timeout:
            return b""
        if not d:
            raise OSError("anslutningen stängdes")
        out = self.p.feed(d)
        self.p.replies.clear()
        return out

    def errors(self):
        return {}

    def close(self):
        self.s.close()


def open_port(name, baud, parity, stop):
    if name.lower().startswith("rfc2217://"):
        hp = name[10:].rstrip("/")
        host, _, prt = hp.partition(":")
        return Rfc2217Port(host, prt or 47922, baud, parity, stop)
    if sys.platform == "win32" and not "://" in name:
        return WinPort(name, baud, parity, stop)
    return PySerialPort(name, baud, parity, stop)


def list_ports():
    try:
        import serialshare
        return serialshare.list_com_ports()
    except Exception:
        return []


RS485_HINTS = ("rs485", "rs-485", "485", "waveshare", "uport 1150", "uport1150", "moxa")


def port_infos():
    """Portlista med beskrivning/tillverkare/VID:PID/serienr (pyserial), annars bara namn."""
    out = []
    try:
        from serial.tools import list_ports as _lp
        for p in sorted(_lp.comports(), key=lambda x: x.device):
            txt = " ".join(str(v or "") for v in (p.description, p.product, p.manufacturer)).lower()
            out.append({"device": p.device, "description": p.description or "", "manufacturer": p.manufacturer or "",
                        "product": p.product or "", "vid": p.vid, "pid": p.pid, "serial": p.serial_number or "",
                        "usb": p.vid is not None, "rs485": any(h in txt for h in RS485_HINTS)})
    except Exception:
        pass
    if not out:
        out = [{"device": d, "description": "", "manufacturer": "", "product": "", "vid": None, "pid": None,
                "serial": "", "usb": False, "rs485": False} for d in list_ports()]
    return out


def port_label(i):
    s = i["device"]
    if i.get("description") and i["description"] != i["device"]:
        s += f" \u2013 {i['description']}"
    if i.get("manufacturer"):
        s += f" [{i['manufacturer']}]"
    if i.get("vid") is not None:
        s += f" {i['vid']:04X}:{(i.get('pid') or 0):04X}"
    if i.get("serial"):
        s += f" SN {i['serial']}"
    if i.get("rs485"):
        s += "  \u2605RS485"
    return s


def port_name(label):
    """'COM5 - USB Serial ...' -> 'COM5'."""
    label = (label or "").strip()
    return label.split()[0] if label else ""


def pick_port(infos, last=""):
    """Senast vald om den finns, annars RS485-adapter, annars första USB-serial, annars första."""
    if not infos:
        return None
    for i in infos:
        if last and i["device"].upper() == last.upper():
            return i
    for i in infos:
        if i["rs485"]:
            return i
    for i in infos:
        if i["usb"]:
            return i
    return infos[0]


class MultiCapture:
    """Lyssna på flera portar samtidigt (4-trådsbuss: ett par för master->slav, ett för slav->master)
    och slå ihop trafiken i samma Monitor. Sänder aldrig."""

    def __init__(self, mon, port_names, baud, parity, stop, log_path=None, on_error=None):
        self.caps = []
        self.mon = mon
        self.port_name = " + ".join(port_names)
        for i, pn in enumerate(port_names):
            lp = None
            if log_path:
                root, ext = os.path.splitext(log_path)
                lp = log_path if i == 0 else f"{root}_{pn}{ext}"
            self.caps.append(Capture(mon, pn, baud, parity, stop, log_path=lp, on_error=on_error))
        self.port = self.caps[0].port if self.caps else None
        self.error = ""

    def start(self):
        started = []
        try:
            for c in self.caps:
                c.start()
                started.append(c)
        except Exception:
            for c in started:
                c.stop()
            raise
        self.port = self.caps[0].port

    @property
    def th(self):
        class _T:
            def __init__(s, caps):
                s.caps = caps

            def is_alive(s):
                return all(c.th and c.th.is_alive() for c in s.caps)
        return _T(self.caps)

    def errors(self):
        out = {}
        for c in self.caps:
            try:
                for k, v in (c.port.errors() if c.port else {}).items():
                    out[k] = out.get(k, 0) + v
            except Exception:
                pass
        return out

    def stop(self):
        for c in self.caps:
            try:
                c.stop()
            except Exception:
                pass
        self.error = "; ".join(c.error for c in self.caps if c.error)


class Capture:
    """Lästråd: port -> Monitor (+ ev. rålogg)."""

    def __init__(self, mon, port_name, baud, parity, stop, log_path=None, on_error=None):
        self.mon = mon
        self.port_name = port_name
        self.cfg = (baud, parity, stop)
        self.log = RawLog(log_path, make_header(port_name, baud, parity, stop)) if log_path else None
        self.on_error = on_error
        self.stop_ev = threading.Event()
        self.port = None
        self.th = None
        self.error = ""
        self.taps = []          # callbacks(data) - t.ex. master som delar porten

    def write(self, b):
        """Sänd via lyssnarens port (bara från Sändare/AI när sändning är aktiverad)."""
        if not hasattr(self.port, "write"):
            raise OSError("porten kan inte sända (fjärrport/uppspelning)")
        self.port.write(b)

    def start(self):
        self.port = open_port(self.port_name, *self.cfg)
        self.th = threading.Thread(target=self._run, daemon=True, name="modbus-mon")
        self.th.start()

    def _run(self):
        pc = time.perf_counter
        last = {}
        try:
            while not self.stop_ev.is_set():
                d = self.port.read()
                t = pc()
                try:
                    cur = self.port.errors() if d else None
                except Exception:
                    cur = None
                if cur:
                    for k, v in cur.items():
                        dv = v - last.get(k, 0)
                        if dv > 0 and last:
                            self.mon.line_error(t, k, dv)
                    last = dict(cur)
                if d:
                    if self.log:
                        self.log.write(t, d)
                    self.mon.feed(t, d)
                    for tap in list(self.taps):
                        try:
                            tap(d)
                        except Exception:
                            pass
                else:
                    self.mon.tick(t)
        except Exception as e:  # kabel ut, port borta
            self.error = str(e)
            if self.on_error:
                self.on_error(self.error)
        finally:
            try:
                self.port.close()
            except Exception:
                pass
            if self.log:
                self.log.close()

    def stop(self):
        self.stop_ev.set()
        if self.th:
            self.th.join(2)
        self.mon.flush()


# ------------------------------------------------------------------ auto-detektering

def score_bytes(chunks, baud, parity, stop):
    """Hur många giltiga ramar en inspelning ger tolkad med given inställning."""
    m = Monitor(baud, parity, stop, resp_timeout=1.0)
    replay(chunks, m)
    ok = sum(1 for e in m.events if e.kind in ("req", "resp", "exc", "bcast"))
    bad = sum(1 for e in m.events if e.kind in ("junk", "crc"))
    return ok, bad


def autodetect(port_name, seconds=1.5, bauds=BAUDS, parities=PARITIES, stops=STOPS,
               progress=None, stop_flag=None):
    """
    Provar varje kombination av baud/paritet/stoppbitar, läser några sekunder och
    räknar giltiga CRC-ramar. UART-fel (paritet/ramfel) räknas också: 8N1 tar emot
    nästan allt utan fel, så en striktare kombination (E1/O1/N2) utan UART-fel vinner
    vid lika antal ramar. Returnerar lista sorterad bäst först:
    [{"baud","parity","stop","frames","bad","uart_err","score"}]
    """
    res = []
    combos = [(b, p, s) for b in bauds for p in parities for s in stops]
    for i, (b, p, s) in enumerate(combos):
        if stop_flag and stop_flag():
            break
        if progress:
            progress(i, len(combos), b, p, s)
        try:
            port = open_port(port_name, b, p, s)
        except Exception as e:
            res.append({"baud": b, "parity": p, "stop": s, "frames": 0, "bad": 0, "uart_err": 0,
                        "score": -1, "note": str(e)})
            continue
        chunks = []
        t_end = time.perf_counter() + seconds
        try:
            port.errors()
            while time.perf_counter() < t_end:
                d = port.read()
                if d:
                    chunks.append((time.perf_counter(), d))
            errs = port.errors()
        finally:
            port.close()
        ok, bad = score_bytes(chunks, b, p, s)
        uerr = sum(errs.values()) if errs else 0
        res.append({"baud": b, "parity": p, "stop": s, "frames": ok, "bad": bad, "uart_err": uerr,
                    "score": 0, "note": ""})
    return rank(res)


def rank(res):
    for r in res:
        if r["score"] == -1:
            continue
        strict = (1 if r["parity"] != "N" else 0) + (1 if r["stop"] == 2 else 0)
        penalty = r["bad"] * 2 + min(r["uart_err"], 50) * 2
        r["score"] = round(r["frames"] * 10 - penalty * 10 + (strict if r["uart_err"] == 0 else 0), 1)
    res.sort(key=lambda r: r["score"], reverse=True)
    return res


def autodetect_note(best, res):
    """Text om stoppbitar: en mottagare kan oftast inte skilja 1 från 2 stoppbitar."""
    same = [r for r in res if r["baud"] == best["baud"] and r["parity"] == best["parity"]
            and r["stop"] != best["stop"] and r["frames"] == best["frames"]]
    if same:
        spec = 2 if best["parity"] == "N" else 1
        return (f"1 och 2 stoppbitar gav samma resultat - mottagaren kan inte alltid skilja dem. "
                f"Modbus-specen: {best['parity']} -> {spec} stoppbit(ar). Välj det PLC:n är inställd på.")
    return ""


# ------------------------------------------------------------------ simulator (test utan hårdvara)

def _resp_for(req, regs_seed=0):
    a, fc = req[0], req[1]
    if fc in (3, 4):
        n = (req[4] << 8) | req[5]
        data = b"".join(((regs_seed + k) & 0xFFFF).to_bytes(2, "big") for k in range(n))
        return with_crc(bytes([a, fc, len(data)]) + data)
    if fc in (1, 2):
        n = (req[4] << 8) | req[5]
        nb = (n + 7) // 8
        return with_crc(bytes([a, fc, nb]) + bytes([0x55] * nb))
    if fc in (5, 6):
        return bytes(req)
    if fc in (0x0F, 0x10):
        return with_crc(req[:6])
    return with_crc(bytes([a, fc | 0x80, 1]))


def simulate(duration=10.0, baud=19200, parity="E", stop=1, slaves=(1, 2, 3, 4, 5, 6),
             faults=None, usb_latency=0.001, seed=1, poll_gap=0.005, resp_delay=0.003, return_bytes=False):
    """
    Bygger en syntetisk inspelning [(t, bytes)] som en USB-adapter skulle leverera.
    faults: dict {"timeout": {slav: sannolikhet}, "crc": {...}, "exception": {...},
                  "dup": {...}, "noise": sannolikhet per cykel, "early": {...},
                  "slow": {slav: extra_s}}
    """
    import random
    rnd = random.Random(seed)
    faults = faults or {}
    ct = char_time(baud, parity, stop)
    byte_times = []   # (t, byte)
    t = 0.01

    def put(frame, t0, tag=None):
        for k, b in enumerate(frame):
            byte_times.append((t0 + (k + 1) * ct, b, tag))
        return t0 + len(frame) * ct

    def p(kind, s):
        v = faults.get(kind, {})
        return rnd.random() < (v.get(s, 0) if isinstance(v, dict) else v)

    cycle = 0
    stopped = {}
    while t < duration:
        for s in slaves:
            if stopped.get(s):
                continue
            stop_at = faults.get("stop", {}).get(s)
            fc = 3 if (cycle + s) % 3 else (6 if s % 2 else 0x10)
            extra_addr = None
            if fc == 3 and cycle % 5 == 0:
                extra_addr = 300   # gemensamt parameterregister som alla läser ibland
                req = with_crc(bytes([s, 3, 1, 44, 0, 1]))
            elif fc == 3:
                req = with_crc(bytes([s, 3, 0, 100, 0, 4]))
            elif fc == 6:
                req = with_crc(bytes([s, 6, 0, 1, 0, cycle & 0xFF]))
            else:
                req = with_crc(bytes([s, 0x10, 0, 20, 0, 2, 4, 0, 1, 0, cycle & 0xFF]))
            t = put(req, t)
            if p("early", s):
                t += 0.002   # master väntar för kort och sänder nästa
                continue
            if stop_at is not None and t > stop_at:
                stopped[s] = True      # slutar svara, PLC ger upp adressen
                t += 0.6
                continue
            if p("timeout", s):
                t += 0.1
                continue
            resp = _resp_for(req, cycle)
            if extra_addr is not None:
                if extra_addr in faults.get("illegal", {}).get(s, ()):
                    resp = with_crc(bytes([s, 0x83, 2]))
                else:
                    resp = with_crc(bytes([s, 3, 2, 0, 7]))
            elif fc == 3 and s in faults.get("regdiff", {}):
                b = bytearray(resp[:-2])
                k = faults["regdiff"][s]          # registerindex i blocket 100..103
                b[3 + 2 * k] ^= 0x01
                resp = with_crc(bytes(b))
            if p("exception", s):
                resp = with_crc(bytes([s, fc | 0x80, 2]))
            delay = resp_delay + faults.get("slow", {}).get(s, 0) + rnd.random() * 0.001
            if p("crc", s):
                resp = bytearray(resp)
                resp[len(resp) // 2] ^= 0x10
                resp = bytes(resp)
            if p("late", s):
                t = put(resp, t + 0.6 + delay, s) + poll_gap
                continue
            if p("collide", s):
                put(resp, t + 0.6 + delay, s)
                t += 0.6 + delay + 0.001
                continue
            t = put(resp, t + delay, s)
            if p("dup", s):
                t = put(_resp_for(req, cycle + 7), t + 0.002, s)
            t += poll_gap
        if rnd.random() < faults.get("noise", 0):
            t = put(bytes(rnd.randrange(256) for _ in range(3)), t, "noise")
            t += poll_gap
        cycle += 1

    # USB-adaptern levererar i block var usb_latency
    byte_times.sort(key=lambda x: x[0])
    if return_bytes:
        return byte_times
    return chunk_bytes(byte_times, usb_latency)


def chunk_bytes(byte_times, usb_latency=0.001):
    """[(t, byte, ...)] -> [(t, bytes)] så som en USB-adapter levererar block."""
    chunks = []
    cur, cur_t = bytearray(), None
    for item in byte_times:
        bt, b = item[0], item[1]
        slot = (int(bt / usb_latency) + 1) * usb_latency
        if cur_t is not None and slot != cur_t:
            chunks.append((cur_t, bytes(cur)))
            cur = bytearray()
        cur_t = slot
        cur.append(b)
    if cur:
        chunks.append((cur_t, bytes(cur)))
    return chunks


def write_log(path, chunks, header):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n")
        for t, d in chunks:
            fh.write(f"{t:.6f} {d.hex()}\n")


# ------------------------------------------------------------------ CLI

def _fmt(v):
    return "-" if v is None else f"{v:.1f}"


def stats_table(rows):
    head = f"{'slav':>4} {'frågor':>7} {'svar':>7} {'timeout':>7} {'crc':>5} {'exc':>5} {'övr':>5} {'fel%':>6} {'min':>7} {'medel':>7} {'max':>7} {'p99':>7} {'sena':>5} {'krock':>5}  orsak"
    out = [head, "-" * len(head)]
    for r in rows:
        out.append(f"{r['slav']:>4} {r['frågor']:>7} {r['svar']:>7} {r['timeout']:>7} {r['crc']:>5} {r['exception']:>5} "
                   f"{r['övrigt']:>5} {r['felprocent']:>6.2f} {_fmt(r['min']):>7} {_fmt(r['medel']):>7} {_fmt(r['max']):>7} "
                   f"{_fmt(r['p99']):>7} {r['sena']:>5} {r['krockar']:>5}  {cause_text(r)[:80]}")
    return "\n".join(out)


def compare_table(rows, slaves):
    """Sida vid sida för t.ex. gammal och ny SW1A4080."""
    sel = [r for r in rows if r["slav"] in slaves]
    if not sel:
        return "(inga data för valda slavar)"
    keys = [("frågor", "Frågor"), ("svar", "Svar"), ("timeout", "Timeouts"), ("crc", "CRC-fel"),
            ("exception", "Exceptions"), ("övrigt", "Övriga fel"), ("felprocent", "Fel %"),
            ("min", "Svarstid min ms"), ("medel", "Svarstid medel ms"), ("max", "Svarstid max ms"),
            ("p99", "Svarstid p99 ms")]
    out = [f"{'':<20}" + "".join(f"{'slav ' + str(r['slav']):>12}" for r in sel)]
    for k, name in keys:
        vals = []
        for r in sel:
            v = r[k]
            vals.append("-" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v)))
        out.append(f"{name:<20}" + "".join(f"{v:>12}" for v in vals))
    return "\n".join(out)


COLORS = {"ok": "", "warn": "\033[33m", "err": "\033[31m"}


def _print_ev(ev, args, t0):
    if args.only_errors and ev.level == "ok":
        return
    if args.slave and ev.slave not in args.slave:
        return
    line = ev.line(t0)
    if args.color:
        c = COLORS.get(ev.level, "")
        line = f"{c}{line}\033[0m" if c else line
    print(line, flush=False)


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Passiv Modbus RTU-busmonitor (sänder aldrig).")
    ap.add_argument("--port", help="COM5, /dev/ttyUSB0 eller rfc2217://ip:47922")
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--parity", default="E", choices=["N", "E", "O"])
    ap.add_argument("--stop", type=float, default=1, choices=[1, 2])
    ap.add_argument("--auto", action="store_true", help="prova baud/paritet/stoppbitar, välj bäst")
    ap.add_argument("--auto-seconds", type=float, default=1.5)
    ap.add_argument("--timeout", type=float, default=500, help="svarstimeout i ms (sätt som PLC:ns)")
    ap.add_argument("--gap", type=float, default=0, help="ramtystnad i ms (0 = 3,5 tecken)")
    ap.add_argument("--log", help="spara rå trafik (.zmlog) för uppspelning")
    ap.add_argument("--csv", help="exportera händelser till CSV vid slut")
    ap.add_argument("--replay", help="spela upp en .zmlog")
    ap.add_argument("--simulate", action="store_true", help="kör mot syntetisk buss med fel")
    ap.add_argument("--slave", type=int, action="append", help="visa bara denna slav (kan upprepas)")
    ap.add_argument("--only-errors", action="store_true")
    ap.add_argument("--trap", type=int, action="append", help="felfälla för slav (kan upprepas)")
    ap.add_argument("--trap-pre", type=float, default=10)
    ap.add_argument("--trap-post", type=float, default=3)
    ap.add_argument("--trap-dir", default="felfalla")
    ap.add_argument("--compare", type=int, nargs="+", help="jämför slavar sida vid sida, t.ex. --compare 4 5")
    ap.add_argument("--regs", action="store_true", help="skriv registerdump vid slut")
    ap.add_argument("--regs-csv", help="exportera registerdump till CSV")
    ap.add_argument("--compare-regs", type=int, nargs=2, metavar=("A", "B"),
                    help="jämför register mellan två slavar, t.ex. 4 5")
    ap.add_argument("--stats-every", type=float, default=10)
    ap.add_argument("--list", action="store_true", help="lista COM-portar")
    ap.add_argument("--no-color", dest="color", action="store_false")
    args = ap.parse_args(argv)
    stop = int(args.stop)
    if sys.platform == "win32" and args.color:
        os.system("")  # slå på ANSI i konsolen

    if args.list:
        print("\n".join(port_label(i) for i in port_infos()) or "inga COM-portar")
        return 0

    def finish(mon, t0):
        rows = mon.stats_rows()
        print("\n" + stats_table(rows))
        for stp in find_stops(mon.events, quiet=2.0, n=15):
            print(f"\n=== VAD H\u00c4NDE INNAN STOPPET: {stp['typ']} vid {stp['t'] - t0:.3f} s - "
                  f"f\u00f6rst felande adress: {stp['first_fail']} ===")
            for e in stp["before"]:
                print("  " + e.line(t0))
        if args.compare:
            print("\n" + compare_table(rows, args.compare))
        if args.regs:
            for r in mon.regmap.rows():
                print(f"slav {r['slav']:>3} {TABLE_NAMES[r['tabell']]:<16}{r['adress']:>6} = "
                      f"{'' if r['värde'] is None else r['värde']:>6} {r['hex']:>7}  L{r['läsningar']} S{r['skrivningar']}"
                      f" fel {r['fel']} {r['senaste_fel']}  hist: {r['historik']}")
        if args.compare_regs:
            print("\n" + compare_regs_table(mon.regmap, *args.compare_regs))
        if args.regs_csv:
            if args.compare_regs:
                mon.regmap.export_compare_csv(args.regs_csv, *args.compare_regs)
            else:
                mon.regmap.export_csv(args.regs_csv)
            print(f"Register-CSV: {args.regs_csv}")
        if args.csv:
            export_csv(list(mon.events), args.csv, t0)
            print(f"CSV: {args.csv}")
        if mon.trap and mon.trap.saved:
            print("Felfälla sparade:\n  " + "\n  ".join(mon.trap.saved))

    gap = args.gap / 1000 if args.gap else None
    if args.replay or args.simulate:
        if args.replay:
            st, chunks = read_log(args.replay)
            baud = int(st.get("baud", args.baud))
            par = st.get("parity", args.parity)
            stop = int(float(st.get("stop", stop)))
        else:
            baud, par = args.baud, args.parity
            chunks = simulate(30, baud, par, stop, faults=DEMO_FAULTS)
            if args.log:
                write_log(args.log, chunks, make_header("SIM", baud, par, stop))
        mon = Monitor(baud, par, stop, resp_timeout=args.timeout / 1000, gap=gap,
                      on_event=lambda e: _print_ev(e, args, chunks[0][0] if chunks else 0))
        if args.trap:
            mon.trap = ErrorTrap(args.trap, args.trap_pre, args.trap_post, args.trap_dir,
                                 make_header("REPLAY", baud, par, stop))
        replay(chunks, mon)
        if mon.trap:
            mon.trap.tick(mon, 1e12)
        finish(mon, chunks[0][0] if chunks else 0)
        return 0

    if not args.port:
        ap.error("ange --port, --replay eller --simulate")
    baud, par = args.baud, args.parity
    if args.auto:
        print(f"Söker inställning på {args.port} ({args.auto_seconds}s per kombination)...")
        res = autodetect(args.port, args.auto_seconds,
                         progress=lambda i, n, b, p, s: print(f"  [{i + 1}/{n}] {b} {p}{s}", end="\r"))
        print()
        for r in res[:10]:
            print(f"  {r['baud']:>6} 8{r['parity']}{r['stop']}  ramar {r['frames']:>4}  skräp/crc {r['bad']:>3}  "
                  f"UART-fel {r['uart_err']:>3}  poäng {r['score']}")
        if not res or res[0]["frames"] == 0:
            print("Inga giltiga Modbus-ramar hittades. Kontrollera A/B, att bussen har trafik och adaptern.")
            return 1
        baud, par, stop = res[0]["baud"], res[0]["parity"], res[0]["stop"]
        note = autodetect_note(res[0], res)
        print(f"Vald: {baud} 8{par}{stop}. {note}")
    mon = Monitor(baud, par, stop, resp_timeout=args.timeout / 1000, gap=gap)
    t0 = time.perf_counter()
    mon.on_event = lambda e: _print_ev(e, args, t0)
    hdr = make_header(args.port, baud, par, stop)
    if args.trap:
        mon.trap = ErrorTrap(args.trap, args.trap_pre, args.trap_post, args.trap_dir, hdr)
    cap = Capture(mon, args.port, baud, par, stop, log_path=args.log)
    cap.start()
    print(f"Lyssnar på {args.port} {baud} 8{par}{stop} (passivt, Ctrl+C för att avsluta)")
    next_stats = time.time() + args.stats_every
    try:
        while cap.th.is_alive():
            time.sleep(0.2)
            sys.stdout.flush()
            if args.stats_every and time.time() > next_stats:
                next_stats = time.time() + args.stats_every
                print("\n" + stats_table(mon.stats_rows()) + "\n")
        if cap.error:
            print("Fel:", cap.error)
    except KeyboardInterrupt:
        pass
    cap.stop()
    finish(mon, t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
