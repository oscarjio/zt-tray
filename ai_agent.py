"""
AI-felsökning av fångad Modbus-trafik (OpenAI-kompatibelt API, standard OpenRouter).

- Nyckeln läses från config (cfg["ai"]["api_key"]) eller miljövariabeln OPENROUTER_API_KEY /
  OPENAI_API_KEY. Den skrivs ALDRIG till logg, journal eller felmeddelanden.
- Modellen får kompakta sammanfattningar via verktyg, inte rådata i bulk.
- Sändverktyg finns bara när "Tillåt AI att sända" är på, och varje sändning måste
  bekräftas i UI:t (confirm-callback).
"""

import collections
import datetime as _dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import modbus_monitor as M

DEFAULT_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
MODELS = [DEFAULT_MODEL, "~deepseek/deepseek-v4-flash-latest", "deepseek/deepseek-chat",
          "openai/gpt-4o-mini", "google/gemini-2.5-flash", "anthropic/claude-sonnet-4"]
# USD per miljon tokens (in, ut) - uppskattning när API:t inte returnerar kostnad
PRICES = {DEFAULT_MODEL: (0.0475, 0.1568)}
MAX_TOOL_CHARS = 6000
MAX_STEPS = 10


def get_key(cfg=None):
    k = ((cfg or {}).get("ai") or {}).get("api_key") or ""
    return k.strip() or os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")


def mask(k):
    if not k:
        return "(ingen)"
    return k[:5] + "…" + k[-4:] if len(k) > 12 else "****"


