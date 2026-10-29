"""
Agentläge: snabbstart (portförslag + auto-linje) och en kostnadsmedveten övervakningsloop.

- Händelsestyrt: nya felhändelser i journalen (ev. bara för fokus-slavar) triggar en AI-analys, med
  minsta paus mellan analyser och tak per timme.
- Periodisk kort sammanfattning; om inget nytt hänt görs den lokalt utan AI-anrop (kostar inget).
- AI:n ombeds börja svaret med "LARM:" vid fel/fynd som kräver åtgärd, annars "STATUS:".
Sändning på bussen styrs fortfarande av Agent (kryssruta + bekräftelse varje gång).
"""

import time
import collections

CHIPS = {0x1A86: "CH340/CH341", 0x0403: "FTDI", 0x10C4: "CP210x", 0x067B: "PL2303"}

EVENT_PROMPT = ("[AGENTLÄGE - automatisk analys] {n} nya felhändelser sedan {since}{focus}. Exempel: {ex}. "
                "Undersök med verktygen (error_events, slave_stats, search_packets, packets_around). Om linjeinställningen "
                "verkar fel (många CRC/UART-fel på alla slavar): kör autodetect och byt med set_line (påverkar bara "
                "lyssnaren). Svara KORT (max 3 meningar). Börja med 'LARM:' om något kräver åtgärd/ett tydligt fynd, "
                "annars 'STATUS:'.")
SUMMARY_PROMPT = ("[AGENTLÄGE - periodisk sammanfattning] Sammanfatta läget sedan {since}{focus} i max 3 meningar: "
                  "felfrekvens, trend, trolig orsak och nästa steg. Börja med 'LARM:' om något kräver åtgärd, annars 'STATUS:'.")
START_PROMPT = ("[AGENTLÄGE - start] Lyssningen har startat. Gör en första helhetsbedömning av bussen (get_status, "
                "slave_stats). Om inga giltiga ramar syns eller många CRC/UART-fel: kör autodetect och set_line. "
                "Svara KORT (max 3 meningar), börja med 'LARM:' eller 'STATUS:'.")


def chip(info):
    return CHIPS.get(info.get("vid")) or ""


def suggest_ports(infos):
    """Sorterar portar: RS485-adapter, känt USB-serial-chip, annan USB, övriga. Returnerar [(etikett, info)]."""
    def rank(i):
        return (0 if i.get("rs485") else 1 if chip(i) else 2 if i.get("usb") else 3, i["device"])
    out = []
    for i in sorted(infos, key=rank):
        lbl = i["device"]
        extra = [x for x in (chip(i), "RS485" if i.get("rs485") else "", i.get("description") or "") if x]
        if extra:
            lbl += "  –  " + " · ".join(dict.fromkeys(extra))
        out.append((lbl, i))
    return out


def is_alarm(text):
    return (text or "").lstrip().upper().startswith("LARM")


def short(text, n=300):
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


class Watchdog:
    def __init__(self, journal, focus=None, clock=time.time, min_errors=3, cooldown_s=180, summary_s=900,
                 max_per_hour=10):
        self.j, self.focus, self.clock = journal, set(focus or []), clock
        self.min_errors, self.cooldown_s, self.summary_s, self.max_per_hour = min_errors, cooldown_s, summary_s, max_per_hour
        now = clock()
        self.since = now
        self.last_run = 0.0
        self.last_summary = now
        self.runs = []

    def _new_errors(self, since):
        with self.j.lock:
            ev = [e for e in self.j.errors if e[0] > since]
        if self.focus:
            ev = [e for e in ev if e[2] in self.focus]
        return ev

    def _budget(self, now):
        self.runs = [t for t in self.runs if now - t < 3600]
        return len(self.runs) < self.max_per_hour

    def _focus_txt(self):
        return f" (fokus slav {', '.join(str(s) for s in sorted(self.focus))})" if self.focus else ""

    def check(self):
        """Returnerar None, ('ai', prompt) eller ('local', text). Anropas t.ex. var 5:e sekund."""
        now = self.clock()
        new = self._new_errors(self.since)
        if (len(new) >= self.min_errors and now - self.last_run >= self.cooldown_s and self._budget(now)):
            ex = "; ".join(f"slav {e[2]} {e[1]}" for e in new[-3:])
            p = EVENT_PROMPT.format(n=len(new), since=time.strftime("%H:%M:%S", time.localtime(self.since)),
                                    focus=self._focus_txt(), ex=ex)
            self._mark(now)
            return ("ai", p)
        if now - self.last_summary >= self.summary_s:
            since = self.last_summary
            self.last_summary = now
            if self._new_errors(since) and self._budget(now):
                self._mark(now, summary=True)
                return ("ai", SUMMARY_PROMPT.format(since=time.strftime("%H:%M", time.localtime(since)),
                                                    focus=self._focus_txt()))
            return ("local", f"STATUS: inga nya fel sedan {time.strftime('%H:%M', time.localtime(since))}"
                             f"{self._focus_txt()}.")
        return None

    def _mark(self, now, summary=False):
        self.runs.append(now)
        self.last_run = now
        self.since = now
        if summary:
            self.last_summary = now


