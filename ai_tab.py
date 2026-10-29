"""
Fliken AI: chatta med en LLM om trafiken som Modbus-fliken fångat, och automatisk felsökning.
Nyckeln sparas lokalt i config.json (cfg["ai"]["api_key"]) under användarens profil - aldrig i repo/logg.
"""

import os
import sys
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import ai_agent as A
import agent_mode as AM
import cursor_backend as CB
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

    def autodetect(self, seconds=2.0, apply=False, baud=None):
        port = M.port_name(self.mt.cb_port.get())
        if not port:
            return {"fel": "ingen port vald"}
        was = self.running()
        rows = self.mt.mon.stats_rows() if self.mt.mon else None
        cur = self.mt.line()
        if was:
            self.ui(self.mt.stop)
        try:
            kw = {"bauds": (int(baud),)} if baud else {}
            res = L.live_scan(port, max(0.5, min(5.0, seconds)), **kw)
        finally:
            if was:
                self.ui(self.mt.toggle)
        d = L.diagnose(res, current=cur, stats_rows=rows)
        top = [{"inst": L._lbl(r), "ramar": r["frames"], "trasiga": r["bad"], "uart": r["uart_err"]} for r in res[:6]]
        out = {"rapport": L.report_text(d), "konfidens": d["confidence"], "topp": top}
        b = d.get("best")
        if apply and b and d["confidence"] >= 60:
            self.set_line(b["baud"], b["parity"], b["stop"])
            out["vald"] = f"{b['baud']} 8{b['parity']}{b['stop']}"
        return out

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
        ttk.Label(top, text="Leverantör").pack(side="left")
        self.cb_prov = ttk.Combobox(top, width=11, state="readonly", values=["OpenRouter", "Cursor"])
        self.cb_prov.set("Cursor" if ai.get("provider") == "cursor" else "OpenRouter")
        self.cb_prov.pack(side="left", padx=4)
        self.cb_prov.bind("<<ComboboxSelected>>", lambda e: self._prov_changed())
        self._cclient = None
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
        self.btn_sdk = ttk.Button(top, text="Hämta Cursor-komponent", command=self._get_sdk)

        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=6)
        self.v_send = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="Tillåt AI att sända (kräver aktiverad Sändare + bekräftelse varje gång)",
                        variable=self.v_send, command=self._send_toggle).pack(side="left")
        self.v_auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="Automatisk övervakning", variable=self.v_auto,
                        command=self._auto_toggle).pack(side="left", padx=16)
        ttk.Button(r2, text="Ny chatt", command=self.new_chat).pack(side="right")
        self.btn_agent = ttk.Button(r2, text="▶ Agentläge (snabbstart)…", style="Accent.TButton",
                                    command=self.quickstart)
        self.btn_agent.pack(side="right", padx=8)
        self.wd = None
        self.auto = None          # AM.AutoMode
        self.bh = AM.BusHealth()
        self._bh_mon = None
        self._wd_next = 0.0
        self.lbl_use = ttk.Label(r2, text="", style="Sub.TLabel")
        self.lbl_use.pack(side="right", padx=10)

        self.txt = tk.Text(f, height=22, wrap="word", font=("Segoe UI", 10))
        self.txt.tag_configure("user", foreground="#58a6ff", font=("Segoe UI", 10, "bold"))
        self.txt.tag_configure("tool", foreground="#8b949e", font=("Consolas", 9))
        self.txt.tag_configure("err", foreground="#da3633")
        self.txt.tag_configure("status", foreground="#2ea043")
        self.txt.tag_configure("alarm", foreground="white", background="#da3633", font=("Segoe UI", 10, "bold"))
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
        self._prov_changed(initial=True)
        self.console = getattr(modbus_tab, "console", None)
        if self.console:
            self.console.ai = self
        self._tick()

    # ------------------------------------------------------------ inställningar
    def prov(self):
        return "cursor" if self.cb_prov.get() == "Cursor" else "openrouter"

    def key(self):
        return CB.get_key(self.app.cfg) if self.prov() == "cursor" else A.get_key(self.app.cfg)

    def _key_label(self):
        k = self.key()
        field = "cursor_api_key" if self.prov() == "cursor" else "api_key"
        src = "config" if ((self.app.cfg.get("ai") or {}).get(field)) else ("miljövariabel" if k else "")
        self.lbl_key.config(text=f"nyckel: {A.mask(k)} {('(' + src + ')') if src else ''}")

    def _prov_changed(self, initial=False):
        ai = self.app.cfg.get("ai") or {}
        if self.prov() == "cursor":
            self.cb_model.config(values=CB.MODELS)
            self.cb_model.set(ai.get("cursor_model") or CB.DEFAULT_MODEL)
            self.e_base.config(state="disabled")
            self.btn_sdk.pack(side="left", padx=4)
        else:
            self.cb_model.config(values=A.MODELS)
            self.cb_model.set(ai.get("model") or A.DEFAULT_MODEL)
            self.e_base.config(state="normal")
            self.btn_sdk.pack_forget()
        if not initial:
            self.agent = None
        self._drop_cursor()
        self._key_label()

    def _drop_cursor(self):
        if self._cclient is not None:
            self._cclient.close()
            self._cclient = None

    def _get_sdk(self):
        def work():
            try:
                CB.load_sdk(C.APP_DIR, progress=lambda m: self._say_ts("tool", m + "\n"))
                self._say_ts("tool", "Cursor-komponenten är klar.\n")
            except Exception as e:
                self._say_ts("err", f"Kunde inte hämta Cursor-komponent: {A.redact(str(e), self.key())}\n")
        threading.Thread(target=work, daemon=True).start()

    def save(self):
        newkey = self.e_key.get().strip()

        def upd(cfg):
            ai = dict(cfg.get("ai") or {})
            ai["provider"] = self.prov()
            if self.prov() == "cursor":
                ai["cursor_model"] = self.cb_model.get().strip() or CB.DEFAULT_MODEL
                if newkey:
                    ai["cursor_api_key"] = newkey
            else:
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
            env = "CURSOR_API_KEY" if self.prov() == "cursor" else "OPENROUTER_API_KEY"
            raise A.LLMError("Ingen API-nyckel. Ange den i fältet API-nyckel och tryck Spara "
                             f"(eller sätt miljövariabeln {env}).")
        model = self.cb_model.get().strip()
        if self.prov() == "cursor":
            c = self._cclient
            if c is None or c._key != k or c.model != model:
                self._drop_cursor()
                c = self._cclient = CB.CursorClient(k, model, C.APP_DIR,
                                                    progress=lambda m: self._say_ts("tool", m + "\n"))
            client = c
        else:
            self._drop_cursor()
            client = A.LLMClient(k, self.e_base.get().strip(), model)
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

    # ------------------------------------------------------------ agentläge
    def quickstart(self):
        if self.wd:
            self.stop_agent_mode()
            return
        mt = self.host.mt
        top = tk.Toplevel(self.f)
        top.title("Modbus RTU-monitor – agentläge")
        top.transient(self.f.winfo_toplevel())
        g = ttk.Frame(top, padding=12)
        g.pack(fill="both", expand=True)
        ports = AM.suggest_ports(M.port_infos())
        ttk.Label(g, text="USB-serial / RS485-adapter").grid(row=0, column=0, sticky="w")
        cb_p = ttk.Combobox(g, width=60, state="readonly", values=[l for l, _ in ports])
        if ports:
            cb_p.current(0)
        cb_p.grid(row=0, column=1, sticky="we", pady=3)
        ttk.Label(g, text="Baud").grid(row=1, column=0, sticky="w")
        cb_b = ttk.Combobox(g, width=12, values=["Auto"] + [str(b) for b in M.BAUDS])
        cb_b.set("Auto")
        cb_b.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(g, text="Fokus-slav(ar) (valfritt)").grid(row=2, column=0, sticky="w")
        e_f = ttk.Entry(g, width=20)
        e_f.grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(g, text="Felbeskrivning (valfritt)").grid(row=3, column=0, sticky="w")
        e_d = ttk.Entry(g, width=60)
        e_d.insert(0, self.e_in.get().strip())
        e_d.grid(row=3, column=1, sticky="we", pady=3)
        ttk.Label(g, text=f"AI: {self.cb_prov.get()} ({self.cb_model.get()}). Paritet/stoppbitar/baud hittas och byts "
                          "automatiskt (bara lyssnaren). Agenten sänder aldrig utan att du tillåtit och bekräftat.",
                  wraplength=520, style="Sub.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=6)
        if not ports:
            ttk.Label(g, text="Ingen serieport hittad – anslut adaptern (drivrutin CH340/FTDI/CP210x).",
                      foreground="#da3633").grid(row=5, column=0, columnspan=2, sticky="w")

        def go():
            if not ports:
                return
            info = ports[cb_p.current()][1]
            focus = [int(x) for x in e_f.get().replace(";", ",").split(",") if x.strip().isdigit()]
            baud = cb_b.get().strip()
            desc = e_d.get().strip() or "Helhetsfelsökning av Modbus RTU-bussen"
            top.destroy()
            self.start_agent_mode(info, None if baud.lower() == "auto" or not baud.isdigit() else int(baud),
                                  focus, desc)
        ttk.Button(g, text="Start", style="Accent.TButton", command=go).grid(row=6, column=1, sticky="e", pady=8)

    def start_agent_mode(self, info, baud, focus, desc):
        try:
            ag = self._get_agent()
        except A.LLMError as e:
            self._say("err", str(e) + "\n")
            return
        mt = self.host.mt
        if self.host.running():
            mt.stop()
        mt.refresh_ports(force=True, prefer=info["device"])
        mt.cb_port.set(M.port_label(info))
        if baud:
            mt.cb_baud.set(str(baud))
        ag.focus = focus
        self.btn_agent.config(text="■ Stoppa agentläge")
        if self.console and not baud:
            self._pending = (ag, desc, focus)
            self.console.start()
            return
        self._say("status", time.strftime("%H:%M ") + f"Agentläge: {info['device']} "
                  f"{AM.chip(info)} – {'baud ' + str(baud) if baud else 'auto-söker linjeinställning'}…\n")
        self.busy = True

        def work():
            try:
                if True:   # passiv sökning av paritet/stopp (och baud om Auto, tar ~1 min) innan start
                    r = self.host.autodetect(1.0, apply=True, baud=baud)
                    self._say_ts("status", time.strftime("%H:%M ") + "Linje: " +
                                 AM.short((r.get("rapport") or r.get("fel") or "").splitlines()[0]
                                          if (r.get("rapport") or r.get("fel")) else "?", 200) +
                                 (f" (vald {r['vald']})" if r.get("vald") else "") + "\n")
                ag.start_auto(desc)
            except Exception as e:
                self._say_ts("err", f"Kunde inte starta: {A.redact(str(e), self.key())}\n")
                self.f.after(0, self.stop_agent_mode)
                return
            finally:
                self.f.after(0, self._done)
            self.f.after(0, lambda: self._wd_begin(ag, focus))
        threading.Thread(target=work, daemon=True).start()

    def after_detect(self):
        """Anropas av konsolen när linjedetekteringen är klar och lyssningen startat."""
        p, self._pending = getattr(self, "_pending", None), None
        if not p:
            return
        ag, desc, focus = p
        ag.start_auto(desc)
        self._wd_begin(ag, focus)

    def _wd_begin(self, ag, focus):
        self.v_auto.set(True)
        self.wd = AM.Watchdog(self.journal, focus=focus)
        self.start_auto()
        self._agent_run(AM.START_PROMPT)

    def auto_minutes(self):
        try:
            return max(1, int((self.app.cfg.get("ai") or {}).get("auto_minutes", 10)))
        except Exception:
            return 10

    def start_auto(self):
        if not self.key():
            return
        if self.auto and self.auto.active():
            self.auto.extend()
        else:
            self.auto = AM.AutoMode(duration_s=60 * self.auto_minutes())
            self.auto.mark()      # startanalysen räknas som första
        self._say("status", time.strftime("%H:%M ") + f"Autoläge på i {self.auto_minutes()} min: analys var 60:e s "
                  "och direkt vid allvarligt fel.\n")

    def stop_agent_mode(self):
        self.wd = None
        self.auto = None
        if self.agent:
            self.agent.stop_auto()
        self.v_auto.set(False)
        self.btn_agent.config(text="▶ Agentläge (snabbstart)…")
        self._say("status", time.strftime("%H:%M ") + "Agentläge stoppat (lyssningen fortsätter).\n")

    def _agent_run(self, prompt):
        ag = self.agent
        if ag is None or ag.client is None or self.busy:
            return
        self.busy = True
        self.btn_ask.state(["disabled"])

        def work():
            try:
                ans = ag.chat(prompt, on_step=lambda s: self.f.after(0, lambda: self._say("tool", s + "\n")))
                self.f.after(0, lambda: self._agent_said(ans))
            except Exception as e:
                msg = A.redact(str(e), self.key())
                self.f.after(0, lambda: self._say("err", "Agent: " + msg + "\n"))
            finally:
                self.f.after(0, self._done)
        threading.Thread(target=work, daemon=True).start()

    def _agent_said(self, ans):
        if AM.is_alarm(ans):
            self._say("alarm", time.strftime("%H:%M ") + ans.strip() + "\n")
            self._alarm(ans)
        else:
            self._say("status", time.strftime("%H:%M ") + ans.strip() + "\n")

    def _alarm(self, text):
        try:
            self.f.bell()
            if sys.platform == "win32":
                import winsound
                winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:
            pass
        n = getattr(self.app, "notify", None)
        if n:
            try:
                n("Modbus-agent: " + AM.short(text, 180))
            except Exception:
                pass
        top = tk.Toplevel(self.f)
        top.title("Modbus-agent: LARM")
        top.attributes("-topmost", True)
        tk.Label(top, text=AM.short(text, 600), wraplength=460, justify="left", fg="white", bg="#da3633",
                 font=("Segoe UI", 11, "bold"), padx=12, pady=12).pack(fill="both", expand=True)
        ttk.Button(top, text="OK", command=top.destroy).pack(pady=6)

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

    def _say_ts(self, tag, s):
        self.f.after(0, lambda: self._say(tag, s))

    def _say(self, tag, s, mirror=True):
        self.txt.insert("end", s, (tag,) if tag else ())
        self.txt.see("end")
        if mirror and getattr(self, "console", None) and tag != "user":
            self.console.write(None if tag == "status" else tag, s)

    def console_input(self, text, focus=None):
        """Fritext från agentkonsolen bredvid paketlistan."""
        self._say("user", "Du (konsol): " + text + "\n")
        if not self.key():
            self.console.write("dim", "Agentdialog kräver nyckel: välj leverantör (Cursor/OpenRouter) och ange "
                                      "API-nyckel i AI-fliken. Linjedetektering och lyssning fungerar ändå.\n")
            return
        try:
            ag = self._get_agent()
        except A.LLMError as e:
            self.console.write("err", str(e) + "\n")
            return
        if focus:
            ag.focus = focus
            if self.wd:
                self.wd.focus = set(focus)
        if AM.wants_monitor(text) and not self.wd:
            if not ag.auto:
                ag.start_auto(text)
            self.v_auto.set(True)
            self.wd = AM.Watchdog(self.journal, focus=getattr(ag, "focus", None))
            self.btn_agent.config(text="■ Stoppa agentläge")
            self.console.write("ok", "Agentläge på: analyserar vid nya fel + kort sammanfattning var 15:e minut.\n")
        if self.busy:
            self.console.write("dim", "Agenten arbetar redan – försök igen strax.\n")
            return
        self._agent_run(text)

    def _tick(self):
        try:
            if not self.f.winfo_exists():
                return
            if self.agent:
                self.agent.tick()
                if self.agent.client:
                    self.lbl_use.config(text=self.agent.usage_text())
                mon = self.host.mon()
                if mon is not None:
                    if self._bh_mon != id(mon):
                        self._bh_mon, self.bh = id(mon), AM.BusHealth()
                    self.bh.sample(mon.stats_rows(), time.time())
                if self.auto and self.agent.client:
                    if not self.auto.active():
                        if not self.auto.expired_told:
                            self.auto.expired_told = True
                            self._say("status", time.strftime("%H:%M ") +
                                      f"Autoläget stängdes av efter {self.auto_minutes()} min. Lokal bevakning "
                                      "fortsätter. Tryck 'Kör 10 min till' för mer AI-analys. Manuella frågor fungerar alltid.\n")
                    elif not self.busy:
                        why = self.auto.due(self.bh if mon is not None else None)
                        if why:
                            self.auto.mark()
                            self._say("status", time.strftime("%H:%M ") + f"Autoanalys ({why})…\n")
                            self._agent_run(AM.AUTO_PROMPT.format(reason=why))
                if self.wd and not self.busy and time.time() >= self._wd_next and not (self.auto and self.auto.active()):
                    self._wd_next = time.time() + 5
                    r = self.wd.check()
                    if r and r[0] == "ai":
                        self._agent_run(r[1])
                    elif r:
                        self._say("status", time.strftime("%H:%M ") + r[1] + "\n")
            else:
                self.journal.update(self.host.mon(), self.host.to_wall)
        except Exception as e:
            C.log(f"ai-flik: {type(e).__name__}")
        self.f.after(1000, self._tick)
