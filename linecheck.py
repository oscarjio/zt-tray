"""
Linjeanalys för RS485/Modbus RTU: "koppla på och kör".

* UART-emulator på bitnivå (sändare + mottagare) så att fel baud, fel paritet/stoppbitar,
  A/B-omkastning (inverterad linje) och en enskild slav med avvikande inställning kan
  simuleras och testas utan hårdvara.
* Auto-detektering mot riktig port: provar baud/paritet/stopp, räknar giltiga CRC-ramar,
  skräp, UART-fel (ram-/paritetsfel via ClearCommError) och bitinverterade ramar.
* diagnose(): bästa inställning + konfidens + varningar i klartext (för hög/låg baud,
  paritet, stoppbitar, A/B, slav med avvikande inställning).

Begränsning: en USB-adapter levererar bytes i block (FTDI latency 1-16 ms). Exakt
bitbredd kan därför INTE mätas; baud uppskattas grovt ur bytetakten i långa ramar
och säkert genom att prova standardhastigheter och räkna giltiga CRC-ramar.
"""

import bisect
import time

import modbus_monitor as M

STD_BAUDS = (1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600, 76800, 115200)


def _cfg_bits(par, stop):
    return 1 + 8 + (0 if par == "N" else 1) + int(stop)


# ------------------------------------------------------------------ UART-emulator

def encode_wave(byte_times, cfg_for, invert=False):
    """byte_times [(t_slut, byte, tagg)] -> kanter [(t, nivå)]. cfg_for(tagg) -> (baud, par, stop)."""
    edges = []
    cur = 1
    prev_end = -1e9
    for item in byte_times:
        t_end, b = item[0], item[1]
        tag = item[2] if len(item) > 2 else None
        baud, par, stop = cfg_for(tag)
        bt = 1.0 / baud
        bits = [0] + [(b >> k) & 1 for k in range(8)]
        if par != "N":
            ones = bin(b).count("1")
            bits.append((ones & 1) if par == "E" else (1 - (ones & 1)))
        bits += [1] * int(stop)
        t = max(t_end - len(bits) * bt, prev_end)
        for v in bits:
            if v != cur:
                edges.append((t, v))
                cur = v
            t += bt
        prev_end = t
    if invert:
        edges = [(t, 1 - v) for t, v in edges]
    return edges


def decode_wave(edges, rx, invert=False):
    """Mottagare (rx=(baud, par, stop)) samplar linjen mitt i varje bit som en UART.
    Returnerar (bytes [(t, byte)], fel [(t, 'frame'|'parity')])."""
    baud, par, stop = rx
    bt = 1.0 / baud
    idle = 0 if invert else 1
    times = [e[0] for e in edges]
    levels = [e[1] for e in edges]

    def lvl(t):
        i = bisect.bisect_right(times, t) - 1
        return idle if i < 0 else levels[i]

    out, errs = [], []
    j = 0
    n = len(edges)
    t_next = -1e18
    while True:
        j = bisect.bisect_left(times, t_next, j)
        while j < n:
            prev = idle if j == 0 else levels[j - 1]
            if levels[j] == 0 and prev == 1:
                break
            j += 1
        if j >= n:
            break
        t0 = times[j]
        if lvl(t0 + 0.5 * bt) != 0:
            t_next = t0 + 0.5 * bt
            j += 1
            continue
        b = 0
        for k in range(8):
            b |= lvl(t0 + (1.5 + k) * bt) << k
        pos = 9.5
        perr = False
        if par != "N":
            pb = lvl(t0 + pos * bt)
            ones = bin(b).count("1") + pb
            perr = (ones & 1) != (0 if par == "E" else 1)
            pos += 1
        ferr = lvl(t0 + pos * bt) != 1
        tb = t0 + (pos + 0.5) * bt
        out.append((tb, b))
        if ferr:
            errs.append((tb, "frame"))
        if perr:
            errs.append((tb, "parity"))
        t_next = t0 + pos * bt
        j += 1
    return out, errs