PAR_TXT = {"N": "none", "E": "even", "O": "odd"}


def stepwise_detect(scan, say, baud=None, bauds=None, seconds=0.6):
    """Hittar linjeinställning i tre synliga steg (lokalt, ingen AI/nyckel krävs, sänder aldrig).
    scan(bauds, parities, stops, seconds) -> rankad lista (som linecheck.live_scan/sim_scan).
    Returnerar (baud, paritet, stopp) eller None om ingen trafik hittas."""
    import modbus_monitor as M
    if baud:
        say(f"Baudrate: {baud} (vald)")
        b = int(baud)
    else:
        say("Söker baudrate…", end="")
        r = scan(tuple(bauds or M.BAUDS), ("E", "N"), (1,), seconds)
        if not r or r[0]["frames"] == 0:
            say(" ingen giltig Modbus-trafik hittad")
            return None
        b = r[0]["baud"]
        say(f" hittat {b}")
    say("Kollar paritet…", end="")
    r = scan((b,), M.PARITIES, (1,), seconds)
    if not r or r[0]["frames"] == 0:
        say(" ingen giltig trafik")
        return None
    p = r[0]["parity"]
    say(" " + PAR_TXT.get(p, p))
    say("Kollar stoppbitar…", end="")
    r = scan((b,), (p,), M.STOPS, seconds)
    s = r[0]["stop"] if r and r[0]["frames"] else (2 if p == "N" else 1)
    say(f" {s}")
    return b, p, s


FOCUS_RE = r"(?:adress|address|slav|slave|enhet|addr)\s*(?:nr\.?\s*)?(\d{1,3}(?:\s*(?:,|och|&)\s*\d{1,3})*)"


def parse_focus(text):
    import re
    m = re.search(FOCUS_RE, text or "", re.I)
    if not m or not re.search(r"fokus|bara|filtrera|titta på|kolla", text, re.I):
        return None
    return [int(x) for x in re.findall(r"\d{1,3}", m.group(1)) if 0 < int(x) < 248]


def wants_monitor(text):
    import re
    return bool(re.search(r"monitor|övervak|bevaka|felsök|hitta fel|larma", text or "", re.I))


