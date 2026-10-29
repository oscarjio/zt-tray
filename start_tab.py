"""
Startskärmen: en stor START-knapp, stor status (OK/VARNING/FEL) och klartext om vad som är fel och vilken enhet.
Avancerade vyer (paketlista, register, sändare, AI-chatt) ligger bakom 'Avancerat'.
"""

import time
import tkinter as tk
from tkinter import ttk

import agent_mode as AM
import modbus_monitor as M

CURVE = {"ok": "#3fb950", "missade": "#f85149", "sena": "#d29922", "krockar": "#a371f7", "crc": "#58a6ff"}
COL = {"OK": "#2ea043", "VARNING": "#d29922", "FEL": "#da3633", "VÄNTAR": "#57606a"}


class StartTab:
    def __init__(self, f, app_ui):
        self.f, self.ui = f, app_ui
        self.t0 = None
        self.level = None
        wrap = tk.Frame(f)
        wrap.pack(fill="both", expand=True)
        top = tk.Frame(wrap)
        top.pack(fill="x", pady=(0, 10))
        self.btn = tk.Button(top, text="▶  START", font=("Segoe UI", 22, "bold"), bg="#238636", fg="white",
                             activebackground="#2ea043", relief="flat", padx=30, pady=10, command=self.start)
        self.btn.pack(side="left")
        self.lbl_port = tk.Label(top, text="", font=("Segoe UI", 13), anchor="w", justify="left")
        self.lbl_port.pack(side="left", padx=20)
        tk.Button(top, text="Avancerat ▸", font=("Segoe UI", 12), relief="flat",
                  command=lambda: self.ui.show("modbus")).pack(side="right")
        self.banner = tk.Label(wrap, text="Tryck START", font=("Segoe UI", 30, "bold"), fg="white",
                               bg=COL["VÄNTAR"], pady=18)
        self.banner.pack(fill="x")
        self.lbl_det = tk.Label(wrap, text="Koppla in RS485-kabeln och tryck START.", font=("Segoe UI", 15),
                                justify="left", anchor="nw", wraplength=900)
        self.lbl_det.pack(fill="x", pady=(8, 6))
        body = ttk.PanedWindow(wrap, orient="horizontal")
        body.pack(fill="both", expand=True)
        left, right = tk.Frame(body), tk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)
        # --- busshälsa + kurva
        hr = tk.Frame(left)
        hr.pack(fill="x")
        tk.Label(hr, text="Busshälsa", font=("Segoe UI", 14, "bold")).pack(side="left")
        self.lbl_h = tk.Label(hr, text="–", font=("Segoe UI", 24, "bold"))
        self.lbl_h.pack(side="left", padx=12)
        self.lbl_h2 = tk.Label(hr, text="senaste minut  |  total –", font=("Segoe UI", 12))
        self.lbl_h2.pack(side="left")
        self.cv = tk.Canvas(left, height=130, bg="#0d1117", highlightthickness=0)
        self.cv.pack(fill="x", pady=(4, 0))
        leg = tk.Frame(left)
        leg.pack(fill="x")
        for k in AM.BusHealth.KEYS:
            tk.Label(leg, text="■ " + ("svar ok" if k == "ok" else k), fg=CURVE[k], font=("Segoe UI", 10)).pack(side="left", padx=4)
        # --- en rad per adress
        st = ttk.Style(f)
        st.configure("Big.Treeview", font=("Segoe UI", 12), rowheight=26)
        st.configure("Big.Treeview.Heading", font=("Segoe UI", 11, "bold"))
        cols = ("adr", "hälsa", "missade", "sena", "krockar", "orsak")
        self.tv = ttk.Treeview(left, columns=cols, show="headings", height=6, style="Big.Treeview")
        for c, w, t in zip(cols, (130, 80, 80, 70, 80, 420),
                           ("Adress", "Hälsa", "Missade", "Sena", "Krockar", "Senaste felorsak")):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="w" if c in ("adr", "orsak") else "center", stretch=(c == "orsak"))
        for lv, bg in (("FEL", "#ffd7d5"), ("VARNING", "#fff1c2"), ("OK", "#dafbe1")):
            self.tv.tag_configure(lv, background=bg)
        self.tv.pack(fill="both", expand=True, pady=(8, 0))
        # --- agentkonsol + rullande paket
        tk.Label(right, text="Agenten:", font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x")
        self.log = tk.Text(right, height=9, font=("Consolas", 11), bg="#0d1117", fg="#c9d1d9", relief="flat",
                           wrap="word", padx=8, pady=6, state="disabled")
        self.log.tag_configure("err", foreground="#f85149")
        self.log.tag_configure("ok", foreground="#3fb950")
        self.log.tag_configure("alarm", foreground="white", background="#b62324")
        self.log.pack(fill="both", expand=True)
        q = tk.Frame(right)
        q.pack(fill="x", pady=(4, 0))
        tk.Label(q, text="Fråga:", font=("Segoe UI", 11)).pack(side="left")
        self.ent = tk.Entry(q, font=("Segoe UI", 12))
        self.ent.pack(side="left", fill="x", expand=True, padx=4)
        self.ent.bind("<Return>", self._ask_agent)
        ar = tk.Frame(right)
        ar.pack(fill="x", pady=(4, 0))
        self.lbl_auto = tk.Label(ar, text="Autoläge AV", font=("Segoe UI", 11, "bold"), anchor="w")
        self.lbl_auto.pack(side="left")
        self.btn_more = tk.Button(ar, text="Kör 10 min till", font=("Segoe UI", 11), command=self._more)
        self.btn_more.pack(side="right")
        self.lbl_tok = tk.Label(right, text="", font=("Segoe UI", 9), fg="#57606a", anchor="w")
        self.lbl_tok.pack(fill="x")
        tk.Label(right, text="Paket (löpande):", font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x", pady=(6, 0))
        self.pk = tk.Text(right, height=9, font=("Consolas", 9), bg="#161b22", fg="#8b949e", relief="flat",
                          wrap="none", padx=6, state="disabled")
        self.pk.tag_configure("bad", foreground="#f85149")
        self.pk.tag_configure("good", foreground="#8b949e")
        self.pk.pack(fill="both", expand=True)
        self.health = AM.BusHealth()
        self._last_ev_t = 0.0
        self._mon_id = None
        self._hooked = False
        f.after(1500, self._auto_start)
        f.after(1000, self._tick)

    # ------------------------------------------------------------ koppling
    def _mt(self):
        return getattr(self.ui, "modbus", None)

    def _hook(self):
        mt = self._mt()
        if not self._hooked and mt is not None and getattr(mt, "console", None):
            mt.console.listeners.append(self._from_console)
            self._hooked = True

    def _from_console(self, tag, s):
        if tag in ("tool", "dim", "user"):
            return
        self.log.config(state="normal")
        self.log.insert("end", s, (tag,) if tag else ())
        self.log.see("end")
        self.log.config(state="disabled")
        if tag == "alarm":
            self.ui.show("start")

    # ------------------------------------------------------------ start
    def _candidates(self):
        infos = M.port_infos()
        good = [i for i in infos if i.get("usb") or i.get("rs485")]
        return AM.suggest_ports(good or infos)

    def _auto_start(self):
        self._hook()
        mt = self._mt()
        if mt is None or mt.cap is not None:
            return
        c = self._candidates()
        if len([1 for _l, i in c if i.get("usb") or i.get("rs485")]) == 1:
            self.start()
        elif not c:
            self.lbl_det.config(text="Ingen USB-RS485-adapter hittad. Koppla in kabeln och tryck START.")

    def start(self):
        self._hook()
        mt = self._mt()
        if mt is None:
            return
        c = self._candidates()
        if not c:
            self._set("FEL", "Ingen adapter hittad", ["Koppla in USB-RS485-kabeln (CH340/FTDI/CP210x) och tryck START."])
            return
        if len(c) == 1:
            self._go(c[0][1])
        else:
            self._ask(c)

    def _ask(self, c):
        top = tk.Toplevel(self.f)
        top.title("Välj adapter")
        top.transient(self.f.winfo_toplevel())
        tk.Label(top, text="Flera adaptrar hittades – vilken sitter på Modbus-bussen?",
                 font=("Segoe UI", 14, "bold")).pack(padx=20, pady=12)
        for lbl, info in c:
            tk.Button(top, text=lbl, font=("Segoe UI", 13), anchor="w", relief="groove", padx=12, pady=6,
                      command=lambda i=info: (top.destroy(), self._go(i))).pack(fill="x", padx=20, pady=4)
        top.grab_set()

    def _go(self, info):
        mt = self._mt()
        if mt.cap is not None:
            mt.stop()
        mt.refresh_ports(force=True, prefer=info["device"])
        mt.cb_port.set(M.port_label(info))
        self.lbl_port.config(text=f"{info['device']}  {AM.chip(info)}".strip())
        self.t0 = time.time()
        self._set("VÄNTAR", "Söker inställningar…", ["Letar baudrate, paritet och stoppbitar (lyssnar bara, sänder inget)."])
        ai = getattr(self.ui, "ai", None)
        if ai is not None and ai.key():
            ai.start_agent_mode(info, None, [], "Helhetsfelsökning av Modbus RTU-bussen (startskärmen)")
        elif mt.console:
            mt.console.start()
        else:
            mt.toggle()

    def _ask_agent(self, _e=None):
        t = self.ent.get().strip()
        mt = self._mt()
        if not t or mt is None or not getattr(mt, "console", None):
            return
        self.ent.delete(0, "end")
        mt.console.ent.delete(0, "end")
        mt.console.ent.insert(0, t)
        mt.console.submit()

    def _more(self):
        ai = getattr(self.ui, "ai", None)
        if ai is None or not ai.key():
            self._from_console("err", "AI-analys kräver en nyckel (Avancerat → AI-felsökning). "
                                      "Lokal bevakning fungerar ändå.\n")
            return
        if ai.agent is None or ai.wd is None:
            mt = self._mt()
            if mt is not None and mt.console and mt.cap is None:
                self.start()
                return
        ai.start_auto()
        if ai.wd is None:
            import agent_mode as _AM
            ai.wd = _AM.Watchdog(ai.journal)

    def _auto_label(self):
        ai = getattr(self.ui, "ai", None)
        a = getattr(ai, "auto", None) if ai is not None else None
        txt = a.status() if a else ("Autoläge AV" + ("" if ai is not None and ai.key() else " (ingen AI-nyckel – bara lokal analys)"))
        self.lbl_auto.config(text=txt, fg="#2ea043" if a and a.active() else "#57606a")
        self.btn_more.config(text=f"Kör {ai.auto_minutes() if ai else 10} min till" if a else "Starta autoläge")
        if ai is not None and getattr(ai, "agent", None) is not None and ai.agent.client:
            self.lbl_tok.config(text=ai.agent.usage_text())

    # ------------------------------------------------------------ status
    def _set(self, level, head, det):
        self.banner.config(text=head if level == "VÄNTAR" else f"{level}:  {head}", bg=COL[level])
        self.lbl_det.config(text="\n".join("• " + d for d in det[:6]))
        self.level = level

    def _update_live(self, mt, mon):
        now = time.time()
        if self._mon_id != id(mon):
            self._mon_id, self.health, self._last_ev_t = id(mon), AM.BusHealth(), 0.0
        rows = mon.stats_rows()
        self.health.sample(rows, now)
        p1, pt = AM.BusHealth.pct(self.health.last(60)), AM.BusHealth.pct(self.health.total)
        f = lambda p: "–" if p is None else f"{p:.1f} %"
        self.lbl_h.config(text=f(p1), fg="#2ea043" if (p1 or 0) >= 98 else "#d29922" if (p1 or 0) >= 90 else "#da3633")
        self.lbl_h2.config(text=f"senaste minut  |  total {f(pt)}")
        self._draw()
        names = getattr(mt, "_names_cache", {}) or {}
        sel = self.tv.get_children()
        self.tv.delete(*sel)
        for a, n, h, mi, se, kr, cause, lv in AM.addr_rows(rows, names, (mon.events[-1].t if mon.events else None)):
            self.tv.insert("", "end", values=(f"{a}" + (f"  {n}" if n else ""), f"{h:.0f} %", mi, se, kr, cause), tags=(lv,))
        evs = [e for e in list(mon.events)[-40:] if e.t > self._last_ev_t]
        if evs:
            self._last_ev_t = evs[-1].t
            self.pk.config(state="normal")
            for e in evs:
                self.pk.insert("end", e.line(getattr(mt, "t0", 0) or 0)[:140] + "\n",
                               ("bad",) if e.kind in M.ERROR_KINDS else ("good",))
            n = int(self.pk.index("end-1c").split(".")[0])
            if n > 300:
                self.pk.delete("1.0", f"{n - 300}.0")
            self.pk.see("end")
            self.pk.config(state="disabled")

    def _draw(self):
        cv = self.cv
        cv.delete("all")
        w, h = max(cv.winfo_width(), 200), max(cv.winfo_height(), 100)
        hist = list(self.health.hist)[-120:]
        if len(hist) < 2:
            cv.create_text(w // 2, h // 2, text="kurvan visas när trafik finns", fill="#8b949e")
            return
        top = max(1, max(max(d.values()) for _t, d in hist))
        dx = w / 119
        x0 = w - dx * (len(hist) - 1)
        for k in AM.BusHealth.KEYS:
            pts = []
            for i, (_t, d) in enumerate(hist):
                pts += [x0 + i * dx, h - 4 - (h - 12) * d[k] / top]
            cv.create_line(*pts, fill=CURVE[k], width=2 if k == "ok" else 1.5)
        cv.create_text(4, 4, anchor="nw", text=f"{top}/s", fill="#8b949e", font=("Segoe UI", 8))
        cv.create_text(w - 4, h - 2, anchor="se", text="senaste 2 min", fill="#8b949e", font=("Segoe UI", 8))

    def _tick(self):
        try:
            if not self.f.winfo_exists():
                return
            self._hook()
            self._auto_label()
            mt = self._mt()
            if mt is not None and self.t0 is not None:
                mon = mt.mon
                running = mt.cap is not None
                if mon is not None and running:
                    lvl, head, det = AM.assess(mon.stats_rows(), True, mon.crc_ok_frames, mon.bad_frames,
                                               sum(dict(mon.line_err_total).values()), time.time() - self.t0,
                                               getattr(mt, "_names_cache", {}), list(mon.events)[-2000:])
                    if lvl != "VÄNTAR" or self.level in (None, "VÄNTAR"):
                        self._set(lvl, head, det)
                    self._update_live(mt, mon)
        except Exception:
            pass
        self.f.after(1000, self._tick)
