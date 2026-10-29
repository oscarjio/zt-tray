"""
Fliken AI: chatta med en LLM om trafiken som Modbus-fliken fångat, och automatisk felsökning.
Nyckeln sparas lokalt i config.json (cfg["ai"]["api_key"]) under användarens profil - aldrig i repo/logg.
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import ai_agent as A
import config as C
import linecheck as L
import modbus_monitor as M

LOG_DIR = os.path.join(str(C.APP_DIR), "modbus-loggar")


class TabHost:
    """Kopplar agentens verktyg till Modbus- och Sändare-flikarna. Anropas från agentens tråd."""

    def __init__(self, mt, master_tab, root):
        self.mt, self.ms, self.root = mt, master_tab, root

    def ui(self, fn, timeout=15):
        if threading.current_thread() is threading.main_thread():
            return fn()
        q = queue.Queue()

        def run():
            try:
                q.put((True, fn()))
            except Exception as e:
                q.put((False, e))
        self.root.after(0, run)
        ok, v = q.get(timeout=timeout)
        if not ok:
            raise v
        return v

    def mon(self):
        return self.mt.mon

    def to_wall(self, t):
        return time.time() - (self.mt.clock() - t)

    def running(self):
        return self.mt.cap is not None

    def names(self):
        return {str(k): v for k, v in self.mt._names_cache.items()}

    def status(self):
        mt, mon = self.mt, self.mt.mon
        try:
            b, p, s = mt.line()
        except ValueError:
            b, p, s = "?", "?", "?"
        st = {"linje": f"{M.port_name(mt.cb_port.get())} {b} 8{p}{s}", "körs": self.running(),
              "länk": mt._link[1]}
        if mon:
            st.update({"bytes": mon.bytes_total, "giltiga_ramar": mon.crc_ok_frames, "trasiga_ramar": mon.bad_frames,
                       "uart_fel": dict(mon.line_err_total), "stopp_hittade": len(mt.stops)})
        return st

    def start(self):
        if not self.running():
            self.ui(self.mt.toggle)
        return {"körs": self.running()}

    def stop(self):
        if self.running():
            self.ui(self.mt.stop)
        return {"körs": self.running()}

    def set_line(self, baud, parity, stop):
        was = self.running()

        def do():
            if was:
                self.mt.stop()
            self.mt.cb_baud.set(str(baud))
            self.mt.cb_par.set(parity)
            self.mt.cb_stop.set(str(stop))
            if was:
                self.mt.toggle()
        self.ui(do)
        return {"linje": f"{baud} 8{parity}{stop}", "körs": self.running()}

    def autodetect(self, seconds=2.0):
        port = M.port_name(self.mt.cb_port.get())
        if not port:
            return {"fel": "ingen port vald"}
        was = self.running()
        rows = self.mt.mon.stats_rows() if self.mt.mon else None
        cur = self.mt.line()
        if was:
            self.ui(self.mt.stop)
        try:
            res = L.live_scan(port, max(0.5, min(5.0, seconds)))
        finally:
            if was:
                self.ui(self.mt.toggle)
        d = L.diagnose(res, current=cur, stats_rows=rows)
        top = [{"inst": L._lbl(r), "ramar": r["frames"], "trasiga": r["bad"], "uart": r["uart_err"]} for r in res[:6]]
        return {"rapport": L.report_text(d), "konfidens": d["confidence"], "topp": top}

    def set_trap(self, slaves, pre, post):
        def do():
            self.mt.ent_trap.delete(0, "end")
            self.mt.ent_trap.insert(0, ",".join(str(s) for s in slaves))
            self.mt.sp_pre.set(pre)
            self.mt.sp_post.set(post)
            if self.mt.mon:
                hdr = M.make_header(M.port_name(self.mt.cb_port.get()), *self.mt.line())
                self.mt.mon.trap = M.ErrorTrap(slaves, pre, post, LOG_DIR, hdr)
            self.mt.save_cfg()
        self.ui(do)
        return {"felfälla": slaves or "alla", "före_s": pre, "efter_s": post, "mapp": LOG_DIR}

    def master_request(self, slave, fc, addr, qty=1, values=None):
        return self.ms.request(slave, fc, addr, qty, values)

    def master_scan(self, start, end):
        return {"hittade": self.ms.scan(start, end, 100, from_ui=False)}


class AiTab:
    def __init__(self, f, win, modbus_tab, master_tab):
        self.f, self.win, self.app = f, win, win.app
        self.host = TabHost(modbus_tab, master_tab, f)
        self.ms = master_tab
        ai = dict(self.app.cfg.get("ai") or {})
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            jpath = os.path.join(LOG_DIR, time.strftime("ai-journal_%Y%m%d-%H%M%S.jsonl"))
        except OSError:
            jpath = None
        self.journal = A.Journal(path=jpath)
        self.agent = None
        self.busy = False

        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="API-bas").pack(side="left")
        self.e_base = ttk.Entry(top, width=30)
        self.e_base.insert(0, ai.get("base_url") or A.DEFAULT_BASE)
        self.e_base.pack(side="left", padx=4)
        ttk.Label(top, text="Modell").pack(side="left")
        self.cb_model = ttk.Combobox(top, width=34, values=A.MODELS)
        self.cb_model.set(ai.get("model") or A.DEFAULT_MODEL)
        self.cb_model.pack(side="left", padx=4)
        ttk.Label(top, text="API-nyckel").pack(side="left")
        self.e_key = ttk.Entry(top, width=22, show="•")
        self.e_key.pack(side="left", padx=4)
        ttk.Button(top, text="Spara", command=self.save).pack(side="left", padx=4)
        self.lbl_key = ttk.Label(top, text="", style="Sub.TLabel")
        self.lbl_key.pack(side="left", padx=6)

        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=6)
        self.v_send = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="Tillåt AI att sända (kräver aktiverad Sändare + bekräftelse varje gång)",
                        variable=self.v_send, command=self._send_toggle).pack(side="left")
        self.v_auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="Automatisk övervakning", variable=self.v_auto,
                        command=self._auto_toggle).pack(side="left", padx=16)
        ttk.Button(r2, text="Ny chatt", command=self.new_chat).pack(side="right")
        self.lbl_use = ttk.Label(r2, text="", style="Sub.TLabel")
        self.lbl_use.pack(side="right", padx=10)

        self.txt = tk.Text(f, height=22, wrap="word", font=("Segoe UI", 10))
        self.txt.tag_configure("user", foreground="#58a6ff", font=("Segoe UI", 10, "bold"))
        self.txt.tag_configure("tool", foreground="#8b949e", font=("Consolas", 9))
        self.txt.tag_configure("err", foreground="#da3633")
        self.txt.pack(fill="both", expand=True)
        r3 = ttk.Frame(f)
        r3.pack(fill="x", pady=6)
        self.e_in = ttk.Entry(r3)
        self.e_in.pack(side="left", fill="x", expand=True)
        self.e_in.bind("<Return>", lambda e: self.ask())
        self.btn_ask = ttk.Button(r3, text="Fråga", style="Accent.TButton", command=self.ask)
        self.btn_ask.pack(side="left", padx=6)
        for q in ("Nu strular det", "Sammanfatta bussen", "Varför felar slav…?"):
            ttk.Button(r3, text=q, command=lambda q=q: self._quick(q)).pack(side="left", padx=2)
        self._say("tool", "Exempel: \"Vi har ett fel på modbus, det kommer 3 ggr per halvtimme, sätt igång automatisk "
                          "övervakning.\" Skriv sedan \"nu strular det\" när felet syns. AI:n får sammanfattningar och "
                          "sökresultat - inte all rådata. Kostnad visas uppe till höger.\n")
        self._key_label()
        self._tick()

    # ------------------------------------------------------------ inställningar
    def key(self):
        return A.get_key(self.app.cfg)

    def _key_label(self):
        k = self.key()
        src = "config" if ((self.app.cfg.get("ai") or {}).get("api_key")) else ("miljövariabel" if k else "")
        self.lbl_key.config(text=f"nyckel: {A.mask(k)} {('(' + src + ')') if src else ''}")

    def save(self):
        newkey = self.e_key.get().strip()

        def upd(cfg):
            ai = dict(cfg.get("ai") or {})
            ai["base_url"] = self.e_base.get().strip() or A.DEFAULT_BASE
            ai["model"] = self.cb_model.get().strip() or A.DEFAULT_MODEL
            if newkey:
                ai["api_key"] = newkey
            cfg["ai"] = ai
        self.app.update_cfg(upd)
        self.e_key.delete(0, "end")
        self.agent = None
        self._key_label()

    def _get_agent(self):
        k = self.key()
        if not k:
            raise A.LLMError("Ingen API-nyckel. Ange den i fältet API-nyckel och tryck Spara "
                             "(eller sätt miljövariabeln OPENROUTER_API_KEY).")
        client = A.LLMClient(k, self.e_base.get().strip(), self.cb_model.get().strip())
        if self.agent is None:
            self.agent = A.Agent(self.host, client, self.journal, allow_send=self._allow_send,
                                 confirm=self._confirm)
        else:
            self.agent.client = client
        return self.agent

    def _allow_send(self):
        return bool(self.v_send.get()) and self.ms.armed

    def _send_toggle(self):
        if self.v_send.get() and not self.ms.armed:
            messagebox.showinfo("AI", "Aktivera först sändning i fliken Sändare (gäller sessionen).", parent=self.f)
            self.v_send.set(False)

    def _confirm(self, desc):
        return bool(self.host.ui(lambda: messagebox.askyesno("AI vill sända", desc + "\n\nTillåt?",
                                                              icon="warning", parent=self.f), timeout=600))

    def _auto_toggle(self):
        if self.v_auto.get():
            desc = self.e_in.get().strip() or "Intermittent Modbus-fel"
            try:
                ag = self._get_agent()
            except A.LLMError:
                ag = A.Agent(self.host, None, self.journal)
                self.agent = ag
            try:
                ag.start_auto(desc)
            except Exception as e:
                self._say("err", f"Kunde inte starta lyssning: {e}\n")
            self._say("tool", f"Automatisk övervakning startad ({time.strftime('%H:%M')}): {desc}. "
                              "Felhändelser sparas långsiktigt (journal + modbus-loggar). Skriv 'nu strular det' när det händer.\n")
        elif self.agent:
            self.agent.stop_auto()
            self._say("tool", "Automatisk övervakning stoppad.\n")

    def new_chat(self):
        if self.agent:
            self.agent.reset()
        self.txt.delete("1.0", "end")

    # ------------------------------------------------------------ chatt
    def _quick(self, q):
        self.e_in.delete(0, "end")
        self.e_in.insert(0, q)
        if not q.endswith("…?"):
            self.ask()

    def ask(self):
        text = self.e_in.get().strip()
        if not text or self.busy:
            return
        low = text.lower()
        if not self.v_auto.get() and ("automat" in low and ("övervak" in low or "monitor" in low)):
            self.v_auto.set(True)
            self._auto_toggle()
        try:
            ag = self._get_agent()
        except A.LLMError as e:
            self._say("err", str(e) + "\n")
            return
        self.e_in.delete(0, "end")
        self._say("user", "Du: " + text + "\n")
        self.busy = True
        self.btn_ask.state(["disabled"])

        def work():
            try:
                ans = ag.chat(text, on_step=lambda s: self.f.after(0, lambda: self._say("tool", s + "\n")))
                self.f.after(0, lambda: self._say(None, "AI: " + ans + "\n\n"))
            except Exception as e:
                msg = A.redact(str(e), self.key())
                self.f.after(0, lambda: self._say("err", "Fel: " + msg + "\n"))
            finally:
                self.f.after(0, self._done)
        threading.Thread(target=work, daemon=True).start()

    def _done(self):
        self.busy = False
        self.btn_ask.state(["!disabled"])

    def _say(self, tag, s):
        self.txt.insert("end", s, (tag,) if tag else ())
        self.txt.see("end")

    def _tick(self):
        try:
            if not self.f.winfo_exists():
                return
            if self.agent:
                self.agent.tick()
                if self.agent.client:
                    self.lbl_use.config(text=self.agent.usage_text())
            else:
                self.journal.update(self.host.mon(), self.host.to_wall)
        except Exception as e:
            C.log(f"ai-flik: {type(e).__name__}")
        self.f.after(1000, self._tick)
