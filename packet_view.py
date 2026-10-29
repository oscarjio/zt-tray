"""
Wireshark-lik paketvy för Modbus-monitorn: nr/tid/delta/frågad/svarande/fc/info/svarstid/status,
färgregler, filtersyntax (addr==5 fc==3 status!=ok timeout reg==100), fritextsök (hex eller text)
med nästa/föregående, detaljpanel med avkodade fält + hexdump och markering av fråga<->svar.
Ren Tk – ingen I/O här; fliken matar in händelser med add().
"""

import tkinter as tk
from tkinter import ttk

import modbus_monitor as M

MAX_ROWS = 3000
KEEP_EVENTS = 100000    # max händelser i minnet (äldsta släpps, numreringen behålls)


class EventStore:
    """Lista med global numrering men begränsat minne (lång körning hela dagen)."""

    def __init__(self, keep=KEEP_EVENTS):
        self.keep = keep
        self.base = 0
        self.items = []

    def __len__(self):
        return self.base + len(self.items)

    def __getitem__(self, i):
        if isinstance(i, slice):
            return self.items[i]
        if i < 0:
            return self.items[i]
        if i < self.base:
            raise IndexError("händelsen har släppts ur minnet")
        return self.items[i - self.base]

    def __iter__(self):
        return iter(self.items)

    def has(self, i):
        return self.base <= i < len(self)

    def extend(self, evs):
        self.items.extend(evs)
        if len(self.items) > self.keep:
            drop = len(self.items) - int(self.keep * 0.8)
            del self.items[:drop]
            self.base += drop

    def indexed(self):
        return enumerate(self.items, self.base)

COLORS = {  # status -> (bakgrund, text)
    "timeout": ("#7a1f1f", "#ffd7d5"),   # inget svar: röd
    "late": ("#7a4a10", "#ffe2b0"),      # sent svar: gul/orange
    "krock": ("#4b2a6b", "#ead8ff"),     # krock: lila
    "crc": ("#4b2a6b", "#ead8ff"),       # CRC-fel: lila
    "skrap": ("#4b2a6b", "#ead8ff"),
    "exc": ("#6b4a12", "#ffd9a0"),       # exception: orange
    "feladr": ("#7a1f1f", "#ffd7d5"),    # svar från annan adress: röd
    "dubbel": ("#6b4a12", "#ffd9a0"),
    "gap": ("", "#9aa4ad"),
}


def hexdump(b, width=16):
    out = []
    for i in range(0, len(b), width):
        chunk = b[i:i + width]
        hx = " ".join(f"{x:02x}" for x in chunk)
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in chunk)
        out.append(f"{i:04x}  {hx:<{width * 3}} {asc}")
    return "\n".join(out)


def fields(raw):
    """Avkodade fält för en RTU-ram: [(namn, värde)]."""
    if len(raw) < 2:
        return [("rådata", raw.hex(" "))]
    out = [("adress", f"{raw[0]} (0x{raw[0]:02X})"), ("funktionskod", f"{raw[1] & 0x7F} (0x{raw[1]:02X})"
                                                   + (" exception" if raw[1] & 0x80 else ""))]
    body = raw[2:-2]
    if raw[1] & 0x80 and body:
        out.append(("exception-kod", f"{body[0]} {M.EXC_NAMES.get(body[0], '')}".strip()))
    elif (len(raw) == 8 and raw[1] in (1, 2, 3, 4, 5, 6)) or (raw[1] in (15, 16) and len(body) >= 5 and body[4] == len(body) - 5) or (raw[1] in (15, 16) and len(raw) == 8):
        out.append(("startadress", str((body[0] << 8) | body[1])))
        out.append(("antal/värde", str((body[2] << 8) | body[3])))
        if raw[1] in (15, 16) and len(body) > 5:
            data = body[5:]
            out.append(("byte", str(body[4])))
            if raw[1] == 16:
                out.append(("värden", ", ".join(str((data[i] << 8) | data[i + 1]) for i in range(0, len(data) - 1, 2))))
    elif body and raw[1] in (1, 2, 3, 4) and body[0] == len(body) - 1:
        out.append(("byte", str(body[0])))
        data = body[1:]
        if raw[1] in (3, 4):
            out.append(("register", ", ".join(str((data[i] << 8) | data[i + 1]) for i in range(0, len(data) - 1, 2))))
        else:
            out.append(("bitar", " ".join(f"{x:08b}"[::-1] for x in data)))
    if len(raw) >= 4:
        crc = raw[-2] | (raw[-1] << 8)
        ok = M.crc16(raw[:-2]) == crc
        out.append(("CRC", f"0x{crc:04X}" + ("" if ok is None else (" OK" if ok else " FEL"))))
    return out