def redact(text, key):
    text = str(text)
    if key:
        text = text.replace(key, "***")
    return re.sub(r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+", r"\1***", text)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, api_key, base_url=DEFAULT_BASE, model=DEFAULT_MODEL, timeout=120):
        self._key = api_key
        self.base = (base_url or DEFAULT_BASE).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout

    def __repr__(self):
        return f"LLMClient({self.base}, {self.model}, key={mask(self._key)})"

    def chat(self, messages, tools=None):
        body = {"model": self.model, "messages": messages, "temperature": 0.2}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if "openrouter" in self.base:
            body["usage"] = {"include": True}
        req = urllib.request.Request(self.base + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + self._key,
                                              "Content-Type": "application/json",
                                              "HTTP-Referer": "https://github.com/oscarjio/zt-tray",
                                              "X-Title": "zt-tray Modbus"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                msg = ""
            raise LLMError(redact(f"HTTP {e.code}: {msg}", self._key)) from None
        except Exception as e:
            raise LLMError(redact(f"Nätverksfel: {e}", self._key)) from None
        if "error" in data:
            raise LLMError(redact(str(data["error"])[:400], self._key))
        msg = data["choices"][0]["message"]
        return msg, data.get("usage") or {}


# ------------------------------------------------------------------ långtidsjournal

class Journal:
    """Långtidsminne utöver Monitorns ringbuffert: alla felhändelser (kompakt), minut-hinkar
    och incidenter (kluster av fel). Kan skriva felhändelser till disk (JSONL)."""

    def __init__(self, path=None, keep=200000, cluster_s=30.0):
        self.errors = collections.deque(maxlen=keep)       # (wall, kind, slave, asked, fc, rtt_ms, text)
        self.buckets = collections.OrderedDict()           # minut -> Counter
        self.incidents = []
        self.marks = []                                    # (wall, text) - användaren: "nu strular det"
        self.summaries = []                                # periodiska lokala sammanfattningar
        self.cluster_s = cluster_s
        self.path = path
        self._last_t = None
        self.started = time.time()
        self.lock = threading.Lock()

    def update(self, mon, to_wall):
        if mon is None:
            return 0
        with mon.lock:
            evs = list(mon.events)
        new = [e for e in evs if self._last_t is None or e.t > self._last_t]
        if not new:
            return 0
        self._last_t = new[-1].t
        lines = []
        with self.lock:
            for e in new:
                w = to_wall(e.t)
                mkey = int(w // 60) * 60
                b = self.buckets.setdefault(mkey, collections.Counter())
                if e.kind in ("resp",):
                    b["ok"] += 1
                if e.kind in M.ERROR_KINDS:
                    b[e.kind] += 1
                    rec = (w, e.kind, e.slave, e.asked, e.fc, None if e.rtt is None else round(e.rtt * 1000, 1),
                           e.text[:120])
                    self.errors.append(rec)
                    self._cluster(rec)
                    if self.path:
                        lines.append(json.dumps({"t": _fmt(w), "kind": e.kind, "slav": e.slave, "fragad": e.asked,
                                                 "fc": e.fc, "rtt_ms": rec[5], "text": rec[6]},
                                                ensure_ascii=False))
            while len(self.buckets) > 60 * 24 * 7:
                self.buckets.popitem(last=False)
        if lines:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            except OSError:
                pass
        return len(new)

    def _cluster(self, rec):
        w = rec[0]
        if self.incidents and w - self.incidents[-1]["end"] <= self.cluster_s:
            inc = self.incidents[-1]
        else:
            inc = {"start": w, "end": w, "n": 0, "kinds": collections.Counter(), "slaves": collections.Counter()}
            self.incidents.append(inc)
            if len(self.incidents) > 2000:
                self.incidents.pop(0)
        inc["end"] = w
        inc["n"] += 1
        inc["kinds"][rec[1]] += 1
        inc["slaves"][rec[3] if rec[3] is not None else rec[2]] += 1

    def mark(self, text):
        self.marks.append((time.time(), text[:200]))

    def incident_list(self, since=None, limit=40):
        with self.lock:
            incs = [i for i in self.incidents if since is None or i["end"] >= since]
        out = []
        for i in incs[-limit:]:
            out.append({"start": _fmt(i["start"]), "slut": _fmt(i["end"]), "längd_s": round(i["end"] - i["start"], 1),
                        "antal_fel": i["n"], "typer": dict(i["kinds"]),
                        "slavar": {str(k): v for k, v in i["slaves"].items()}})
        return out

    def timeline(self, minutes=60, step=5):
        now = time.time()
        rows = []
        with self.lock:
            items = [(k, v) for k, v in self.buckets.items() if k >= now - minutes * 60]
        agg = collections.OrderedDict()
        for k, v in items:
            kk = int(k // (step * 60)) * step * 60
            agg.setdefault(kk, collections.Counter()).update(v)
        for k, v in agg.items():
            rows.append(f"{_fmt(k)[:5]} ok={v.get('ok', 0)} " + " ".join(
                f"{a}={n}" for a, n in sorted(v.items()) if a != "ok"))
        return rows

    def periodic(self):
        """Lokal sammanfattning (ingen LLM) - kallas t.ex. var 10:e minut i automatläge."""
        tl = self.timeline(10, 10)
        s = f"{_fmt(time.time())}: " + (tl[-1] if tl else "ingen trafik")
        self.summaries.append(s)
        self.summaries = self.summaries[-500:]
        return s


def _fmt(w):
    return _dt.datetime.fromtimestamp(w).strftime("%H:%M:%S")


def _parse_clock(s, ref=None):
    """'HH:MM[:SS]' (idag) eller sekunder sedan nu ('-90') -> wall."""
    ref = ref or time.time()
    s = str(s).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return ref - abs(float(s))
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if not m:
        raise ValueError("tid ska vara HH:MM:SS eller sekunder bakåt")
    d = _dt.datetime.fromtimestamp(ref)
    t = d.replace(hour=int(m[1]), minute=int(m[2]), second=int(m[3] or 0), microsecond=0)
    if t.timestamp() > ref + 60:
        t -= _dt.timedelta(days=1)
    return t.timestamp()


# ------------------------------------------------------------------ verktyg

def _fn(name, desc, props=None, req=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": list(req)}}}


READ_TOOLS = [
    _fn("get_status", "Länkstatus, linjeinställning, bytes, giltiga/trasiga ramar, UART-fel, övervakningstid, automatläge."),
    _fn("slave_stats", "Statistik per slavadress (frågor, svar, timeouts, CRC, exceptions, sena, krockar, svarstider, UART-fel).",
        {"slave": {"type": "integer", "description": "valfri adress"}}),
    _fn("search_packets", "Sök paket i senaste ringbufferten med filteruttryck, t.ex. 'addr=5 status=timeout', "
        "'fc=3 ms>50', 'status=crc'. Fält: addr, fc, ms, status(ok/timeout/late/krock/crc/exc/feladr/dubbel/skrap), reg.",
        {"filter": {"type": "string"}, "limit": {"type": "integer", "description": "max 60"}}),
    _fn("packets_around", "Paket runt en tidpunkt ('HH:MM:SS' eller sekunder bakåt som '-120').",
        {"time": {"type": "string"}, "window_s": {"type": "number"}, "slave": {"type": "integer"},
         "limit": {"type": "integer"}}, ["time"]),
    _fn("error_events", "Felhändelser ur långtidsjournalen (överlever ringbufferten).",
        {"since_min": {"type": "number", "description": "minuter bakåt, standard 60"},
         "slave": {"type": "integer"}, "kind": {"type": "string",
                                                "description": "timeout/crc/junk/wrongaddr/dup/early/exc/late/collision"},
         "limit": {"type": "integer"}}),
    _fn("incidents", "Kluster av fel (incidenter) och felfrekvens per 5 min, plus användarens markeringar.",
        {"since_min": {"type": "number"}}),
    _fn("registers", "Registervärden som setts i trafiken för en slav (värde, ändringar).",
        {"slave": {"type": "integer"}}, ["slave"]),
    _fn("autodetect", "Passiv auto-sökning av baud/paritet/stoppbitar/A-B-omkastning (lyssnar kort på varje "
        "inställning, sänder inget). Avbryter lyssningen medan den pågår.", {"seconds": {"type": "number"}}),
    _fn("set_line", "Ändra lyssnarens baud/paritet/stoppbitar (påverkar inte bussen).",
        {"baud": {"type": "integer"}, "parity": {"type": "string", "enum": ["N", "E", "O"]},
         "stop": {"type": "integer", "enum": [1, 2]}}, ["baud", "parity", "stop"]),
    _fn("start_listening", "Starta lyssning (monitor) med aktuell inställning."),
    _fn("stop_listening", "Stoppa lyssning."),
    _fn("set_error_trap", "Sätt felfälla: spara rålogg N s före och M s efter varje fel för valda slavar.",
        {"slaves": {"type": "array", "items": {"type": "integer"}}, "pre_s": {"type": "number"},
         "post_s": {"type": "number"}}),
]
SEND_TOOLS = [
    _fn("modbus_request", "SÄNDER på bussen: Modbus-fråga som master (FC 1-6, 15, 16). Användaren måste bekräfta.",
        {"slave": {"type": "integer"}, "fc": {"type": "integer"}, "addr": {"type": "integer"},
         "qty": {"type": "integer"}, "values": {"type": "array", "items": {"type": "integer"}}},
        ["slave", "fc", "addr"]),
    _fn("scan_addresses", "SÄNDER: frågar adress start..end (FC3 reg 0) för att hitta enheter. Kräver bekräftelse.",
        {"start": {"type": "integer"}, "end": {"type": "integer"}}, ["start", "end"]),
]

SYSTEM = """Du är en erfaren felsökare av RS485/Modbus RTU inbyggd i verktyget zt-tray. Du ser trafik som en passiv
lyssnare har fångat (PLC = master, motordrivare/enheter = slavar) via verktyg. Svara på svenska, kort och konkret.
Arbetssätt: hämta status och slavstatistik först, borra sedan ned med sökning/incidenter/paket runt tidpunkter.
Skilj på: fel inställning (baud/paritet/stoppbitar - UART-fel, inga giltiga ramar), A/B omkastade (trafik men inget
avkodbart), terminering/bias/störning (sporadiska CRC/UART-fel, fler vid långa kablar), timing (sena svar, krockar,
för korta pauser), enskild enhet (bara en adress felar), master-problem (frågor uteblir), exceptions (fel register).
Ange alltid: trolig orsak, hur säker du är, bevis (tider, adresser, antal) och nästa konkreta steg.
Sänd bara om det behövs för att verifiera och förklara först varför. USB-adaptrar ger inte exakt bit-timing,
tidsstämplar har ~1-16 ms osäkerhet."""


class Agent:
    def __init__(self, host, client, journal=None, allow_send=lambda: False, confirm=lambda text: False,
                 log=None):
        self.host = host
        self.client = client
        self.journal = journal or Journal()
        self.allow_send = allow_send
        self.confirm = confirm
        self.log = log or (lambda s: None)
        self.history = []
        self.usage = {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0}
        self.auto = None    # {"desc", "since", "last_summary"}
        self.lock = threading.Lock()

    # ---------------- automatläge
    def start_auto(self, desc):
        self.auto = {"desc": desc, "since": time.time(), "last_summary": time.time()}
        self.journal.mark("AUTOMATLÄGE START: " + desc)
        if not self.host.running():
            self.host.start()

    def stop_auto(self):
        self.auto = None

    def tick(self):
        """Anropas ~1 gång/s från UI: matar journalen och gör periodiska sammanfattningar."""
        self.journal.update(self.host.mon(), self.host.to_wall)
        if self.auto and time.time() - self.auto["last_summary"] > 600:
            self.auto["last_summary"] = time.time()
            self.journal.periodic()

    # ---------------- verktyg
    def tools(self):
        return READ_TOOLS + (SEND_TOOLS if self.allow_send() else [])

    def run_tool(self, name, args):
        try:
            res = self._tool(name, args or {})
        except Exception as e:
            res = {"fel": str(e)[:300]}
        s = json.dumps(res, ensure_ascii=False, default=str)
        if len(s) > MAX_TOOL_CHARS:
            s = s[:MAX_TOOL_CHARS] + " …(avkortat, snäva in sökningen)"
        return s

    def _events(self):
        mon = self.host.mon()
        if mon is None:
            return []
        with mon.lock:
            return list(mon.events)

    def _ev_line(self, e):
        w = self.host.to_wall(e.t)
        return f"{_fmt(w)}.{int((w % 1) * 1000):03d} " + e.line(e.t).strip().split(" ", 1)[1].strip()[:200]

    def _tool(self, name, a):
        h = self.host
        if name == "get_status":
            st = h.status()
            st["övervakat_sedan"] = _fmt(self.journal.started)
            st["automatläge"] = self.auto["desc"] if self.auto else "av"
            st["fel_i_journal"] = len(self.journal.errors)
            return st
        if name == "slave_stats":
            mon = h.mon()
            if mon is None:
                return {"fel": "ingen lyssning"}
            names = {int(k): v for k, v in h.names().items()}
            keep = ("slav", "frågor", "svar", "timeout", "crc", "exception", "sena", "krockar", "feladr",
                    "linjefel", "trasiga_svar", "felprocent", "min", "medel", "max", "p99", "senaste_fel",
                    "exc_koder")
            out = []
            for r in mon.stats_rows():
                if a.get("slave") is not None and r["slav"] != a["slave"]:
                    continue
                d = {k: (round(r[k], 1) if isinstance(r.get(k), float) else r.get(k)) for k in keep}
                d["namn"] = names.get(r["slav"], "")
                if r.get("senaste_fel_t"):
                    d["senaste_fel_tid"] = _fmt(h.to_wall(r["senaste_fel_t"]))
                out.append(d)
            return out
        if name == "search_packets":
            flt = M.parse_filter(a.get("filter", "") or "")
            lim = max(1, min(60, int(a.get("limit") or 30)))
            hits = [e for e in self._events() if flt(e)]
            return {"träffar": len(hits), "visar_senaste": [self._ev_line(e) for e in hits[-lim:]]}
        if name == "packets_around":
            w = _parse_clock(a["time"])
            win = float(a.get("window_s") or 5)
            lim = max(1, min(120, int(a.get("limit") or 60)))
            evs = [e for e in self._events() if abs(h.to_wall(e.t) - w) <= win
                   and (a.get("slave") is None or a["slave"] in (e.slave, e.asked))]
            if not evs:
                return {"info": "inga paket i ringbufferten då (kan vara utanför bufferten - använd error_events)"}
            mid = min(range(len(evs)), key=lambda i: abs(h.to_wall(evs[i].t) - w))
            lo = max(0, mid - lim // 2)
            return {"antal": len(evs), "paket": [self._ev_line(e) for e in evs[lo:lo + lim]]}
        if name == "error_events":
            since = time.time() - 60 * float(a.get("since_min") or 60)
            lim = max(1, min(80, int(a.get("limit") or 40)))
            with self.journal.lock:
                rows = [r for r in self.journal.errors if r[0] >= since
                        and (a.get("slave") is None or a["slave"] in (r[2], r[3]))
                        and (not a.get("kind") or r[1] == a["kind"])]
            cnt = collections.Counter(r[1] for r in rows)
            return {"antal": len(rows), "per_typ": dict(cnt),
                    "senaste": [f"{_fmt(r[0])} {r[1]} slav {r[2]} (frågad {r[3]}) fc {r[4]} {r[5] or ''}ms {r[6]}"
                                for r in rows[-lim:]]}
        if name == "incidents":
            since = time.time() - 60 * float(a.get("since_min") or 240)
            return {"incidenter": self.journal.incident_list(since),
                    "per_5_min": self.journal.timeline(int(float(a.get("since_min") or 240)), 5)[-48:],
                    "markeringar": [f"{_fmt(t)} {x}" for t, x in self.journal.marks[-20:]],
                    "lokala_sammanfattningar": self.journal.summaries[-12:]}
        if name == "registers":
            mon = h.mon()
            if mon is None:
                return {"fel": "ingen lyssning"}
            rows = mon.regmap.rows(int(a["slave"]))
            return {"antal": len(rows), "register": [str(r)[:160] for r in rows[:80]]}
        if name == "autodetect":
            return h.autodetect(float(a.get("seconds") or 2.0))
        if name == "set_line":
            return h.set_line(int(a["baud"]), a["parity"], int(a["stop"]))
        if name == "start_listening":
            return h.start()
        if name == "stop_listening":
            return h.stop()
        if name == "set_error_trap":
            return h.set_trap(a.get("slaves") or [], float(a.get("pre_s") or 10), float(a.get("post_s") or 3))
        if name in ("modbus_request", "scan_addresses"):
            if not self.allow_send():
                return {"fel": "Sändning är inte tillåten för AI (kryssrutan 'Tillåt AI att sända' är av)."}
            if name == "modbus_request":
                desc = (f"AI vill SÄNDA: slav {a['slave']} FC{a['fc']} adress {a['addr']} antal {a.get('qty', 1)}"
                        + (f" värden {a.get('values')}" if a.get("values") is not None else ""))
            else:
                desc = f"AI vill SÄNDA en adress-scan {a['start']}..{a['end']} (FC3 reg 0)"
            if not self.confirm(desc):
                return {"fel": "Användaren nekade sändningen."}
            self.journal.mark(desc)
            if name == "modbus_request":
                return h.master_request(int(a["slave"]), int(a["fc"]), int(a["addr"]), int(a.get("qty") or 1),
                                        a.get("values"))
            return h.master_scan(int(a["start"]), int(a["end"]))
        return {"fel": f"okänt verktyg {name}"}

    # ---------------- chatt
    def _context(self):
        st = self.host.status()
        c = (f"Nu: {_fmt(time.time())}. Lyssnare: {st.get('linje')} körs={st.get('körs')}. "
             f"Kända enheter: {json.dumps(self.host.names(), ensure_ascii=False)}. "
             f"Sändning tillåten för AI: {'ja (kräver bekräftelse)' if self.allow_send() else 'nej'}.")
        if self.auto:
            c += (f"\nAUTOMATLÄGE sedan {_fmt(self.auto['since'])}. Användarens felbeskrivning: {self.auto['desc']!r}. "
                  f"Incidenter hittills: {len(self.journal.incident_list(self.auto['since'], 2000))}. Om användaren "
                  "säger att det strular NU: titta på de senaste minuterna (error_events/packets_around), jämför med "
                  "tidigare incidenter, och ge trolig orsak + nästa steg.")
        return c

    def chat(self, text, on_step=None):
        with self.lock:
            if self.auto and re.search(r"strular|nu händer|händer nu|felet nu|nu igen|nu kom", text, re.I):
                self.journal.mark("ANVÄNDAREN: " + text)
            self.history.append({"role": "user", "content": text})
            self.history = self.history[-30:]
            while self.history and self.history[0]["role"] != "user":
                self.history.pop(0)
            for _step in range(MAX_STEPS):
                msgs = [{"role": "system", "content": SYSTEM + "\n\n" + self._context()}] + self.history
                msg, usage = self.client.chat(msgs, self.tools())
                self._account(usage)
                calls = msg.get("tool_calls") or []
                entry = {"role": "assistant", "content": msg.get("content") or ""}
                if calls:
                    entry["tool_calls"] = calls
                self.history.append(entry)
                if not calls:
                    return entry["content"]
                for c in calls:
                    fn = c.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    if on_step:
                        on_step(f"→ {fn.get('name')}({json.dumps(args, ensure_ascii=False)[:120]})")
                    out = self.run_tool(fn.get("name"), args)
                    self.history.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
            self.history.append({"role": "assistant", "content": "(avbröt efter max antal verktygssteg)"})
            return "(avbröt efter max antal verktygssteg - fråga mer specifikt)"

    def _account(self, u):
        pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        self.usage["prompt"] += pt
        self.usage["completion"] += ct
        self.usage["calls"] += 1
        if u.get("cost") is not None:
            self.usage["cost"] += float(u["cost"])
        else:
            pin, pout = PRICES.get(getattr(self.client, "model", ""), (0.0, 0.0))
            self.usage["cost"] += (pt * pin + ct * pout) / 1e6

    def usage_text(self):
        u = self.usage
        return f"{u['calls']} anrop, {u['prompt']} in + {u['completion']} ut tokens, ≈ ${u['cost']:.4f}"

    def reset(self):
        self.history = []