def capture_line(byte_times, tx, rx, slave_cfg=None, invert=False, usb_latency=0.001):
    """Simulerar vad en USB-RS485-adapter inställd på rx tar emot när bussen kör tx.
    slave_cfg {adress: (baud, par, stop)} = slavar med avvikande inställning.
    Returnerar (chunks, fel)."""
    slave_cfg = slave_cfg or {}

    def cfg_for(tag):
        return slave_cfg.get(tag, tx) if isinstance(tag, int) else tx
    edges = encode_wave(byte_times, cfg_for, invert)
    got, errs = decode_wave(edges, rx, invert)
    chunks = M.chunk_bytes(got, usb_latency)
    return chunks, errs


def feed(mon, chunks, errs=()):
    """Spela upp chunks + UART-fel i en Monitor (fel rapporteras vid närmast följande block)."""
    errs = sorted(errs)
    k = 0
    for t, d in chunks:
        mon.tick(t - 1e-6)
        while k < len(errs) and errs[k][0] <= t:
            mon.line_error(t, errs[k][1])
            k += 1
        mon.feed(t, d)
    if chunks:
        mon.tick(chunks[-1][0] + 5)
    mon.flush()
    return mon


# ------------------------------------------------------------------ poängsättning

def score_capture(chunks, errs, baud, par, stop):
    """Statistik för en inspelning tolkad med (baud, par, stop)."""
    data = b"".join(d for _t, d in chunks)
    ok, bad = M.score_bytes(chunks, baud, par, stop)
    inv = [(t, bytes(x ^ 0xFF for x in d)) for t, d in chunks]
    inv_ok, _ = M.score_bytes(inv, baud, par, stop) if data else (0, 0)
    fe = sum(1 for e in errs if e[1] == "frame") if errs and not isinstance(errs, dict) else (errs or {}).get("frame", 0) if isinstance(errs, dict) else 0
    pe = sum(1 for e in errs if e[1] == "parity") if errs and not isinstance(errs, dict) else (errs or {}).get("parity", 0) if isinstance(errs, dict) else 0
    zeros = data.count(0) / len(data) if data else 0.0
    bursts = sum(1 for (a, _x), (b, _y) in zip(chunks, chunks[1:]) if b - a > 0.003)
    return {"baud": baud, "parity": par, "stop": stop, "bytes": len(data), "frames": ok, "bad": bad,
            "frame_err": fe, "parity_err": pe, "uart_err": fe + pe, "inv_frames": inv_ok,
            "zero_share": round(zeros, 3), "bursts": bursts,
            "break": (errs or {}).get("break", 0) if isinstance(errs, dict) else 0, "rate_baud": estimate_baud(chunks), "score": 0, "note": ""}


def estimate_baud(chunks, bits=11):
    """Grov baud ur bytetakt inne i långa ramar (kräver block tätare än ramlängden).
    Returnerar närmaste standardbaud eller None om underlaget är för litet."""
    rates = []
    for (t1, d1), (t2, d2) in zip(chunks, chunks[1:]):
        dt = t2 - t1
        if 0.0004 < dt < 0.02 and len(d2) >= 1:
            rates.append(len(d2) / dt)
    if len(rates) < 20:
        return None
    rates.sort()
    r = rates[int(len(rates) * 0.9)]
    est = r * bits
    best = min(STD_BAUDS, key=lambda b: abs(b - est) / b)
    return best if abs(best - est) / best < 0.3 else None


def rank(res):
    """Poäng: giltiga ramar, minus skräp, viktat med andel UART-fel. Lika -> Modbus-spec
    (paritet -> 1 stoppbit, ingen paritet -> 2 stoppbitar)."""
    for r in res:
        if r.get("score") == -1:
            continue
        ushare = min(1.0, r["uart_err"] / max(1, r["bytes"]))
        spec = 2 if r["parity"] == "N" else 1
        r["score"] = round(r["frames"] * 10 * (1 - ushare) - r["bad"] * 20 + (0.5 if r["stop"] == spec else 0)
                           + (0.2 if r["parity"] != "N" and r["uart_err"] == 0 else 0), 1)
    res.sort(key=lambda r: (r["score"], r["frames"]), reverse=True)
    return res


def sim_scan(byte_times, tx, slave_cfg=None, invert=False, bauds=M.BAUDS, parities=M.PARITIES,
             stops=M.STOPS, usb_latency=0.001):
    """Auto-sök mot en simulerad buss (för test)."""
    res = []
    for b in bauds:
        for p in parities:
            for s in stops:
                ch, er = capture_line(byte_times, tx, (b, p, s), slave_cfg, invert, usb_latency)
                res.append(score_capture(ch, er, b, p, s))
    return rank(res)


