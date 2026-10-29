"""
Fliken Modbus i ZT-Tray: passiv Modbus RTU-busmonitor (se modbus_monitor.py).
All Tk körs i UI-tråden; lästråden matar Monitor och fliken hämtar händelser var 200 ms.
"""

import os
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import config as C
import modbus_monitor as M

MAX_ROWS = 3000
LOG_DIR = os.path.join(str(C.APP_DIR), "modbus-loggar")


def parse_names(text):
    """'1=LD2, 4=SW1A gammal, 5=SW1A ny' -> {1: 'LD2', ...}"""
    out = {}
    for part in text.replace(";", ",").split(","):
        if "=" in part:
            a, n = part.split("=", 1)
            if a.strip().isdigit():
                out[int(a.strip())] = n.strip()
    return out


class ModbusTab:
    def __init__(self, f, win):
        self.f = f
        self.win = win
        self.app = win.app
        mc = self.app.cfg.get("modbus") or {}
        self.mon = None
        self.cap = None
        self.t0 = 0.0
        self.compare_win = None
        self.auto_busy = False
        self._names_cache = {}
        self.reg_win = None
        self._reg_tick = 0

        # --- rad 1: port och linje (manuellt är standard)
        r1 = ttk.Frame(f)
        r1.pack(fill="x")
        ttk.Label(r1, text="Port").pack(side="left")
        self.cb_port = ttk.Combobox(r1, width=26, values=M.list_ports())
        self.cb_port.set(mc.get("port", (M.list_ports() or ["COM3"])[0]))
        self.cb_port.pack(side="left", padx=(4, 10))
        self.cb_port.bind("<Button-1>", lambda e: self.cb_port.configure(values=M.list_ports()))
        ttk.Label(r1, text="Baud").pack(side="left")
        self.cb_baud = ttk.Combobox(r1, width=7, values=[str(b) for b in sorted(M.BAUDS)])
        self.cb_baud.set(str(mc.get("baud", 19200)))
        self.cb_baud.pack(side="left", padx=(4, 10))
        ttk.Label(r1, text="Paritet").pack(side="left")
        self.cb_par = ttk.Combobox(r1, width=4, state="readonly", values=["N", "E", "O"])
        self.cb_par.set(mc.get("parity", "E"))
        self.cb_par.pack(side="left", padx=(4, 10))
        ttk.Label(r1, text="Stopp").pack(side="left")
        self.cb_stop = ttk.Combobox(r1, width=3, state="readonly", values=["1", "2"])
        self.cb_stop.set(str(mc.get("stop", 1)))
        self.cb_stop.pack(side="left", padx=(4, 10))
        ttk.Label(r1, text="Timeout ms").pack(side="left")
        self.sp_to = ttk.Spinbox(r1, from_=20, to=5000, increment=10, width=6)
        self.sp_to.set(mc.get("timeout_ms", 500))
        self.sp_to.pack(side="left", padx=(4, 10))
        self.btn_auto = ttk.Button(r1, text="Auto-sök…", command=self.autodetect)
        self.btn_auto.pack(side="left")
        self.btn_start = ttk.Button(r1, text="Starta", style="Accent.TButton", command=self.toggle)
        self.btn_start.pack(side="right")

        # --- rad 2: bevakad slav / felfälla / namn
        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=(8, 0))
        ttk.Label(r2, text="Bevakad slav (felfälla)").pack(side="left")
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
        ttk.Checkbutton(r2, text="Logga rådata", variable=self.v_log).pack(side="left")
        ttk.Button(r2, text="Öppna mapp", command=self.open_dir).pack(side="right")

        r2b = ttk.Frame(f)
        r2b.pack(fill="x", pady=(6, 0))
        ttk.Label(r2b, text="Namn").pack(side="left")
        self.ent_names = ttk.Entry(r2b)
        self.ent_names.insert(0, mc.get("names", "1=LD2, 4=SW1A4080 gammal, 5=SW1A4080 ny"))
        self.ent_names.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Label(f, text="Lyssnar bara, sänder aldrig. Slavnummer i felfällan, t.ex. 5 eller 4,5. "
                  "Namn: adress=namn, kommaseparerat.", style="Sub.TLabel").pack(anchor="w")

        # --- statistik
        self.lbl_info = ttk.Label(f, text="Stoppad", style="Sub.TLabel")
        self.lbl_info.pack(anchor="w", pady=(6, 2))
        cols = ("slav", "namn", "fragor", "svar", "timeout", "crc", "exc", "ovr", "felp",
                "min", "medel", "max", "p99", "senast")
        heads = ("Slav", "Namn", "Frågor", "Svar", "Timeout", "CRC", "Exc", "Övr", "Fel %",
                 "Min ms", "Medel", "Max", "p99", "Senaste fel")
        widths = (40, 120, 60, 60, 60, 45, 40, 40, 50, 55, 55, 55, 55, 260)
        sf = ttk.Frame(f)
        sf.pack(fill="x")
        self.tv_stats = ttk.Treeview(sf, columns=cols, show="headings", height=6)
        for c, h, w in zip(cols, heads, widths):
            self.tv_stats.heading(c, text=h)
            self.tv_stats.column(c, width=w, stretch=(c == "senast"), anchor="w" if c in ("namn", "senast") else "e")
        self.tv_stats.tag_configure("err", foreground="#e5534b")
        self.tv_stats.tag_configure("warn", foreground="#d29922")
        self.tv_stats.pack(fill="x")
        sb = ttk.Frame(f)
        sb.pack(fill="x", pady=(4, 0))
        ttk.Button(sb, text="Jämför markerade sida vid sida", command=self.open_compare).pack(side="left")
        ttk.Button(sb, text="Registervy…", command=self.open_regs).pack(side="left", padx=(6, 0))
        ttk.Button(sb, text="Nollställ", command=self.reset).pack(side="left", padx=6)
        ttk.Button(sb, text="Exportera CSV…", command=self.export_csv).pack(side="left")
        ttk.Button(sb, text="Spela upp logg…", command=self.replay_file).pack(side="left", padx=6)

        # --- paketlista
        r3 = ttk.Frame(f)
        r3.pack(fill="x", pady=(8, 2))
        ttk.Label(r3, text="Visa slav").pack(side="left")
        self.cb_filter = ttk.Combobox(r3, width=8, values=["Alla"])
        self.cb_filter.set("Alla")
        self.cb_filter.pack(side="left", padx=(4, 10))
        self.v_err = tk.BooleanVar(value=False)
        ttk.Checkbutton(r3, text="Bara fel", variable=self.v_err, command=self.refilter).pack(side="left")
        self.v_follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, text="Följ", variable=self.v_follow).pack(side="left", padx=8)
        self.cb_filter.bind("<<ComboboxSelected>>", lambda e: self.refilter())
        self.cb_filter.bind("<Return>", lambda e: self.refilter())
        ttk.Button(r3, text="Rensa lista", command=self.clear_list).pack(side="right")

        pf = ttk.Frame(f)
        pf.pack(fill="both", expand=True)
        pcols = ("tid", "typ", "slav", "fc", "ms", "text", "hex")
        self.tv = ttk.Treeview(pf, columns=pcols, show="headings")
        for c, h, w in zip(pcols, ("Tid s", "Typ", "Slav", "FC", "Svar ms", "Avkodat", "Hex"),
                           (80, 90, 50, 35, 60, 330, 300)):
            self.tv.heading(c, text=h)
            self.tv.column(c, width=w, stretch=c in ("text", "hex"), anchor="w")
        self.tv.tag_configure("err", background="#5a1e1e", foreground="#ffd7d5")
        self.tv.tag_configure("warn", background="#4d3b10", foreground="#ffe8a3")
        ys = ttk.Scrollbar(pf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=ys.set)
        self.tv.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.tv.bind("<Control-c>", self.copy_rows)
        self.all_events = []
        self._poll()

    # ------------------------------------------------------------ hjälp
    def names(self):
        return parse_names(self.ent_names.get())

    def line(self):
        return (int(self.cb_baud.get()), self.cb_par.get() or "E", int(self.cb_stop.get() or 1))

    def trap_slaves(self):
        return [int(x) for x in self.ent_trap.get().replace(";", ",").split(",") if x.strip().isdigit()]

    def save_cfg(self):
        def upd(cfg):
            cfg["modbus"] = {
                "port": self.cb_port.get().strip(), "baud": int(self.cb_baud.get()),
                "parity": self.cb_par.get(), "stop": int(self.cb_stop.get()),
                "timeout_ms": int(float(self.sp_to.get())), "trap": self.ent_trap.get().strip(),
                "trap_pre": float(self.sp_pre.get()), "trap_post": float(self.sp_post.get()),
                "log": bool(self.v_log.get()), "names": self.ent_names.get().strip()}
        try:
            self.app.update_cfg(upd)
        except Exception as e:
            C.log(f"modbus: kunde inte spara inställningar: {e!r}")

    def new_monitor(self, baud, par, stop, hdr):
        mon = M.Monitor(baud, par, stop, resp_timeout=max(0.02, float(self.sp_to.get()) / 1000))
        traps = self.trap_slaves()
        if traps:
            mon.trap = M.ErrorTrap(traps, float(self.sp_pre.get()), float(self.sp_post.get()),
                                   LOG_DIR, hdr)
        return mon

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
        port = self.cb_port.get().strip()
        self.save_cfg()
        hdr = M.make_header(port, baud, par, stop)
        self.mon = self.new_monitor(baud, par, stop, hdr)
        log_path = None
        if self.v_log.get():
            os.makedirs(LOG_DIR, exist_ok=True)
            log_path = os.path.join(LOG_DIR, time.strftime("bus_%Y%m%d-%H%M%S.zmlog"))
        self.clear_list()
        self.cap = M.Capture(self.mon, port, baud, par, stop, log_path=log_path)
        try:
            self.cap.start()
        except Exception as e:
            self.cap = None
            messagebox.showerror("Modbus", f"Kan inte öppna {port}:\n{e}", parent=self.f)
            return
        self.t0 = time.perf_counter()
        self.log_path = log_path
        self.btn_start.config(text="Stoppa")
        C.log(f"modbus: lyssnar på {port} {baud} 8{par}{stop}")

    def stop(self):
        cap, self.cap = self.cap, None
        if cap:
            cap.stop()
        self.btn_start.config(text="Starta")

    def reset(self):
        if self.mon:
            self.mon.reset_stats()
        self.tv_stats.delete(*self.tv_stats.get_children())

    # ------------------------------------------------------------ auto
    def autodetect(self):
        if self.cap or self.auto_busy:
            messagebox.showinfo("Modbus", "Stoppa lyssningen först.", parent=self.f)
            return
        port = self.cb_port.get().strip()
        self.auto_busy = True
        self.btn_auto.config(text="Söker…")
        state = {"txt": ""}

        def prog(i, n, b, p, s):
            state["txt"] = f"Auto-sök {i + 1}/{n}: {b} 8{p}{s}"

        def work():
            try:
                res = M.autodetect(port, 1.5, progress=prog)
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
        self._names_cache = {}
        self.btn_auto.config(text="Auto-sök…")
        if err or not res:
            messagebox.showerror("Modbus", f"Auto-sök misslyckades: {err}", parent=self.f)
            return
        top = tk.Toplevel(self.f)
        top.title("Auto-sök: poäng per inställning")
        tv = ttk.Treeview(top, columns=("l", "fr", "bad", "u", "p"), show="headings", height=16)
        for c, h in zip(("l", "fr", "bad", "u", "p"), ("Inställning", "Giltiga ramar", "Skräp/CRC", "UART-fel", "Poäng")):
            tv.heading(c, text=h)
            tv.column(c, width=110, anchor="e")
        for r in res:
            tv.insert("", "end", values=(f"{r['baud']} 8{r['parity']}{r['stop']}", r["frames"], r["bad"],
                                         r["uart_err"], r["score"]))
        tv.pack(fill="both", expand=True, padx=10, pady=10)
        best = res[0]
        note = M.autodetect_note(best, res)
        if best["frames"] == 0:
            msg = "Inga giltiga ramar. Kontrollera A/B (prova att skifta), trafik på bussen och adaptern."
        else:
            msg = f"Bäst: {best['baud']} 8{best['parity']}{best['stop']}. {note}"
        ttk.Label(top, text=msg, wraplength=520).pack(padx=10, anchor="w")

        def use():
            self.cb_baud.set(str(best["baud"]))
            self.cb_par.set(best["parity"])
            self.cb_stop.set(str(best["stop"]))
            top.destroy()
        b = ttk.Frame(top)
        b.pack(pady=10)
        if best["frames"]:
            ttk.Button(b, text="Använd bästa", style="Accent.TButton", command=use).pack(side="left", padx=4)
        ttk.Button(b, text="Stäng", command=top.destroy).pack(side="left", padx=4)
        self.lbl_info.config(text=msg)

    # ------------------------------------------------------------ uppspelning/export
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
        self.mon = M.Monitor(baud, par, stop, resp_timeout=max(0.02, float(self.sp_to.get()) / 1000))
        self.clear_list()
        M.replay(chunks, self.mon)
        self.t0 = chunks[0][0] if chunks else 0.0
        self.lbl_info.config(text=f"Uppspelad: {os.path.basename(p)}  ({len(chunks)} block)")

    def export_csv(self):
        if not self.mon:
            return
        p = filedialog.asksaveasfilename(parent=self.f, defaultextension=".csv",
                                         filetypes=[("CSV", "*.csv")], initialfile="modbus.csv")
        if p:
            M.export_csv(list(self.mon.events), p, self.t0)

    def open_dir(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        os.startfile(LOG_DIR)

    # ------------------------------------------------------------ lista
    def _match(self, e):
        flt = self.cb_filter.get().strip()
        if flt and flt != "Alla":
            try:
                if e.slave != int(flt.split()[0]):
                    return False
            except ValueError:
                pass
        if self.v_err.get() and e.level == "ok":
            return False
        return True

    def _insert(self, e):
        names = self._names_cache
        s = "" if e.slave is None else str(e.slave)
        if e.slave in names:
            s += f" {names[e.slave]}"
        self.tv.insert("", "end", values=(
            f"{e.t - self.t0:.4f}", M.KIND_TEXT.get(e.kind, e.kind), s,
            "" if e.fc is None else f"{e.fc:02X}", "" if e.rtt is None else f"{e.rtt * 1000:.1f}",
            e.text, e.raw.hex(" ")), tags=(e.level,))

    def refilter(self):
        self.tv.delete(*self.tv.get_children())
        self._names_cache = self.names()
        for e in [x for x in self.all_events if self._match(x)][-MAX_ROWS:]:
            self._insert(e)

    def clear_list(self):
        self.all_events = []
        self.tv.delete(*self.tv.get_children())

    def copy_rows(self, _e=None):
        rows = ["\t".join(str(v) for v in self.tv.item(i, "values")) for i in self.tv.selection()]
        self.f.clipboard_clear()
        self.f.clipboard_append("\n".join(rows))

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
        if self.cap and not self.cap.th.is_alive():
            err = self.cap.error
            self.stop()
            if err:
                messagebox.showerror("Modbus", f"Porten stängdes: {err}", parent=self.f)
        if not self.mon:
            return
        self._names_cache = self.names()
        evs = self.mon.drain()
        if evs:
            self.all_events.extend(evs)
            if len(self.all_events) > 50000:
                self.all_events = self.all_events[-40000:]
            for e in evs:
                if self._match(e):
                    self._insert(e)
            kids = self.tv.get_children()
            if len(kids) > MAX_ROWS:
                self.tv.delete(*kids[:len(kids) - MAX_ROWS])
            if self.v_follow.get() and self.tv.get_children():
                self.tv.see(self.tv.get_children()[-1])
        rows = self.mon.stats_rows()
        names = self._names_cache
        sel = {self.tv_stats.item(i, "values")[0] for i in self.tv_stats.selection()}
        self.tv_stats.delete(*self.tv_stats.get_children())
        f = lambda v: "-" if v is None else f"{v:.1f}"
        for r in rows:
            tag = "err" if r["timeout"] or r["crc"] or r["övrigt"] else ("warn" if r["exception"] else "")
            iid = self.tv_stats.insert("", "end", values=(
                r["slav"], names.get(r["slav"], ""), r["frågor"], r["svar"], r["timeout"], r["crc"],
                r["exception"], r["övrigt"], f"{r['felprocent']:.2f}", f(r["min"]), f(r["medel"]),
                f(r["max"]), f(r["p99"]), r["senaste_fel"]), tags=(tag,))
            if str(r["slav"]) in sel:
                self.tv_stats.selection_add(iid)
        self.cb_filter.configure(values=["Alla"] + [f"{r['slav']} {names.get(r['slav'], '')}".strip() for r in rows])
        info = f"{self.mon.bytes_total} byte, {self.mon.frames_total} ramar"
        if self.cap:
            info = f"Lyssnar på {self.cap.port_name} {self.mon.baud} 8{self.mon.parity}{self.mon.stop}  ·  " + info
            errs = self.cap.port.errors() if self.cap.port else {}
            if errs and any(errs.values()):
                info += f"  ·  UART-fel: ram {errs.get('frame', 0)} paritet {errs.get('parity', 0)} överskridning {errs.get('overrun', 0)}"
        if self.mon.trap and self.mon.trap.saved:
            info += f"  ·  felfälla: {len(self.mon.trap.saved)} fil(er)"
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