class PacketView:
    def __init__(self, parent, names=lambda: {}, timeout_s=lambda: 0.5, on_status=None):
        self.names = names
        self.timeout_s = timeout_s
        self.on_status = on_status or (lambda s: None)
        self.events = EventStore()   # händelser (index = nr - 1), begränsat minne
        self.t0 = 0.0
        self.pred = None
        self.errors_only = tk.BooleanVar(value=False)
        self.paused = False
        self._pending_while_paused = 0
        self._pair_iids = []

        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(4, 2))
        ttk.Label(top, text="Filter").pack(side="left")
        self.ent_filter = ttk.Entry(top, width=34)
        self.ent_filter.pack(side="left", padx=(4, 2))
        self.ent_filter.bind("<Return>", lambda e: self.apply_filter())
        ttk.Button(top, text="Använd", command=self.apply_filter).pack(side="left")
        ttk.Button(top, text="×", width=2, command=self.clear_filter).pack(side="left", padx=(2, 8))
        ttk.Checkbutton(top, text="Bara fel", variable=self.errors_only, command=self.refilter).pack(side="left")
        self.btn_pause = ttk.Button(top, text="⏸ Pausa rullning", command=self.toggle_pause)
        self.btn_pause.pack(side="left", padx=8)
        ttk.Label(top, text="Sök").pack(side="left", padx=(8, 0))
        self.ent_search = ttk.Entry(top, width=18)
        self.ent_search.pack(side="left", padx=(4, 2))
        self.ent_search.bind("<Return>", lambda e: self.search(1))
        ttk.Button(top, text="◀", width=2, command=lambda: self.search(-1)).pack(side="left")
        ttk.Button(top, text="▶", width=2, command=lambda: self.search(1)).pack(side="left")
        self.lbl_flt = ttk.Label(parent, text="Filter: addr==5  fc==3  status!=ok  timeout  sent  krock  reg==100  "
                                 "ms>50  ||  !  — tomt = allt", style="Sub.TLabel")
        self.lbl_flt.pack(anchor="w")

        pw = ttk.PanedWindow(parent, orient="vertical")
        pw.pack(fill="both", expand=True)
        tf = ttk.Frame(pw)
        cols = ("nr", "tid", "delta", "fragad", "svarar", "fc", "info", "ms", "status")
        heads = ("Nr", "Tid s", "Delta ms", "Frågad adr", "Svarande adr", "FC", "Info", "Svarstid ms", "Status")
        widths = (55, 80, 65, 90, 95, 35, 430, 80, 95)
        self.tv = ttk.Treeview(tf, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tv.heading(c, text=h)
            self.tv.column(c, width=w, stretch=(c == "info"), anchor="w" if c in ("info", "fragad", "svarar") else "e")
        for st, (bg, fg) in COLORS.items():
            kw = {"foreground": fg}
            if bg:
                kw["background"] = bg
            self.tv.tag_configure(st, **kw)
        self.tv.tag_configure("ok_resp", foreground="#7ee787")
        self.tv.tag_configure("ok_near", foreground="#f2cc60")
        self.tv.tag_configure("ok_req", foreground="")
        self.tv.tag_configure("pair", background="#1f4e79", foreground="#ffffff")
        self.tv.tag_configure("hit", background="#555555")
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=ys.set)
        self.tv.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.tv.bind("<<TreeviewSelect>>", self._on_select)
        self.tv.bind("<Control-c>", self.copy_rows)
        pw.add(tf, weight=4)
        df = ttk.Frame(pw)
        self.txt = tk.Text(df, height=9, font=("Consolas", 9), wrap="none")
        self.txt.pack(fill="both", expand=True)
        pw.add(df, weight=1)

    # ------------------------------------------------------------ data
    def clear(self):
        self.events = EventStore()
        self.tv.delete(*self.tv.get_children())
        self.txt.delete("1.0", "end")

    def add(self, evs):
        if not evs:
            return
        base = len(self.events)
        self.events.extend(evs)
        for i, e in enumerate(evs):
            if self._match(e):
                self._insert(base + i, e)
        kids = self.tv.get_children()
        if len(kids) > MAX_ROWS:
            self.tv.delete(*kids[:len(kids) - MAX_ROWS])
        if self.paused:
            self._pending_while_paused += len(evs)
            self.btn_pause.config(text=f"▶ Fortsätt rullning (+{self._pending_while_paused})")
        elif self.tv.get_children():
            self.tv.see(self.tv.get_children()[-1])

    def toggle_pause(self):
        self.paused = not self.paused
        self._pending_while_paused = 0
        self.btn_pause.config(text="▶ Fortsätt rullning" if self.paused else "⏸ Pausa rullning")
        if not self.paused and self.tv.get_children():
            self.tv.see(self.tv.get_children()[-1])

    def _match(self, e):
        if self.errors_only.get() and M.ev_status(e) == "ok":
            return False
        if self.pred is not None:
            try:
                return bool(self.pred(e))
            except Exception:
                return False
        return True

    def _addr(self, a):
        if a is None:
            return "–"
        n = self.names().get(a)
        return f"{a} {n}" if n else str(a)

    def _row(self, idx, e):
        prev = self.events[idx - 1].t if self.events.has(idx - 1) else e.t
        st = M.ev_status(e)
        resp = M.ev_responder(e)
        asked = e.asked if e.kind not in ("junk", "collision", "gap", "info") or e.asked is not None else None
        status = M.STATUS_TEXT.get(st, st.upper())
        if st == "ok" and resp is not None and asked is not None and resp != asked:
            status = "FEL ADRESS"
        ms = "" if e.rtt is None else f"{e.rtt * 1000:.1f}"
        return (idx + 1, f"{e.t - self.t0:.4f}", f"{(e.t - prev) * 1000:.1f}", self._addr(asked),
                self._addr(resp) if e.kind != "req" else "",
                "" if e.fc is None else f"{e.fc:02X}", e.text, ms, status)

    def _tag(self, e):
        st = M.ev_status(e)
        if st != "ok":
            return st if st in COLORS else "timeout"
        if e.kind == "resp" and e.rtt is not None:
            lim = self.timeout_s() or 0.5
            if e.rtt > lim:
                return "late"
            if e.rtt > 0.7 * lim:
                return "ok_near"
            return "ok_resp"
        if e.kind == "resp" and e.asked is not None and e.slave != e.asked:
            return "feladr"
        return "ok_req"

    def _insert(self, idx, e):
        self.tv.insert("", "end", iid=str(idx), values=self._row(idx, e), tags=(self._tag(e),))

    def refilter(self):
        self.tv.delete(*self.tv.get_children())
        idxs = [i for i, e in self.events.indexed() if self._match(e)][-MAX_ROWS:]
        for i in idxs:
            self._insert(i, self.events[i])
        if self.tv.get_children() and not self.paused:
            self.tv.see(self.tv.get_children()[-1])

    def apply_filter(self):
        expr = self.ent_filter.get().strip()
        try:
            self.pred = M.parse_filter(expr) if expr else None
            self.ent_filter.configure(foreground="")
            self.on_status(f"Filter: {expr or '(inget)'}")
        except ValueError as ex:
            self.ent_filter.configure(foreground="#e5534b")
            self.on_status(f"Filterfel: {ex}")
            return
        self.refilter()

    def clear_filter(self):
        self.ent_filter.delete(0, "end")
        self.apply_filter()

    def set_filter(self, expr):
        self.ent_filter.delete(0, "end")
        self.ent_filter.insert(0, expr)
        self.apply_filter()

    # ------------------------------------------------------------ sök
    def _hit(self, e, q, qhex):
        if qhex and qhex in e.raw.hex():
            return True
        return q in e.text.lower() or q in e.raw.hex(" ")

    def search(self, step):
        q = self.ent_search.get().strip().lower()
        if not q:
            return
        qhex = q.replace(" ", "")
        if not all(c in "0123456789abcdef" for c in qhex) or len(qhex) % 2:
            qhex = ""
        kids = list(self.tv.get_children())
        if not kids:
            return
        sel = self.tv.selection()
        pos = kids.index(sel[0]) if sel and sel[0] in kids else (-1 if step > 0 else len(kids))
        n = len(kids)
        for k in range(1, n + 1):
            iid = kids[(pos + k * step) % n]
            if self.events.has(int(iid)) and self._hit(self.events[int(iid)], q, qhex):
                if not self.paused:
                    self.toggle_pause()
                self.tv.selection_set(iid)
                self.tv.see(iid)
                self.on_status(f"Träff: rad {int(iid) + 1}")
                return
        self.on_status(f"Ingen träff för '{q}'")

    # ------------------------------------------------------------ detalj/par
    def pair_of(self, idx):
        e = self.events[idx]
        if e.kind in ("req", "bcast"):
            for j in range(idx + 1, min(len(self.events), idx + 40)):
                x = self.events[j]
                if x.req is not None and bytes(x.req) == e.raw:
                    return j
                if x.kind in ("req", "bcast") and j > idx:
                    break
        elif e.req is not None:
            for j in range(idx - 1, max(self.events.base - 1, idx - 40), -1):
                x = self.events[j]
                if x.kind in ("req", "bcast") and x.raw == bytes(e.req):
                    return j
        return None

    def _on_select(self, _e=None):
        for iid in self._pair_iids:
            if self.tv.exists(iid) and self.events.has(int(iid)):
                self.tv.item(iid, tags=(self._tag(self.events[int(iid)]),))
        self._pair_iids = []
        sel = self.tv.selection()
        if not sel:
            return
        idx = int(sel[0])
        if not self.events.has(idx):
            return
        e = self.events[idx]
        j = self.pair_of(idx)
        if j is not None and self.tv.exists(str(j)):
            self.tv.item(str(j), tags=("pair",))
            self._pair_iids = [str(j)]
        self.show_detail(idx, e, j)

    def show_detail(self, idx, e, j):
        r = self._row(idx, e)
        lines = [f"Nr {idx + 1}   tid {r[1]} s   delta {r[2]} ms   status {r[8]}",
                 f"Typ: {M.KIND_TEXT.get(e.kind, e.kind)}   frågad: {r[3]}   svarande: {r[4] or '–'}"
                 + (f"   svarstid {r[7]} ms (gräns {self.timeout_s() * 1000:.0f} ms)" if r[7] else ""),
                 f"Info: {e.text}"]
        if j is not None:
            lines.append(f"Hör ihop med rad {j + 1} ({M.KIND_TEXT.get(self.events[j].kind, '')}) – markerad blå")
        reg = M.ev_regs(e)
        if reg:
            lines.append(f"Register: {reg[0]}–{reg[0] + max(1, reg[1]) - 1} ({reg[1]} st)")
        if e.raw:
            lines.append("")
            lines.append("Fält:")
            for k, v in fields(e.raw):
                lines.append(f"  {k:<14} {v}")
            lines.append("")
            lines.append(hexdump(e.raw))
        if e.req and e.kind != "req":
            lines.append("")
            lines.append("Fråga:")
            lines.append(hexdump(bytes(e.req)))
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", "\n".join(lines))

    def copy_rows(self, _e=None):
        rows = ["\t".join(str(v) for v in self.tv.item(i, "values")) for i in self.tv.selection()]
        self.tv.clipboard_clear()
        self.tv.clipboard_append("\n".join(rows))