def assess(rows, running, frames_ok, bad_frames, uart_err, seconds, names=None, events=None):
    """Enkel lokal helhetsbedömning (ingen AI). Returnerar (nivå, rubrik, [detaljrader]).
    nivå: 'VÄNTAR', 'OK', 'VARNING', 'FEL'."""
    names = names or {}

    def nm(a):
        n = names.get(a) or names.get(str(a))
        return f"enhet {a}" + (f" ({n})" if n else "")
    if not running:
        return "VÄNTAR", "Tryck START", ["Koppla in RS485-kabeln och tryck START."]
    if frames_ok == 0:
        if seconds < 8:
            return "VÄNTAR", "Lyssnar…", ["Väntar på Modbus-trafik."]
        if bad_frames or uart_err:
            return "FEL", "Trafik finns men går inte att läsa", [
                "Fel baud/paritet/stoppbitar, eller A/B omkastade. Tryck START igen för ny automatisk sökning."]
        return "FEL", "Ingen Modbus-trafik", [
            "Kontrollera kabeln, A/B, att adaptern sitter på rätt buss och att mastern (PLC:n) pollar."]
    det, level = [], "OK"
    total = frames_ok + bad_frames
    if total > 20 and bad_frames / total > 0.2:
        level = "FEL"
        det.append(f"{int(100 * bad_frames / total)} % trasiga ramar på bussen – störning, terminering, "
                   "jord/skärm eller fel linjeinställning.")
    for r in rows:
        a, q = r["slav"], r.get("frågor", 0)
        if q < 3:
            continue
        if r.get("missade_pct", 0) >= 50:
            level = "FEL"
            det.append(f"{nm(a)[0].upper() + nm(a)[1:]} svarar inte ({r['timeout']} av {q} frågor utan svar).")
        elif r.get("felprocent", 0) >= 5:
            level = "FEL" if r["felprocent"] >= 20 else ("VARNING" if level == "OK" else level)
            kinds = [f"{r[k]} {k}" for k in ("timeout", "crc", "exception", "sena", "krockar") if r.get(k)]
            det.append(f"{nm(a)[0].upper() + nm(a)[1:]}: {r['felprocent']:.0f} % fel ({', '.join(kinds)}).")
    import linecheck as L
    for t in L.slave_anomalies(rows):
        if t not in det:
            det.append(t)
            level = "VARNING" if level == "OK" else level
    failing = [r["slav"] for r in rows if r.get("frågor", 0) >= 3 and r.get("felprocent", 0) >= 5]
    if events is not None and len(failing) >= 2:
        import modbus_monitor as M
        cc = common_cause(list(events), failing, M.ERROR_KINDS)
        if cc:
            det.insert(0, cc)
    n = sum(1 for r in rows if r.get("svar", 0) > 0)
    head = {"OK": f"Bussen fungerar – {n} enheter svarar", "VARNING": "Varning – sporadiska fel",
            "FEL": "Fel på bussen"}[level]
    return level, head, det or ["Inga fel just nu. Programmet fortsätter bevaka och larmar om något händer."]


class BusHealth:
    """Samplar summerade räknare (1/s) -> busshälsa senaste minut/total och tidskurva per typ."""
    KEYS = ("ok", "missade", "sena", "krockar", "crc")

    def __init__(self, keep=300):
        self.prev = None
        self.hist = collections.deque(maxlen=keep)   # (t, {nyckel: delta})
        self.total = dict.fromkeys(self.KEYS, 0)

    @staticmethod
    def counts(rows):
        c = dict.fromkeys(BusHealth.KEYS, 0)
        for r in rows:
            c["ok"] += max(0, r.get("svar", 0) - r.get("exception", 0) - r.get("trasiga_svar", 0))
            c["missade"] += r.get("timeout", 0)
            c["sena"] += r.get("sena", 0)
            c["krockar"] += r.get("krockar", 0)
            c["crc"] += r.get("crc", 0) + r.get("trasiga_svar", 0)
        return c

    def sample(self, rows, t):
        c = self.counts(rows)
        if self.prev is None or any(c[k] < self.prev[k] for k in self.KEYS):
            self.prev = dict.fromkeys(self.KEYS, 0)
        d = {k: c[k] - self.prev[k] for k in self.KEYS}
        self.prev, self.total = c, c
        self.hist.append((t, d))
        return d

    @staticmethod
    def pct(c):
        n = sum(c.values())
        return None if n == 0 else 100.0 * c["ok"] / n

    def last(self, seconds=60):
        if not self.hist:
            return dict.fromkeys(self.KEYS, 0)
        t1 = self.hist[-1][0]
        s = dict.fromkeys(self.KEYS, 0)
        for t, d in self.hist:
            if t > t1 - seconds:
                for k in self.KEYS:
                    s[k] += d[k]
        return s