def live_scan(port_name, seconds=1.5, bauds=M.BAUDS, parities=M.PARITIES, stops=M.STOPS,
              progress=None, stop_flag=None):
    """Auto-sök mot riktig port. Läser passivt varje kombination 'seconds' s. Sänder aldrig."""
    res = []
    combos = [(b, p, s) for b in bauds for p in parities for s in stops]
    for i, (b, p, s) in enumerate(combos):
        if stop_flag and stop_flag():
            break
        if progress:
            progress(i, len(combos), b, p, s)
        try:
            port = M.open_port(port_name, b, p, s)
        except Exception as e:
            res.append({"baud": b, "parity": p, "stop": s, "bytes": 0, "frames": 0, "bad": 0, "frame_err": 0,
                        "parity_err": 0, "uart_err": 0, "inv_frames": 0, "zero_share": 0, "rate_baud": None,
                        "score": -1, "note": str(e)})
            continue
        chunks = []
        t_end = time.perf_counter() + seconds
        try:
            port.errors()
            base = dict(port.errors() or {})
            while time.perf_counter() < t_end:
                d = port.read()
                if d:
                    chunks.append((time.perf_counter(), d))
            errs = port.errors() or {}
        finally:
            port.close()
        e2 = {k: errs.get(k, 0) - base.get(k, 0) for k in errs}
        res.append(score_capture(chunks, e2, b, p, s))
    return rank(res)


# ------------------------------------------------------------------ diagnos

def _lbl(r):
    return f"{r['baud']} 8{r['parity']}{r['stop']}"


