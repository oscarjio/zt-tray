"""
Adresskrock-detektering: två (eller fler) enheter med samma slavadress.
Arbetar på monitorns händelser (Event) och ger per adress en konfidenspoäng 0..1 med skäl i klartext.
"""

import collections

MIN_REQ = 30        # minst så många frågor till adressen innan vi bedömer
FLAG = 0.5          # poäng från vilken vi visar "Misstänkt adresskrock"


def _bimodal(rtts):
    """Två tydliga kluster av svarstider? Returnerar (bool, ms1, ms2)."""
    r = sorted(rtts)
    n = len(r)
    if n < 12:
        return False, None, None
    best, k = 0.0, None
    for i in range(1, n):
        g = r[i] - r[i - 1]
        if g > best:
            best, k = g, i
    lo, hi = r[:k], r[k:]
    if min(len(lo), len(hi)) < max(3, 0.12 * n):
        return False, None, None
    spread = max(lo[-1] - lo[0], hi[-1] - hi[0], 1e-4)
    m1, m2 = sum(lo) / len(lo), sum(hi) / len(hi)
    ok = best > 2 * spread and best > 0.0015 and m2 / max(m1, 1e-4) > 1.4
    return ok, m1 * 1000, m2 * 1000


def _payload(raw):
    return bytes(raw[2:-2]) if raw and len(raw) >= 5 else b""


def analyze(events, rows=None):
    """Returnerar {adress: {"poäng": float, "skäl": [str], "misstänkt": bool}} för adresser med underlag."""
    ev = list(events)
    per = collections.defaultdict(lambda: {"req": 0, "resp": 0, "dup": 0, "crc": 0, "coll": 0, "garbled": 0,
                                           "rtt": [], "by_req": collections.defaultdict(list), "exc_req": set(),
                                           "ok_req": set()})
    for e in ev:
        k = e.kind
        if k == "req" and e.slave:
            per[e.slave]["req"] += 1
        a = e.asked if getattr(e, "asked", None) is not None else e.slave
        if a is None or a == 0:
            continue
        d = per[a]
        rq = bytes(e.req) if getattr(e, "req", None) else None
        if k == "resp" and e.slave == a:
            d["resp"] += 1
            if e.rtt is not None:
                d["rtt"].append(e.rtt)
            if rq:
                d["by_req"][rq].append(bytes(e.raw))
                d["ok_req"].add(rq)
        elif k == "exc" and e.slave == a:
            d["resp"] += 1
            if e.rtt is not None:
                d["rtt"].append(e.rtt)
            if rq:
                d["exc_req"].add(rq)
        elif k == "dup" and e.slave == a:
            d["dup"] += 1
        elif k == "crc":
            d["crc"] += 1
            if e.raw and e.raw[0] == a:
                d["garbled"] += 1
        elif k == "collision":
            d["coll"] += 1
            if e.raw and e.raw[0] == a:
                d["garbled"] += 1
    # buss-baslinje: CRC/krock-andel för övriga adresser
    tot_req = sum(d["req"] for d in per.values()) or 1
    tot_bad = sum(d["crc"] + d["coll"] for d in per.values())
    out = {}
    for a, d in per.items():
        q = max(d["req"], d["resp"])
        if q < MIN_REQ:
            continue
        score, why = 0.0, []
        if d["dup"] >= 3 and d["dup"] / q >= 0.03:
            score += 0.45
            why.append(f"{d['dup']} dubbla svar på samma fråga")
        bad = d["crc"] + d["coll"]
        rate = bad / q
        other_q = tot_req - d["req"]
        other_rate = (tot_bad - bad) / other_q if other_q > 0 else 0.0
        if bad >= 3 and rate >= 0.03 and rate >= 4 * other_rate:
            score += 0.25
            why.append(f"CRC-fel/krockar koncentrerade till adress {a} ({rate * 100:.0f} % mot {other_rate * 100:.1f} % "
                       "på övriga bussen)")
        if d["garbled"] >= 3:
            score += 0.1
            why.append(f"{d['garbled']} förvrängda svar som börjar med adress {a}")
        lens, flips, multi = 0, 0, 0
        for rq, rs in d["by_req"].items():
            if len(rs) < 3:
                continue
            if len({len(r) for r in rs}) > 1:
                lens += 1
            pl = [_payload(r) for r in rs]
            if len(set(pl)) > 1:
                multi += 1
            if len(set(pl)) > 1:
                for i in range(1, len(pl)):
                    # ett tidigare sett mönster återkommer efter ett annat = hoppar mellan mönster
                    if pl[i] != pl[i - 1] and pl[i] in pl[:i - 1]:
                        flips += 1
        if lens:
            score += 0.3
            why.append(f"olika svarslängd på samma fråga ({lens} frågetyper)")
        if flips >= max(3, 0.1 * d["resp"]):
            score += 0.25
            why.append(f"registervärden hoppar fram och tillbaka mellan två mönster ({flips} ggr)")
        bm, m1, m2 = _bimodal(d["rtt"])
        if bm:
            score += 0.2
            why.append(f"svarstiden har två kluster (~{m1:.1f} ms och ~{m2:.1f} ms)")
        mixed = d["exc_req"] & d["ok_req"]
        if mixed:
            score += 0.15
            why.append(f"samma fråga ger ibland exception, ibland normalt svar ({len(mixed)} frågetyper)")
        signals = len(why)
        if signals < 2 and d["dup"] < 3:
            score = min(score, 0.35)    # en enstaka indikation räcker inte
        score = round(min(1.0, score), 2)
        out[a] = {"poäng": score, "skäl": why, "misstänkt": score >= FLAG}
    return out


def suspects(events, rows=None):
    return {a: r for a, r in analyze(events, rows).items() if r["misstänkt"]}


def headline(a, r):
    return f"Misstänkt adresskrock på adress {a} ({int(r['poäng'] * 100)} %): " + "; ".join(r["skäl"][:3])
