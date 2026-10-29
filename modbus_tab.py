"""
Fliken Modbus i ZT-Tray: passiv Modbus RTU-busmonitor (se modbus_monitor.py).
Enkelt först: länkindikator, kända enheter (grön/gul/röd/grå), sammanfattning per adress med
orsak i klartext och "Vad hände innan stoppet". Paketvyn är Wireshark-lik (packet_view.py).
Avancerat (felfälla, jämförelse, registervy, CSV, loggar) ligger bakom knappar.
All Tk körs i UI-tråden; lästrådar matar Monitor och fliken hämtar händelser var 200 ms.
"""

import os
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import config as C
import linecheck as L
import modbus_monitor as M
import packet_view

MAX_ROWS = packet_view.MAX_ROWS
LOG_DIR = os.path.join(str(C.APP_DIR), "modbus-loggar")
NO_PORT2 = "(ingen – 2-tråd)"
DOT = {"green": "#2ea043", "yellow": "#d29922", "red": "#da3633", "grey": "#6e7681"}


def parse_names(text):
    """'1=Motor 1, 2=Motor 2, 3=Motor 3' -> {1: 'Motor 1', ...}"""
    out = {}
    for part in text.replace(";", ",").split(","):
        if "=" in part:
            a, n = part.split("=", 1)
            if a.strip().isdigit():
                out[int(a.strip())] = n.strip()
    return out


class SimCapture:
    """Spelar upp en simulerad buss i realtid in i Monitor (för test utan hårdvara)."""

    def __init__(self, mon, chunks):
        self.mon, self.chunks = mon, chunks
        self.port_name = "SIMULERING"
        self.port = None
        self.error = ""
        self.stop_ev = threading.Event()
        self.th = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.th.start()

    def _run(self):
        if not self.chunks:
            return
        base = time.perf_counter() - self.chunks[0][0]
        for t, d in self.chunks:
            while not self.stop_ev.is_set():
                now = time.perf_counter() - base
                if now >= t:
                    break
                self.mon.tick(now + self.chunks[0][0] * 0)
                time.sleep(min(0.01, t - now))
            if self.stop_ev.is_set():
                return
            self.mon.tick(t - 1e-6)
            self.mon.feed(t, d)
        end = time.perf_counter() - base
        while not self.stop_ev.is_set():   # fortsätt ticka så tystnad/timeout syns
            self.mon.tick(time.perf_counter() - base)
            time.sleep(0.05)
        _ = end

    def errors(self):
        return {}

    def stop(self):
        self.stop_ev.set()