def diagnose(res, current=None, stats_rows=None):
    """Tolkar en skanning. current=(baud, par, stop) som användaren har ställt in.
    Returnerar {"best", "confidence" (0-100), "verdict", "warnings": [...], "table": res}."""
    out = {"best": None, "confidence": 0, "verdict": "", "warnings": [], "table": res}
    w = out["warnings"]
    valid = [r for r in res if r.get("score", 0) != -1]
    total_bytes = max((r["bytes"] for r in valid), default=0)
    if not valid or total_bytes == 0:
        out["verdict"] = ("Ingen trafik alls. Kontrollera kabel/plint, att adaptern sitter på rätt par, "
                          "att PLC:n är igång och pollar, och COM-port/drivrutin.")
        return out
    best = valid[0]
    if best["frames"] < 3:
        inv = max(valid, key=lambda r: r["inv_frames"])
        if inv["inv_frames"] > 0:
            out["best"] = inv
            out["confidence"] = 70
            out["verdict"] = (f"Data verkar INVERTERAD: giltiga ramar först efter bitinvertering ({_lbl(inv)}). "
                              "A och B är troligen omkastade - byt plats på A/B (D+/D-).")
            return out
        fe_share = sum(r["frame_err"] for r in valid) / max(1, sum(r["bytes"] for r in valid))
        zero = max(r["zero_share"] for r in valid)
        brk = sum(r.get("break", 0) for r in valid)
        bursts = max(r.get("bursts", 0) for r in valid)
        top = max(valid, key=lambda r: r["baud"])
        est = top.get("rate_baud") if top.get("rate_baud") and top["rate_baud"] < top["baud"] else None
        out["verdict"] = "Trafik finns men inga giltiga Modbus-ramar vid någon standardinställning."
        if bursts >= 10:
            out["verdict"] += (" Trafiken kommer i skurar som fråga/svar - mest troligt är A och B OMKASTADE "
                               "(inverterad linje). Byt A/B och kör auto-sök igen.")
            out["ab_suspect"] = True
        if brk:
            w.append(f"{brk} 'break'-tillstånd: linjen ligger låg i vila - klassiskt tecken på omkastade A/B "
                     "eller saknad bias/terminering.")
        if zero > 0.3 or fe_share > 0.3:
            w.append("Många ramfel/0x00-byte vid alla hastigheter: typiskt för omkastade A/B, baud utanför "
                     "listan eller fel par på en 4-trådsbuss.")
        else:
            w.append("Möjligt: annat protokoll (Modbus ASCII?), baud utanför listan, eller bara brus.")
        if est:
            w.append(f"Bytetakten antyder ungefär {est} baud (grov uppskattning, USB-adaptern ger inte exakt timing).")
        return out
    out["best"] = best
    share = best["frames"] / max(1, best["frames"] + best["bad"])
    conf = 100 * share
    if best["uart_err"]:
        conf -= min(30, 3 * best["uart_err"])
    others = [r for r in valid[1:] if r["frames"] > 0 and r["uart_err"] <= best["uart_err"]
              and (r["baud"], r["parity"]) != (best["baud"], best["parity"])]
    if others and others[0]["frames"] >= best["frames"] * 0.9:
        conf -= 25
    if best["frames"] < 5:
        conf -= 30
    out["confidence"] = max(5, min(99, int(conf)))
    out["verdict"] = f"Bussen kör {_lbl(best)} ({best['frames']} giltiga ramar, konfidens {out['confidence']} %)."
    note = M.autodetect_note(best, res)
    if note:
        w.append(note)
    # paritet: samma baud, andra pariteter
    for r in valid:
        if r["baud"] == best["baud"] and r["parity"] != best["parity"] and r["frames"] and r["parity_err"]:
            w.append(f"Med paritet {r['parity']} kommer ramarna fram men med {r['parity_err']} paritetsfel - "
                     f"bussen använder {best['parity']}.")
            break
    if best["inv_frames"] and best["inv_frames"] > best["frames"] * 0.2:
        w.append("En del trafik blir giltig först när den inverteras - någon enhet kan ha A/B omkastade.")
    if best["bad"] > best["frames"] * 0.05 or best["uart_err"]:
        w.append(f"{best['bad']} trasiga ramar och {best['uart_err']} UART-fel även vid bästa inställningen - "
                 "störning, terminering/bias, eller en enhet med avvikande inställning (se per adress).")
    if current:
        cb, cp, cs = current
        if int(cb) != best["baud"]:
            dirn = "för HÖG" if int(cb) > best["baud"] else "för LÅG"
            w.append(f"Inställd baud {cb} är {dirn} - bussen kör {best['baud']}. "
                     + ("För hög baud ger fler, trasiga byte med ramfel; " if int(cb) > best["baud"] else
                        "För låg baud ger färre byte och ramfel; ") + "inga giltiga CRC-ramar.")
        elif cp != best["parity"]:
            w.append(f"Inställd paritet {cp} stämmer inte - bussen kör {best['parity']} (paritets-/ramfel per byte).")
        elif int(cs) != best["stop"] and not note:
            w.append(f"Inställda stoppbitar {cs} - bussen verkar köra {best['stop']}.")
    if stats_rows:
        w.extend(slave_anomalies(stats_rows))
    return out


def slave_anomalies(rows):
    """Hitta enskilda slavar vars svar får UART-/CRC-fel när övriga är rena
    (typiskt: den slaven har annan paritet/stoppbitar/baud än mastern)."""
    bad = []
    for r in rows:
        tries = r.get("frågor", 0)
        if tries < 5:
            continue
        nbad = r.get("trasiga_svar", 0) + r.get("crc", 0) + r.get("linjefel", 0)
        bad.append((r["slav"], min(1.0, nbad / tries), r))
    if not bad:
        return []
    worst = max(s for _a, s, _r in bad)
    clean = [b for b in bad if b[1] < 0.05]
    out = []
    for a, s, r in bad:
        if s > 0.3 and s >= 0.5 * worst:
            txt = (f"Slav {a}: {r.get('linjefel', 0)} UART-fel och {r.get('trasiga_svar', 0) + r.get('crc', 0)} "
                   f"trasiga svar på {r['frågor']} frågor")
            if clean:
                txt += (f", medan {len(clean)} andra slavar är rena - slav {a} har troligen annan "
                        "paritet/stoppbitar/baud än mastern (eller dålig kabel/terminering vid den).")
            else:
                txt += "."
            out.append(txt)
    return out


def report_text(d):
    lines = [d["verdict"]] + ["⚠ " + x for x in d["warnings"]]
    return "\n".join(lines)