def addr_rows(rows, names=None, now=None):
    """En rad per adress: (adress, namn, hälsa%, missade, sena, krockar, orsak i klartext, nivå)."""
    names = names or {}
    out = []
    for r in rows:
        q = r.get("frågor", 0)
        if not q and not r.get("svar"):
            continue
        h = max(0.0, 100.0 - r.get("felprocent", 0.0))
        cause = r.get("senaste_fel") or ""
        if cause and r.get("senaste_fel_t") and now:
            cause += f" ({int(now - r['senaste_fel_t'])} s sedan)"
        if r.get("missade_pct", 0) >= 50:
            cause, lvl = "Svarar inte" + (f" – {cause}" if cause else ""), "FEL"
        else:
            lvl = "FEL" if h < 80 else "VARNING" if h < 95 else "OK"
        n = names.get(r["slav"]) or names.get(str(r["slav"])) or ""
        out.append((r["slav"], n, h, r.get("timeout", 0), r.get("sena", 0), r.get("krockar", 0),
                    cause or "–", lvl))
    return sorted(out, key=lambda x: ({"FEL": 0, "VARNING": 1, "OK": 2}[x[7]], x[0]))


def common_cause(events, failing, err_kinds=None):
    """Flera felande adresser: inträffar felen samtidigt (bussproblem) eller var för sig (enhetsproblem)?
    Returnerar klartextrad eller None."""
    failing = set(failing)
    if len(failing) < 2:
        return None
    bins = collections.defaultdict(set)
    for e in events:
        if e.slave in failing and (err_kinds is None or e.kind in err_kinds):
            bins[int(e.t)].add(e.slave)
    if not bins:
        return None
    shared = sum(1 for s in bins.values() if len(s) >= 2)
    lst = ", ".join(str(a) for a in sorted(failing))
    if shared / len(bins) >= 0.5:
        return (f"Adress {lst} felar SAMTIDIGT ({shared} av {len(bins)} felsekunder gemensamma) – troligen "
                "bussproblem: störning, terminering, kabel/skärm, matning eller mastern/timing, inte enskilda enheter.")
    return (f"Adress {lst} felar VAR FÖR SIG (olika tidpunkter) – troligen enhetsproblem: kontrollera varje enhets "
            "inställningar, adress, kabel och matning.")


AUTO_PROMPT = ("Autoläge ({reason}): gör en grundlig felsökning av bussen nu med verktygen. Om flera adresser felar, "
               "avgör om det är gemensam orsak (samtidigt = bussproblem) eller enhetsproblem. Svara kort: börja med "
               "'LARM:' vid allvarligt fel annars 'STATUS:', sedan fynd per adress och trolig orsak, max 5 meningar.")


class AutoMode:
    """Cursor-agenten var period_s och direkt vid allvarligt fel; stängs av efter duration_s."""

    def __init__(self, duration_s=600, period_s=60, min_gap_s=15, health_min=90.0, now=None):
        now = time.time() if now is None else now
        self.duration_s, self.period_s, self.min_gap_s, self.health_min = duration_s, period_s, min_gap_s, health_min
        self.until = now + duration_s
        self.last_call = 0.0
        self.calls = 0
        self.expired_told = False

    def active(self, now=None):
        return (time.time() if now is None else now) < self.until

    def remaining(self, now=None):
        return max(0, int(self.until - (time.time() if now is None else now)))

    def extend(self, now=None):
        now = time.time() if now is None else now
        self.until = max(self.until, now) + self.duration_s
        self.expired_told = False

    @staticmethod
    def severe(health):
        """Allvarlig händelse från BusHealth: timeout-skur, krock, hälsa under tröskel."""
        l10, l60 = health.last(10), health.last(60)
        if l10["missade"] >= 3:
            return f"timeout-skur ({l10['missade']} missade på 10 s)"
        if l10["krockar"] > 0:
            return f"krock på bussen ({l10['krockar']} st)"
        p = BusHealth.pct(l60)
        if p is not None and sum(l60.values()) >= 20 and p < 90.0:
            return f"busshälsa {p:.0f} % senaste minut"
        return None

    def due(self, health, now=None):
        """Returnerar orsak (str) om agenten ska köras nu, annars None."""
        now = time.time() if now is None else now
        if not self.active(now) or now - self.last_call < self.min_gap_s:
            return None
        sev = self.severe(health) if health is not None else None
        if sev:
            return "allvarligt fel: " + sev
        if now - self.last_call >= self.period_s:
            return "periodisk kontroll"
        return None

    def mark(self, now=None):
        self.last_call = time.time() if now is None else now
        self.calls += 1

    def status(self, now=None):
        r = self.remaining(now)
        return f"Autoläge PÅ – {r // 60}:{r % 60:02d} kvar, {self.calls} analyser" if r else "Autoläge AV"