class ModbusTab:
    def __init__(self, f, win):
        self.f = f
        self.win = win
        self.app = win.app
        mc = self.app.cfg.get("modbus") or {}
        self.mon = None
        self.cap = None
        self.t0 = 0.0
        self.clock = time.perf_counter
        self.compare_win = None
        self.auto_busy = False
        self._names_cache = {}
        self.reg_win = None
        self._reg_tick = 0
        self.stops = []
        self._stop_keys = set()
        self.stop_win = None
        self._rate = (0.0, 0, 0, 0)   # tid, bytes, ok, bad
        self._link = ("grey", "Stoppad")
        self._ports_seen = None
        self._tick = 0

        # --- rad 1: port och linje
        r1 = ttk.Frame(f)
        r1.pack(fill="x")
        ttk.Label(r1, text="Port").pack(side="left")
        self.cb_port = ttk.Combobox(r1, width=40)
        self.cb_port.pack(side="left", padx=(4, 2))
        ttk.Button(r1, text="⟳", width=3, command=lambda: self.refresh_ports(force=True)).pack(side="left", padx=(0, 8))
        ttk.Label(r1, text="Baud").pack(side="left")
        self.cb_baud = ttk.Combobox(r1, width=7, values=[str(b) for b in sorted(M.BAUDS)])
        self.cb_baud.set(str(mc.get("baud", 19200)))
        self.cb_baud.pack(side="left", padx=(4, 8))
        ttk.Label(r1, text="Paritet").pack(side="left")
        self.cb_par = ttk.Combobox(r1, width=4, state="readonly", values=["N", "E", "O"])
        self.cb_par.set(mc.get("parity", "E"))
        self.cb_par.pack(side="left", padx=(4, 8))
        ttk.Label(r1, text="Stopp").pack(side="left")
        self.cb_stop = ttk.Combobox(r1, width=3, state="readonly", values=["1", "2"])
        self.cb_stop.set(str(mc.get("stop", 1)))
        self.cb_stop.pack(side="left", padx=(4, 8))
        ttk.Label(r1, text="Timeout ms").pack(side="left")
        self.sp_to = ttk.Spinbox(r1, from_=20, to=5000, increment=10, width=6, command=self._to_changed)
        self.sp_to.set(mc.get("timeout_ms", 500))
        self.sp_to.bind("<Return>", lambda e: self._to_changed())
        self.sp_to.bind("<FocusOut>", lambda e: self._to_changed())
        self.sp_to.pack(side="left", padx=(4, 8))
        self.btn_start = ttk.Button(r1, text="Starta", style="Accent.TButton", command=self.toggle)
        self.btn_start.pack(side="right")
        ttk.Button(r1, text="Simulera", command=self.simulate).pack(side="right", padx=4)
        self.btn_auto = ttk.Button(r1, text="Auto-sök…", command=self.autodetect)
        self.btn_auto.pack(side="right")

        r1b = ttk.Frame(f)
        r1b.pack(fill="x", pady=(4, 0))
        ttk.Label(r1b, text="2:a port (4-tråd, andra paret)").pack(side="left")
        self.cb_port2 = ttk.Combobox(r1b, width=40)
        self.cb_port2.pack(side="left", padx=(4, 8))
        ttk.Label(r1b, text="Kända enheter").pack(side="left")
        self.ent_names = ttk.Entry(r1b)
        self.ent_names.insert(0, mc.get("names", "1=Motor 1, 2=Motor 2, 3=Motor 3"))
        self.ent_names.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.ent_names.bind("<FocusOut>", lambda e: self.save_cfg())
        ttk.Label(f, text="Lyssnar bara, sänder aldrig. Kända enheter: adress=namn, kommaseparerat (sparas). "
                  "4-trådsbuss (t.ex. SW1A4080): välj en adapter per par för att se både frågor och svar.",
                  style="Sub.TLabel").pack(anchor="w")

        # --- länkindikator + enhetsstatus
        lk = tk.Frame(f)
        lk.pack(fill="x", pady=(6, 2))
        self.lbl_link = tk.Label(lk, text="● Stoppad", anchor="w", font=("Segoe UI", 11, "bold"),
                                 fg="white", bg=DOT["grey"], padx=8, pady=3)
        self.lbl_link.pack(fill="x")
        self.dev_frame = ttk.Frame(f)
        self.dev_frame.pack(fill="x", pady=(2, 2))
        self.lbl_missed = ttk.Label(f, text="", font=("Segoe UI", 10, "bold"))
        self.lbl_missed.pack(anchor="w")
        self.stop_bar = tk.Frame(f)
        self.lbl_stop = tk.Label(self.stop_bar, text="", anchor="w", fg="white", bg="#8b1a1a",
                                 font=("Segoe UI", 10, "bold"), padx=8, pady=3)
        self.lbl_stop.pack(side="left", fill="x", expand=True)
        tk.Button(self.stop_bar, text="Visa vad som hände innan", command=self.open_stops).pack(side="right")

        # --- sammanfattning per adress
        self.lbl_info = ttk.Label(f, text="Stoppad", style="Sub.TLabel")
        self.lbl_info.pack(anchor="w", pady=(4, 2))
        cols = ("slav", "namn", "fragor", "svar", "miss", "missp", "sena", "krock", "uart", "max", "orsak")
        heads = ("Adress", "Namn", "Frågor", "Svar", "Missade", "Miss %", "Sena", "Krockar", "UART-fel", "Max ms",
                 "Orsak / senaste fel")
        widths = (55, 120, 60, 60, 60, 55, 45, 55, 60, 60, 420)
        sf = ttk.Frame(f)
        sf.pack(fill="x")
        self.tv_stats = ttk.Treeview(sf, columns=cols, show="headings", height=5)
        for c, h, w in zip(cols, heads, widths):
            self.tv_stats.heading(c, text=h)
            self.tv_stats.column(c, width=w, stretch=(c == "orsak"), anchor="w" if c in ("namn", "orsak") else "e")
        self.tv_stats.tag_configure("err", foreground="#ff7b72")
        self.tv_stats.tag_configure("warn", foreground="#e3b341")
        self.tv_stats.tag_configure("ok", foreground="#7ee787")
        self.tv_stats.pack(fill="x")
        self.tv_stats.bind("<Double-1>", self._stats_dbl)

        sb = ttk.Frame(f)
        sb.pack(fill="x", pady=(4, 0))
        ttk.Button(sb, text="Vad hände innan stoppet…", command=self.open_stops).pack(side="left")
        ttk.Button(sb, text="Nollställ", command=self.reset).pack(side="left", padx=6)
        ttk.Button(sb, text="Spara logg…", command=self.save_log).pack(side="left")
        ttk.Button(sb, text="Öppna logg…", command=self.replay_file).pack(side="left", padx=6)
        self.btn_adv = ttk.Button(sb, text="Avancerat ▸", command=self.toggle_adv)
        self.btn_adv.pack(side="right")

        # --- avancerat (dolt)
        self.adv = ttk.Frame(f)
        r2 = ttk.Frame(self.adv)
        r2.pack(fill="x", pady=(4, 0))
        ttk.Label(r2, text="Felfälla för adress").pack(side="left")
        self.ent_trap = ttk.Entry(r2, width=8)
        self.ent_trap.insert(0, mc.get("trap", ""))
        self.ent_trap.pack(side="left", padx=(4, 4))
        ttk.Label(r2, text="före/efter s").pack(side="left")
        self.sp_pre = ttk.Spinbox(r2, from_=1, to=110, width=4)
        self.sp_pre.set(mc.get("trap_pre", 10))
        self.sp_pre.pack(side="left", padx=2)
        self.sp_post = ttk.Spinbox(r2, from_=1, to=60, width=4)
        self.sp_post.set(mc.get("trap_post", 3))
        self.sp_post.pack(side="left", padx=(2, 10))
        self.v_log = tk.BooleanVar(value=bool(mc.get("log", True)))
        ttk.Checkbutton(r2, text="Logga rådata automatiskt", variable=self.v_log).pack(side="left")
        r2c = ttk.Frame(self.adv)
        r2c.pack(fill="x", pady=(4, 0))
        ttk.Button(r2c, text="Jämför markerade sida vid sida", command=self.open_compare).pack(side="left")
        ttk.Button(r2c, text="Registervy…", command=self.open_regs).pack(side="left", padx=6)
        ttk.Button(r2c, text="Exportera CSV…", command=self.export_csv).pack(side="left")
        ttk.Button(r2c, text="Öppna loggmapp", command=self.open_dir).pack(side="left", padx=6)

        # --- paketvy
        self.pv_frame = ttk.Frame(f)
        self.pv_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.pv = packet_view.PacketView(self.pv_frame, names=lambda: self._names_cache,
                                         timeout_s=self.timeout_s, on_status=self._status_msg)
        self._status_txt = ""
        self.refresh_ports(prefer=mc.get("port", ""), prefer2=mc.get("port2", ""))
        self._names_cache = self.names()
        self._draw_devices([])
        self._poll()

    # ------------------------------------------------------------ hjälp
    def names(self):
        return parse_names(self.ent_names.get())

    def timeout_s(self):
        try:
            return max(0.02, float(self.sp_to.get()) / 1000)
        except ValueError:
            return 0.5

    def _to_changed(self):
        if self.mon:
            self.mon.resp_timeout = self.timeout_s()
        self.pv.refilter()

    def _status_msg(self, s):
        self._status_txt = s
        self._status_until = time.time() + 4

    def line(self):
        return (int(self.cb_baud.get()), self.cb_par.get() or "E", int(self.cb_stop.get() or 1))

    def trap_slaves(self):
        return [int(x) for x in self.ent_trap.get().replace(";", ",").split(",") if x.strip().isdigit()]

    def refresh_ports(self, force=False, prefer=None, prefer2=None):
        infos = M.port_infos()
        devs = [i["device"] for i in infos]
        if not force and devs == self._ports_seen:
            return
        new = [d for d in devs if self._ports_seen is not None and d not in self._ports_seen]
        gone = [d for d in (self._ports_seen or []) if d not in devs]
        self._ports_seen = devs
        labels = [M.port_label(i) for i in infos]
        self.cb_port.configure(values=labels)
        self.cb_port2.configure(values=[NO_PORT2] + labels)
        if self.cap:
            return
        cur = M.port_name(self.cb_port.get()) if prefer is None else prefer
        best = None
        if new:   # nyinkopplad adapter vinner
            best = M.pick_port([i for i in infos if i["device"] in new]) or None
        best = best or M.pick_port(infos, cur)
        self.cb_port.set(M.port_label(best) if best else "")
        cur2 = M.port_name(self.cb_port2.get()) if prefer2 is None else prefer2
        m2 = [i for i in infos if i["device"].lower() == (cur2 or "").lower()]
        self.cb_port2.set(M.port_label(m2[0]) if m2 else NO_PORT2)
        if new or gone:
            self._status_msg(("Ny port: " + ", ".join(new) if new else "") + ("  Borttagen: " + ", ".join(gone) if gone else ""))

    def save_cfg(self):
        def upd(cfg):
            p2 = self.cb_port2.get()
            cfg["modbus"] = {
                "port": M.port_name(self.cb_port.get()), "port2": "" if p2 == NO_PORT2 else M.port_name(p2),
                "baud": int(self.cb_baud.get()), "parity": self.cb_par.get(), "stop": int(self.cb_stop.get()),
                "timeout_ms": int(float(self.sp_to.get())), "trap": self.ent_trap.get().strip(),
                "trap_pre": float(self.sp_pre.get()), "trap_post": float(self.sp_post.get()),
                "log": bool(self.v_log.get()), "names": self.ent_names.get().strip()}
        try:
            self.app.update_cfg(upd)
        except Exception as e:
            C.log(f"modbus: kunde inte spara inställningar: {e!r}")

    def new_monitor(self, baud, par, stop, hdr):
        mon = M.Monitor(baud, par, stop, resp_timeout=self.timeout_s())
        traps = self.trap_slaves()
        if traps:
            mon.trap = M.ErrorTrap(traps, float(self.sp_pre.get()), float(self.sp_post.get()), LOG_DIR, hdr)
        return mon

    def toggle_adv(self):
        if self.adv.winfo_ismapped():
            self.adv.pack_forget()
            self.btn_adv.config(text="Avancerat ▸")
        else:
            self.adv.pack(fill="x", before=self.pv_frame)
            self.btn_adv.config(text="Avancerat ▾")

    def _stats_dbl(self, _e=None):
        sel = self.tv_stats.selection()
        if sel:
            self.pv.set_filter(f"addr=={self.tv_stats.item(sel[0], 'values')[0]}")

    def _begin(self, mon, cap, t0, clock=time.perf_counter):
        self.mon, self.cap, self.t0, self.clock = mon, cap, t0, clock
        self.pv.clear()
        self.pv.t0 = t0
        self.stops, self._stop_keys = [], set()
        self.stop_bar.pack_forget()
        self._rate = (clock(), 0, 0, 0)

    # ------------------------------------------------------------ start/stopp
    def toggle(self):
        if self.cap:
            self.stop()
            return
        try:
            baud, par, stop = self.line()
        except ValueError:
            messagebox.showerror("Modbus", "Ogiltig baud.", parent=self.f)
            return
        port = M.port_name(self.cb_port.get())
        if not port:
            messagebox.showerror("Modbus", "Ingen COM-port vald. Koppla in USB-RS485-adaptern och tryck ⟳.\n"
                                 "Syns den inte: installera drivrutin (FTDI/CH340/CP210x).", parent=self.f)
            return
        p2 = self.cb_port2.get()
        ports = [port] + ([M.port_name(p2)] if p2 and p2 != NO_PORT2 and M.port_name(p2) != port else [])
        self.save_cfg()
        hdr = M.make_header(" + ".join(ports), baud, par, stop)
        mon = self.new_monitor(baud, par, stop, hdr)
        log_path = None
        if self.v_log.get():
            os.makedirs(LOG_DIR, exist_ok=True)
            log_path = os.path.join(LOG_DIR, time.strftime("bus_%Y%m%d-%H%M%S.zmlog"))
        cap = (M.MultiCapture(mon, ports, baud, par, stop, log_path=log_path) if len(ports) > 1
               else M.Capture(mon, port, baud, par, stop, log_path=log_path))
        try:
            cap.start()
        except Exception as e:
            messagebox.showerror("Modbus", f"Kan inte öppna {' + '.join(ports)}:\n{e}\n\n"
                                 "Upptagen av annat program (t.ex. Seriemonitor-fliken)?", parent=self.f)
            return
        self._begin(mon, cap, time.perf_counter())
        self.log_path = log_path
        self.btn_start.config(text="Stoppa")
        C.log(f"modbus: lyssnar på {' + '.join(ports)} {baud} 8{par}{stop}")

    def simulate(self):
        if self.cap:
            self.stop()
        baud, par, stop = 19200, "E", 1
        chunks = M.simulate(40.0, baud, par, stop, faults=M.DEMO_FAULTS)
        mon = self.new_monitor(baud, par, stop, M.make_header("SIM", baud, par, stop))
        cap = SimCapture(mon, chunks)
        start = time.perf_counter()
        base = start - chunks[0][0]
        self._begin(mon, cap, chunks[0][0], clock=lambda: time.perf_counter() - base)
        cap.start()
        self.btn_start.config(text="Stoppa")
        self._status_msg("Simulering (40 s, med inbyggda fel: timeout, CRC, sena svar, krock, stopp av adress 5)")

    def stop(self):
        cap, self.cap = self.cap, None
        if cap:
            cap.stop()
        self.btn_start.config(text="Starta")

    def reset(self):
        if self.mon:
            self.mon.reset_stats()
        self.tv_stats.delete(*self.tv_stats.get_children())
        self.stops, self._stop_keys = [], set()
        self.stop_bar.pack_forget()

    # ------------------------------------------------------------ auto
    def autodetect(self):
        if self.cap or self.auto_busy:
            messagebox.showinfo("Modbus", "Stoppa lyssningen först.", parent=self.f)
            return
        port = M.port_name(self.cb_port.get())
        self.auto_busy = True
        self.btn_auto.config(text="Söker…")
        state = {"txt": ""}

        def prog(i, n, b, p, s):
            state["txt"] = f"Auto-sök {i + 1}/{n}: {b} 8{p}{s}"

        def work():
            try:
                res = L.live_scan(port, 1.5, progress=prog)
                err = ""
            except Exception as e:
                res, err = [], str(e)
            self.f.after(0, lambda: self._auto_done(res, err))

        def show():
            if self.auto_busy:
                self.lbl_info.config(text=state["txt"])
                self.f.after(300, show)
        threading.Thread(target=work, daemon=True).start()
        show()

    def _auto_done(self, res, err):
        self.auto_busy = False
        self.btn_auto.config(text="Auto-sök…")
        if err or not res:
            messagebox.showerror("Modbus", f"Auto-sök misslyckades: {err}", parent=self.f)
            return
        top = tk.Toplevel(self.f)
        top.title("Auto-sök: poäng per inställning")
        tv = ttk.Treeview(top, columns=("l", "fr", "bad", "u", "p"), show="headings", height=12)
        for c, h in zip(("l", "fr", "bad", "u", "p"), ("Inställning", "Giltiga ramar", "Skräp/CRC", "UART-fel", "Poäng")):
            tv.heading(c, text=h)
            tv.column(c, width=110, anchor="e")
        for r in res:
            tv.insert("", "end", values=(f"{r['baud']} 8{r['parity']}{r['stop']}", r["frames"], r["bad"],
                                         r["uart_err"], r["score"]))
        tv.pack(fill="both", expand=True, padx=10, pady=10)
        best = res[0]
        d = L.diagnose(res, current=self.line(), stats_rows=self.mon.stats_rows() if self.mon else None)
        msg = L.report_text(d)
        col = "#2ea043" if d["confidence"] >= 80 else ("#d29922" if d["best"] else "#da3633")
        tk.Label(top, text=f"Konfidens {d['confidence']} %", fg="white", bg=col,
                 font=("Segoe UI", 10, "bold"), padx=6).pack(padx=10, anchor="w")
        ttk.Label(top, text=msg, wraplength=640, justify="left").pack(padx=10, anchor="w")
        best = d["best"] or best
        def use():
            self.cb_baud.set(str(best["baud"]))
            self.cb_par.set(best["parity"])
            self.cb_stop.set(str(best["stop"]))
            top.destroy()
        b = ttk.Frame(top)
        b.pack(pady=10)
        if d["best"]:
            ttk.Button(b, text="Använd bästa", style="Accent.TButton", command=use).pack(side="left", padx=4)
        ttk.Button(b, text="Stäng", command=top.destroy).pack(side="left", padx=4)
        self.lbl_info.config(text=msg.splitlines()[0] if msg else "")

    # ------------------------------------------------------------ loggar
    def replay_file(self):
        if self.cap:
            messagebox.showinfo("Modbus", "Stoppa lyssningen först.", parent=self.f)
            return
        p = filedialog.askopenfilename(parent=self.f, initialdir=LOG_DIR if os.path.isdir(LOG_DIR) else None,
                                       filetypes=[("Modbus-logg", "*.zmlog"), ("Alla", "*.*")])
        if not p:
            return
        st, chunks = M.read_log(p)
        baud = int(st.get("baud", self.cb_baud.get()))
        par = st.get("parity", self.cb_par.get())
        stop = int(float(st.get("stop", self.cb_stop.get())))
        self.cb_baud.set(str(baud))
        self.cb_par.set(par)
        self.cb_stop.set(str(stop))
        mon = M.Monitor(baud, par, stop, resp_timeout=self.timeout_s())
        t_end = chunks[-1][0] if chunks else 0.0
        self._begin(mon, None, chunks[0][0] if chunks else 0.0, clock=lambda: t_end + 10)
        M.replay(chunks, mon)
        self._rate = (chunks[0][0] if chunks else 0.0, 0, 0, 0)
        self._status_msg(f"Uppspelad: {os.path.basename(p)}  ({len(chunks)} block)")

    def save_log(self):
        if not self.mon:
            return
        p = filedialog.asksaveasfilename(parent=self.f, defaultextension=".zmlog",
                                         initialdir=LOG_DIR if os.path.isdir(LOG_DIR) else None,
                                         filetypes=[("Modbus-logg", "*.zmlog")],
                                         initialfile=time.strftime("bus_%Y%m%d-%H%M%S.zmlog"))
        if not p:
            return
        with self.mon.lock:
            chunks = list(self.mon.raw)
        M.write_log(p, chunks, M.make_header(getattr(self.cap, "port_name", "logg"), self.mon.baud,
                                             self.mon.parity, self.mon.stop))
        self._status_msg(f"Sparade {len(chunks)} block (senaste {int(self.mon.raw_keep_s)} s) till {p}")

    def export_csv(self):
        if not self.mon:
            return
        p = filedialog.asksaveasfilename(parent=self.f, defaultextension=".csv",
                                         filetypes=[("CSV", "*.csv")], initialfile="modbus.csv")
        if p:
            M.export_csv(list(self.pv.events), p, self.t0)

    def open_dir(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        os.startfile(LOG_DIR)

    # ------------------------------------------------------------ stopp-analys
    def _check_stops(self):
        evs = self.pv.events[-20000:]
        if not evs:
            return
        quiet = 2.0
        silent = self.clock() - evs[-1].t > quiet
        found = M.find_stops(evs, quiet=quiet, n=30, at_end=silent)
        added = False
        for s in found:
            key = (s["typ"].split(" ")[0], s["addr"], round(s["t"], 1))
            if key in self._stop_keys:
                continue
            self._stop_keys.add(key)
            self.stops.append(s)
            added = True
        if added:
            s = self.stops[-1]
            nm = self._names_cache.get(s["first_fail"], "")
            self.lbl_stop.config(text=f"⚠ STOPP: {s['typ']} vid {s['t'] - self.t0:.1f} s – först felande adress: "
                                      f"{s['first_fail']} {nm}   ({len(self.stops)} st)")
            if not self.stop_bar.winfo_ismapped():
                self.stop_bar.pack(fill="x", pady=(2, 2), before=self.lbl_info)
            if len(self.stops) == 1 and self.stop_win is None:
                self.open_stops()
            elif self.stop_win is not None:
                self._fill_stops()

    def open_stops(self):
        if self.stop_win is not None:
            self.stop_win.lift()
            self._fill_stops()
            return
        top = tk.Toplevel(self.f)
        top.title("Vad hände innan stoppet")
        top.geometry("1100x520")
        self.stop_win = top
        left = ttk.Frame(top)
        left.pack(side="left", fill="y", padx=6, pady=6)
        ttk.Label(left, text="Stopp (senaste överst)").pack(anchor="w")
        self.lb_stops = tk.Listbox(left, width=48)
        self.lb_stops.pack(fill="y", expand=True)
        self.lb_stops.bind("<<ListboxSelect>>", lambda e: self._show_stop())
        right = ttk.Frame(top)
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.lbl_stop_head = ttk.Label(right, text="", font=("Segoe UI", 10, "bold"), wraplength=700)
        self.lbl_stop_head.pack(anchor="w")
        self.txt_stop = tk.Text(right, font=("Consolas", 9), wrap="none")
        self.txt_stop.pack(fill="both", expand=True)
        self.txt_stop.tag_configure("err", foreground="#ff7b72")
        self.txt_stop.tag_configure("warn", foreground="#e3b341")
        self.txt_stop.tag_configure("first", background="#8b1a1a", foreground="white")

        def closed():
            self.stop_win = None
            top.destroy()
        top.protocol("WM_DELETE_WINDOW", closed)
        self._fill_stops()

    def _fill_stops(self):
        if self.stop_win is None:
            return
        self.lb_stops.delete(0, "end")
        self._stop_order = list(reversed(self.stops))
        for s in self._stop_order:
            self.lb_stops.insert("end", f"{s['t'] - self.t0:8.1f} s  {s['typ']}  (först: {s['first_fail']})")
        if self._stop_order:
            self.lb_stops.selection_set(0)
            self._show_stop()
        else:
            self.lbl_stop_head.config(text="Inget stopp upptäckt ännu (> 2 s tystnad efter fel, eller att PLC:n "
                                           "slutar fråga en adress som felat).")

    def _show_stop(self):
        sel = self.lb_stops.curselection()
        if not sel:
            return
        s = self._stop_order[sel[0]]
        nm = self._names_cache.get(s["first_fail"], "")
        self.lbl_stop_head.config(text=f"{s['typ']} vid {s['t'] - self.t0:.3f} s.  Först felande adress: "
                                       f"{s['first_fail']} {nm}.  Senaste {len(s['before'])} paket före:")
        self.txt_stop.delete("1.0", "end")
        first_done = False
        for e in s["before"]:
            tag = ""
            if e.kind in M.ERROR_KINDS:
                tag = "warn" if e.kind in ("exc", "late") else "err"
                if not first_done:
                    tag, first_done = "first", True
            self.txt_stop.insert("end", e.line(self.t0) + "\n", tag)

    # ------------------------------------------------------------ status
    def _draw_devices(self, rows):
        for w in self.dev_frame.winfo_children():
            w.destroy()
        now = self.clock() if self.mon else None
        states = M.device_states(rows, self._names_cache, now, quiet=max(3.0, 5 * self.timeout_s()))
        if not states:
            ttk.Label(self.dev_frame, text="Ange kända enheter ovan (t.ex. 1=Motor 1, 2=Motor 2, 3=Motor 3).",
                      style="Sub.TLabel").pack(anchor="w")
            return
        for a, name, col, text in states:
            row = tk.Frame(self.dev_frame)
            row.pack(fill="x")
            tk.Label(row, text="  ", bg=DOT[col], width=2).pack(side="left", padx=(0, 6), pady=1)
            tk.Label(row, text=f"{a:>3}  {name:<18}", font=("Consolas", 10, "bold"), anchor="w").pack(side="left")
            tk.Label(row, text=text, anchor="w", fg=DOT[col] if col != "grey" else "#8b949e").pack(side="left")

    def _update_link(self):
        now = self.clock()
        t, b, ok, bad = self._rate
        dt = now - t
        if dt < 1.0:
            return
        db = self.mon.bytes_total - b
        dok = self.mon.crc_ok_frames - ok
        dbad = self.mon.bad_frames - bad
        if self.cap is None:   # uppspelad logg: totalt
            db, dok, dbad, dt = self.mon.bytes_total, self.mon.crc_ok_frames, self.mon.bad_frames, 1.0
        self._rate = (now, self.mon.bytes_total, self.mon.crc_ok_frames, self.mon.bad_frames)
        col, txt = M.link_state(db / dt if self.cap else 0, dok, dbad)
        if self.cap is None:
            txt += "  (uppspelad logg)"
        self._link = (col, txt)
        self.lbl_link.config(text="● " + txt, bg=DOT[col])

    # ------------------------------------------------------------ uppdatering
    def _poll(self):
        try:
            if not self.f.winfo_exists():
                self.stop()
                return
        except Exception:
            return
        try:
            self._update()
        except Exception as e:
            C.log(f"modbus-flik: {e!r}")
        self.f.after(200, self._poll)

    def _update(self):
        self._tick += 1
        if not self.cap and self._tick % 10 == 0:
            self.refresh_ports()
        if self.cap and not self.cap.th.is_alive():
            err = self.cap.error
            self.stop()
            if err:
                messagebox.showerror("Modbus", f"Porten stängdes: {err}", parent=self.f)
        if self._tick % 5 == 0:
            self._names_cache = self.names()
        if not self.mon:
            return
        evs = self.mon.drain()
        if evs:
            self.pv.add(evs)
        self._update_link()
        rows = self.mon.stats_rows()
        names = self._names_cache
        sel = {self.tv_stats.item(i, "values")[0] for i in self.tv_stats.selection()}
        self.tv_stats.delete(*self.tv_stats.get_children())
        f = lambda v: "-" if v is None else f"{v:.1f}"
        miss_parts = []
        for r in rows:
            cause = M.cause_text(r)
            bad = r["missade"] or r["crc"] or r.get("feladr") or r.get("krockar")
            tag = "err" if bad else ("warn" if r["exception"] or r.get("sena") else "ok")
            orsak = cause if cause != "OK" else "OK"
            if r["senaste_fel"] and cause != "OK":
                orsak += f"   |  senast: {r['senaste_fel']}"
            iid = self.tv_stats.insert("", "end", values=(
                r["slav"], names.get(r["slav"], ""), r["frågor"], r["svar"], r["missade"],
                f"{r['missade_pct']:.1f}", r.get("sena", 0), r.get("krockar", 0),
                r.get("linjefel", 0), f(r.get("max_alla") or r["max"]),
                orsak), tags=(tag,))
            if str(r["slav"]) in sel:
                self.tv_stats.selection_add(iid)
            if r["missade"]:
                miss_parts.append(f"{r['slav']}{(' ' + names[r['slav']]) if r['slav'] in names else ''}: {r['missade']}")
        self.lbl_missed.config(text=("Missade svar per adress →  " + "   ".join(miss_parts)) if miss_parts
                               else "Missade svar: inga", foreground="#ff7b72" if miss_parts else "#7ee787")
        if self._tick % 5 == 0 or evs:
            self._draw_devices(rows)
        if self._tick % 5 == 0:
            self._check_stops()
        info = f"{self.mon.bytes_total} byte, {self.mon.frames_total} ramar"
        if self.cap:
            info = f"Lyssnar på {self.cap.port_name} {self.mon.baud} 8{self.mon.parity}{self.mon.stop}  ·  " + info
            try:
                errs = self.cap.errors() if hasattr(self.cap, "errors") else (self.cap.port.errors() if self.cap.port else {})
            except Exception:
                errs = {}
            if errs and any(errs.values()):
                info += (f"  ·  UART-fel: ram {errs.get('frame', 0)} paritet {errs.get('parity', 0)} "
                         f"överskridning {errs.get('overrun', 0)}")
        if self.mon.trap and self.mon.trap.saved:
            info += f"  ·  felfälla: {len(self.mon.trap.saved)} fil(er)"
        if self._status_txt and time.time() < getattr(self, "_status_until", 0):
            info += "  ·  " + self._status_txt
        if not self.auto_busy:
            self.lbl_info.config(text=info)
        if self.compare_win is not None:
            self._update_compare(rows)
        self._reg_tick += 1
        if self.reg_win is not None and self._reg_tick % 5 == 0 and self.v_reg_live.get():
            self._update_regs()


    # ------------------------------------------------------------ jämförelse
    def open_compare(self):
        sel = [int(self.tv_stats.item(i, "values")[0]) for i in self.tv_stats.selection()]
        if len(sel) < 2:
            names = self.names()
            sel = [a for a in names if "sw1a" in names[a].lower()] or sel
        if len(sel) < 2:
            messagebox.showinfo("Modbus", "Markera minst två slavar i tabellen (Ctrl+klick).", parent=self.f)
            return
        if self.compare_win is not None:
            try:
                self.compare_win.destroy()
            except Exception:
                pass
        top = self.compare_win = tk.Toplevel(self.f)
        top.title("Jämför slavar")
        self.compare_slaves = sel
        names = self.names()
        cols = ["mått"] + [str(a) for a in sel]
        tv = self.compare_tv = ttk.Treeview(top, columns=cols, show="headings", height=12)
        tv.heading("mått", text="")
        tv.column("mått", width=150, anchor="w")
        for a in sel:
            tv.heading(str(a), text=f"Slav {a} {names.get(a, '')}".strip())
            tv.column(str(a), width=150, anchor="e")
        tv.pack(fill="both", expand=True, padx=10, pady=10)

        def closed():
            self.compare_win = None
            top.destroy()
        top.protocol("WM_DELETE_WINDOW", closed)
        if self.mon:
            self._update_compare(self.mon.stats_rows())

    def _update_compare(self, rows):
        try:
            if not self.compare_win.winfo_exists():
                self.compare_win = None
                return
        except Exception:
            self.compare_win = None
            return
        by = {r["slav"]: r for r in rows}
        keys = [("frågor", "Frågor"), ("svar", "Svar"), ("timeout", "Timeouts"), ("crc", "CRC-fel"),
                ("exception", "Exceptions"), ("övrigt", "Övriga fel"), ("felprocent", "Fel %"),
                ("min", "Svarstid min ms"), ("medel", "Svarstid medel ms"), ("max", "Svarstid max ms"),
                ("p99", "Svarstid p99 ms"), ("senaste_fel", "Senaste fel")]
        tv = self.compare_tv
        tv.delete(*tv.get_children())
        for k, name in keys:
            vals = []
            for a in self.compare_slaves:
                v = by.get(a, {}).get(k)
                vals.append("-" if v in (None, "") else (f"{v:.2f}" if isinstance(v, float) else str(v)))
            tv.insert("", "end", values=[name] + vals)

    # ------------------------------------------------------------ registervy
    def open_regs(self):
        if self.reg_win is not None:
            try:
                self.reg_win.lift()
                return
            except Exception:
                pass
        top = self.reg_win = tk.Toplevel(self.f)
        top.title("Registervy – vad PLC:n läser och skriver")
        top.geometry("1000x600")
        r = ttk.Frame(top)
        r.pack(fill="x", padx=10, pady=(10, 4))
        self.v_reg_mode = tk.StringVar(value="en")
        ttk.Radiobutton(r, text="En slav", variable=self.v_reg_mode, value="en",
                        command=self._update_regs).pack(side="left")
        self.cb_reg_slave = ttk.Combobox(r, width=8, values=["Alla"], state="readonly")
        self.cb_reg_slave.set("Alla")
        self.cb_reg_slave.pack(side="left", padx=(4, 14))
        ttk.Radiobutton(r, text="Jämför", variable=self.v_reg_mode, value="jmf",
                        command=self._update_regs).pack(side="left")
        self.cb_reg_a = ttk.Combobox(r, width=6)
        self.cb_reg_b = ttk.Combobox(r, width=6)
        names = self.names()
        sw = [a for a in names if "sw1a" in names[a].lower()]
        self.cb_reg_a.set(str(sw[0]) if len(sw) > 0 else "4")
        self.cb_reg_b.set(str(sw[1]) if len(sw) > 1 else "5")
        self.cb_reg_a.pack(side="left", padx=4)
        ttk.Label(r, text="mot").pack(side="left")
        self.cb_reg_b.pack(side="left", padx=4)
        self.v_reg_diff = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text="Bara avvikelser", variable=self.v_reg_diff,
                        command=self._update_regs).pack(side="left", padx=10)
        self.v_reg_live = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="Uppdatera live", variable=self.v_reg_live).pack(side="left")
        ttk.Button(r, text="Exportera CSV…", command=self.export_regs).pack(side="right")
        for w in (self.cb_reg_slave, self.cb_reg_a, self.cb_reg_b):
            w.bind("<<ComboboxSelected>>", lambda e: self._update_regs())
            w.bind("<Return>", lambda e: self._update_regs())
        ttk.Label(top, style="Sub.TLabel", text=(
            "Färger i jämförelse: gul = olika värden, röd = exception/inget svar, blå = bara den ena slaven "
            "frågas/svarar. Markera en rad för ändringshistorik.")).pack(anchor="w", padx=10)
        tf = ttk.Frame(top)
        tf.pack(fill="both", expand=True, padx=10, pady=4)
        self.reg_tv = ttk.Treeview(tf, show="headings")
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.reg_tv.yview)
        self.reg_tv.configure(yscrollcommand=ys.set)
        self.reg_tv.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.reg_tv.tag_configure("diff", background="#4d3b10", foreground="#ffe8a3")
        self.reg_tv.tag_configure("err", background="#5a1e1e", foreground="#ffd7d5")
        self.reg_tv.tag_configure("one", background="#1d3557", foreground="#cfe3ff")
        self.reg_tv.bind("<<TreeviewSelect>>", lambda e: self._reg_hist())
        self.reg_hist = ttk.Label(top, text="", style="Sub.TLabel", wraplength=960, justify="left")
        self.reg_hist.pack(fill="x", padx=10, pady=(0, 10))

        def closed():
            self.reg_win = None
            top.destroy()
        top.protocol("WM_DELETE_WINDOW", closed)
        self._update_regs()

    def _reg_cols(self, cols, heads, widths):
        if tuple(self.reg_tv["columns"]) != tuple(cols):
            self.reg_tv.configure(columns=cols)
            for c, h, w in zip(cols, heads, widths):
                self.reg_tv.heading(c, text=h)
                self.reg_tv.column(c, width=w, anchor="w", stretch=c in ("fel", "hist", "afel"))

    def _ab(self):
        try:
            return int(self.cb_reg_a.get().split()[0]), int(self.cb_reg_b.get().split()[0])
        except (ValueError, IndexError):
            return None

    def _update_regs(self):
        if self.reg_win is None or not self.mon:
            return
        try:
            if not self.reg_win.winfo_exists():
                self.reg_win = None
                return
        except Exception:
            self.reg_win = None
            return
        rm = self.mon.regmap
        names = self.names()
        with self.mon.lock:
            slaves = rm.slaves()
            self.cb_reg_slave.configure(values=["Alla"] + [str(a) for a in slaves])
            for cb in (self.cb_reg_a, self.cb_reg_b):
                cb.configure(values=[str(a) for a in slaves])
            sel = self.reg_tv.selection()
            sel_key = self.reg_tv.item(sel[0], "values")[:3] if sel else None
            self.reg_tv.delete(*self.reg_tv.get_children())
            if self.v_reg_mode.get() == "jmf":
                ab = self._ab()
                if not ab:
                    return
                a, b = ab
                self._reg_cols(("tab", "adr", "a", "b", "status", "afel"),
                               ("Tabell", "Adress", f"Slav {a} {names.get(a, '')}", f"Slav {b} {names.get(b, '')}",
                                "Status", "Senaste fel"), (120, 60, 170, 170, 80, 300))
                for r in rm.compare(a, b):
                    st = r["status"]
                    if self.v_reg_diff.get() and st == "lika":
                        continue
                    tag = {"lika": "", "OLIKA": "diff"}.get(st, "err" if st.startswith("FEL") else "one")
                    err = "; ".join(x for x in (f"{a}: {r['a_fel']}" if r["a_fel"] else "",
                                                 f"{b}: {r['b_fel']}" if r["b_fel"] else "") if x)
                    self.reg_tv.insert("", "end", values=(
                        M.TABLE_NAMES[r["tabell"]], r["adress"], M._v(r["a_värde"]), M._v(r["b_värde"]),
                        st, err), tags=(tag,))
            else:
                s = self.cb_reg_slave.get()
                slave = None if s in ("", "Alla") else int(s)
                self._reg_cols(("slav", "tab", "adr", "dec", "hex", "las", "skr", "and", "nfel", "fel", "hist"),
                               ("Slav", "Tabell", "Adress", "Värde", "Hex", "Läsn.", "Skrivn.", "Ändr.", "Fel",
                                "Senaste fel", "Historik (senaste 10)"),
                               (45, 120, 60, 70, 70, 55, 55, 50, 40, 200, 260))
                for r in rm.rows(slave):
                    if self.v_reg_diff.get() and not r["fel"]:
                        continue
                    self.reg_tv.insert("", "end", values=(
                        f"{r['slav']} {names.get(r['slav'], '')}".strip(), M.TABLE_NAMES[r["tabell"]], r["adress"],
                        "" if r["värde"] is None else r["värde"], r["hex"], r["läsningar"], r["skrivningar"],
                        r["ändringar"], r["fel"], r["senaste_fel"], r["historik"]),
                        tags=("err",) if r["fel"] else ())
            if sel_key:
                for iid in self.reg_tv.get_children():
                    if self.reg_tv.item(iid, "values")[:3] == sel_key:
                        self.reg_tv.selection_set(iid)
                        break

    def _reg_hist(self):
        sel = self.reg_tv.selection()
        if not sel or not self.mon:
            return
        v = self.reg_tv.item(sel[0], "values")
        tabs = {n: k for k, n in M.TABLE_NAMES.items()}
        out = []
        try:
            if self.v_reg_mode.get() == "jmf":
                keys = [(s, tabs[v[0]], int(v[1])) for s in self._ab()]
            else:
                keys = [(int(str(v[0]).split()[0]), tabs[v[1]], int(v[2]))]
        except Exception:
            return
        t0 = self.mon.t_first or 0
        for k in keys:
            r = self.mon.regmap.regs.get(k)
            if not r:
                continue
            h = ", ".join(f"{t - t0:.1f}s: {'-' if o is None else o}→{n}" for t, o, n in list(r.history)[-12:])
            out.append(f"Slav {k[0]} {M.TABLE_NAMES[k[1]]} {k[2]}: läsn {r.reads}, skrivn {r.writes}, "
                       f"fel {r.errors} {r.last_err}\n   ändringar: {h or '-'}")
        self.reg_hist.config(text="\n".join(out))

    def export_regs(self):
        if not self.mon:
            return
        jmf = self.v_reg_mode.get() == "jmf"
        path = filedialog.asksaveasfilename(parent=self.reg_win, defaultextension=".csv",
                                            initialfile="registerjamforelse.csv" if jmf else "registerdump.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with self.mon.lock:
            if jmf and self._ab():
                self.mon.regmap.export_compare_csv(path, *self._ab())
            else:
                self.mon.regmap.export_csv(path)
        messagebox.showinfo("Modbus", f"Sparat: {path}", parent=self.reg_win)
