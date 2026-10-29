"""
Fliken Sändare: Modbus Poll-liknande master (läs/skriv, adress-scan, pollning).
AV som standard. Måste aktiveras en gång per session efter varning. Sänder via lyssnarens
port om lyssning pågår (då syns trafiken i Modbus-fliken), annars via egen port.
"""

import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import modbus_master as MM
import modbus_monitor as M

FCS = ["3 Läs holdingreg", "4 Läs inputreg", "1 Läs coils", "2 Läs ingångar", "6 Skriv ett register",
       "16 Skriv register", "5 Skriv en coil", "15 Skriv coils"]
FMTS = ["u16", "s16", "hex", "u32", "s32", "f32"]


class MasterTab:
    def __init__(self, f, win, modbus_tab):
        self.f, self.win, self.mt = f, win, modbus_tab
        self.armed = False
        self.master = None
        self._link_key = None
        self.poller = None
        self.busy = False

        warn = tk.Label(f, text="⚠ " + MM.WARNING, fg="white", bg="#8b1a1a", anchor="w", justify="left",
                        wraplength=900, font=("Segoe UI", 10, "bold"), padx=8, pady=6)
        warn.pack(fill="x")
        r0 = ttk.Frame(f)
        r0.pack(fill="x", pady=(8, 4))
        self.v_arm = tk.BooleanVar(value=False)
        ttk.Checkbutton(r0, text="Aktivera sändning (gäller bara denna session)", variable=self.v_arm,
                        style="Switch.TCheckbutton", command=self._arm).pack(side="left")
        self.lbl_mode = ttk.Label(r0, text="", style="Sub.TLabel")
        self.lbl_mode.pack(side="left", padx=12)

        r1 = ttk.Frame(f)
        r1.pack(fill="x", pady=4)
        ttk.Label(r1, text="Port").pack(side="left")
        self.cb_port = ttk.Combobox(r1, width=34)
        self.cb_port.pack(side="left", padx=(4, 10))
        ttk.Label(r1, text="(tom = samma som Modbus-fliken; används delat om lyssning pågår)",
                  style="Sub.TLabel").pack(side="left")
        ttk.Label(r1, text="  Timeout ms").pack(side="left")
        self.sp_to = ttk.Spinbox(r1, from_=20, to=5000, increment=10, width=6)
        self.sp_to.set(300)
        self.sp_to.pack(side="left", padx=4)

        r2 = ttk.LabelFrame(f, text="Fråga", padding=8)
        r2.pack(fill="x", pady=6)
        self.e_slave = self._ent(r2, "Slav", "1", 4)
        ttk.Label(r2, text="Funktion").pack(side="left")
        self.cb_fc = ttk.Combobox(r2, width=20, values=FCS, state="readonly")
        self.cb_fc.set(FCS[0])
        self.cb_fc.pack(side="left", padx=(4, 10))
        self.e_addr = self._ent(r2, "Adress", "0", 6)
        self.e_qty = self._ent(r2, "Antal", "10", 5)
        self.e_vals = self._ent(r2, "Värden (skriv)", "", 18)
        ttk.Label(r2, text="Format").pack(side="left")
        self.cb_fmt = ttk.Combobox(r2, width=5, values=FMTS, state="readonly")
        self.cb_fmt.set("u16")
        self.cb_fmt.pack(side="left", padx=4)
        self.btn_send = ttk.Button(r2, text="Sänd", style="Accent.TButton", command=self.send_once)
        self.btn_send.pack(side="left", padx=6)
        ttk.Label(r2, text="Poll s").pack(side="left")
        self.sp_poll = ttk.Spinbox(r2, from_=0.1, to=3600, increment=0.5, width=5)
        self.sp_poll.set(1.0)
        self.sp_poll.pack(side="left", padx=4)
        self.btn_poll = ttk.Button(r2, text="Starta poll", command=self.toggle_poll)
        self.btn_poll.pack(side="left", padx=4)

        r3 = ttk.LabelFrame(f, text="Adress-scan (hitta enheter)", padding=8)
        r3.pack(fill="x", pady=6)
        self.e_s0 = self._ent(r3, "Från", "1", 4)
        self.e_s1 = self._ent(r3, "Till", "247", 4)
        self.e_sto = self._ent(r3, "Timeout ms", "100", 5)
        self.btn_scan = ttk.Button(r3, text="Scanna", command=self.scan)
        self.btn_scan.pack(side="left", padx=6)
        self.lbl_scan = ttk.Label(r3, text="", style="Sub.TLabel")
        self.lbl_scan.pack(side="left", padx=6)

        self.txt = tk.Text(f, height=18, font=("Consolas", 9), wrap="none")
        self.txt.pack(fill="both", expand=True, pady=6)
        self.lbl_cnt = ttk.Label(f, text="", style="Sub.TLabel")
        self.lbl_cnt.pack(anchor="w")
        self._set_state()

    def _ent(self, parent, label, default, w):
        ttk.Label(parent, text=label).pack(side="left")
        e = ttk.Entry(parent, width=w)
        e.insert(0, default)
        e.pack(side="left", padx=(4, 10))
        return e

    # ---------------------------------------------------------------- aktivering
    def _arm(self):
        if self.v_arm.get():
            ok = messagebox.askyesno("Aktivera sändning", MM.WARNING + "\n\nAktivera sändning för denna session?",
                                     icon="warning", parent=self.f)
            self.armed = bool(ok)
            self.v_arm.set(self.armed)
        else:
            self.armed = False
            self._drop_master()
        self._set_state()

    def _set_state(self):
        st = ["!disabled"] if self.armed else ["disabled"]
        for b in (self.btn_send, self.btn_poll, self.btn_scan):
            b.state(st)
        self.lbl_mode.config(text="SÄNDNING AKTIV" if self.armed else "Sändning avstängd")

    def _drop_master(self):
        if self.poller:
            self.poller.stop()
            self.poller = None
            self.btn_poll.config(text="Starta poll")
        if self.master:
            self.master.close()
        self.master, self._link_key = None, None

    def get_master(self):
        """Master för aktuell port. Delar lyssnarens port om den lyssnar."""
        if not self.armed:
            raise MM.NotArmed("Sändning är inte aktiverad i fliken Sändare.")
        mt = self.mt
        port = M.port_name(self.cb_port.get()) or M.port_name(mt.cb_port.get())
        baud, par, stop = mt.line()
        cap = mt.cap
        shared = cap is not None and (hasattr(cap, "taps") or hasattr(cap, "caps"))
        key = (port, baud, par, stop, id(cap) if shared else None)
        if self.master and key == self._link_key:
            self.master.timeout = float(self.sp_to.get()) / 1000
            return self.master
        self._drop_master()
        if shared:
            link = MM.SharedLink(cap, mt.mon)
        else:
            if not port:
                raise OSError("Ingen port vald.")
            link = MM.SerialLink(port, baud, par, stop)
        self.master = MM.Master(link, baud, par, stop, timeout=float(self.sp_to.get()) / 1000, armed=True)
        self._link_key = key
        return self.master

    # ---------------------------------------------------------------- API (även för AI)
    def request(self, slave, fc, addr, qty=1, values=None):
        r = self.get_master().request(slave, fc, addr, qty, values)
        self.f.after(0, lambda: self._show(slave, fc, addr, r))
        return r

    def scan(self, start=None, end=None, timeout_ms=None, from_ui=True):
        if start is None:
            start, end = int(self.e_s0.get()), int(self.e_s1.get())
            timeout_ms = float(self.e_sto.get())
        m = self.get_master()

        def work():
            self.busy = True
            found = m.scan(range(start, end + 1), timeout=(timeout_ms or 100) / 1000,
                           progress=lambda a: self.f.after(0, lambda: self.lbl_scan.config(text=f"adress {a}…")),
                           stop_flag=lambda: not self.armed)
            self.busy = False
            self.f.after(0, lambda: self._scan_done(found))
            return found
        if from_ui:
            threading.Thread(target=work, daemon=True).start()
            return None
        return work()

    # ---------------------------------------------------------------- UI-handlingar
    def _params(self):
        fc = int(self.cb_fc.get().split()[0])
        vals = [int(x, 0) for x in self.e_vals.get().replace(";", ",").replace(" ", ",").split(",") if x.strip()]
        if fc in (5, 6, 15, 16) and not vals:
            raise ValueError("Ange värde(n) att skriva")
        return dict(slave=int(self.e_slave.get()), fc=fc, addr=int(self.e_addr.get(), 0),
                    qty=int(self.e_qty.get() or 1), values=vals if fc in (5, 6, 15, 16) else None)

    def send_once(self):
        try:
            p = self._params()
            self.get_master()
        except Exception as e:
            messagebox.showerror("Sändare", str(e), parent=self.f)
            return
        threading.Thread(target=lambda: self._safe(lambda: self.request(**p)), daemon=True).start()

    def _safe(self, fn):
        try:
            fn()
        except Exception as e:
            msg = str(e)
            self.f.after(0, lambda: self._log(f"FEL: {msg}"))

    def toggle_poll(self):
        if self.poller:
            self.poller.stop()
            self.poller = None
            self.btn_poll.config(text="Starta poll")
            return
        try:
            p = self._params()
            m = self.get_master()
        except Exception as e:
            messagebox.showerror("Sändare", str(e), parent=self.f)
            return
        self.poller = MM.Poller(m, [p], float(self.sp_poll.get()),
                                on_result=lambda it, r: self.f.after(0, lambda: self._show(
                                    it["slave"], it["fc"], it["addr"], r)))
        self.poller.start()
        self.btn_poll.config(text="Stoppa poll")

    def _scan_done(self, found):
        self.lbl_scan.config(text=f"klart: {len(found)} enheter")
        self._log("SCAN: " + (", ".join(f"{d['slav']} ({d['svar']}, {d['rtt_ms']} ms)" for d in found) or "inga svar"))

    def _show(self, slave, fc, addr, r):
        line = f"{time.strftime('%H:%M:%S')} slav {slave} FC{fc} @{addr}: {r.get('text')}"
        if r.get("rtt_ms") is not None:
            line += f"  ({r['rtt_ms']} ms)"
        self._log(line)
        self._log(f"   TX {r.get('req', '')}\n   RX {r.get('resp', '')}")
        if r.get("values") is not None and fc in (3, 4):
            vals = MM.format_values(r["values"], addr, self.cb_fmt.get())
            self._log("   " + "  ".join(f"[{a}]={v}" for a, v in vals))
        if self.master:
            self.lbl_cnt.config(text="  ".join(f"{k}: {v}" for k, v in self.master.count.items()))

    def _log(self, s):
        self.txt.insert("end", s + "\n")
        if int(self.txt.index("end-1c").split(".")[0]) > 3000:
            self.txt.delete("1.0", "500.0")
        self.txt.see("end")
