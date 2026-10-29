"""
Agentkonsol bredvid paketlistan (mörk, monospace, inmatning längst ner, streamad text).

Start: lokal stegvis linjedetektering (fungerar utan nyckel), sedan lyssning. Fritext går till AI-agenten
(Cursor/OpenRouter via AI-fliken) - lokala kommandon som "fokusera på adress 5" fungerar även utan nyckel.
"""

import threading
import time
import tkinter as tk
from tkinter import ttk

import agent_mode as AM
import linecheck as L
import modbus_monitor as M

BG, FG, DIM, ACC, OK, ERR = "#0d1117", "#c9d1d9", "#8b949e", "#58a6ff", "#3fb950", "#f85149"


class AgentConsole:
    def __init__(self, parent, mt):
        self.mt, self.ai = mt, None
        self.f = tk.Frame(parent, bg=BG)
        hdr = tk.Frame(self.f, bg=BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="◆ Modbus-agent", bg=BG, fg=ACC, font=("Consolas", 10, "bold")).pack(side="left", padx=6, pady=4)
        self.btn = tk.Button(hdr, text="▶ Start", bg="#238636", fg="white", relief="flat", font=("Consolas", 9, "bold"),
                             activebackground="#2ea043", command=self.start)
        self.btn.pack(side="right", padx=6, pady=3)
        self.txt = tk.Text(self.f, bg=BG, fg=FG, insertbackground=FG, font=("Consolas", 10), wrap="word",
                           relief="flat", padx=8, pady=6, width=48, state="disabled")
        for t, c in (("dim", DIM), ("user", ACC), ("ok", OK), ("err", ERR), ("tool", DIM)):
            self.txt.tag_configure(t, foreground=c)
        self.txt.tag_configure("alarm", foreground="white", background="#b62324", font=("Consolas", 10, "bold"))
        self.txt.pack(fill="both", expand=True)
        row = tk.Frame(self.f, bg="#161b22")
        row.pack(fill="x")
        tk.Label(row, text="›", bg="#161b22", fg=ACC, font=("Consolas", 12, "bold")).pack(side="left", padx=(8, 2))
        self.ent = tk.Entry(row, bg="#161b22", fg=FG, insertbackground=FG, relief="flat", font=("Consolas", 10))
        self.ent.pack(side="left", fill="x", expand=True, ipady=6)
        self.ent.bind("<Return>", lambda e: self.submit())
        self.listeners = []   # t.ex. startskärmen: cb(tag, text)
        self._q = []          # streamkö: (tag, text)
        self._streaming = False
        self.started = False
        self.write("dim", "Tryck ▶ Start (eller skriv 'start'). Linjeinställningen hittas automatiskt.\n", stream=False)

    # ------------------------------------------------------------ utskrift
    def write(self, tag, s, stream=True):
        if threading.current_thread() is not threading.main_thread():
            self.f.after(0, lambda: self.write(tag, s, stream))
            return
        for cb in self.listeners:
            try:
                cb(tag, s)
            except Exception:
                pass
        if not stream or tag in ("tool", "user", "dim"):
            self._q.append((tag, s, False))
        else:
            self._q.append((tag, s, True))
        if not self._streaming:
            self._pump()

    def _pump(self):
        if not self._q:
            self._streaming = False
            return
        self._streaming = True
        tag, s, stream = self._q[0]
        n = 6 if stream and len(self._q) < 4 else len(s)
        part, rest = s[:n], s[n:]
        self.txt.config(state="normal")
        self.txt.insert("end", part, (tag,) if tag else ())
        self.txt.see("end")
        self.txt.config(state="disabled")
        if rest:
            self._q[0] = (tag, rest, stream)
        else:
            self._q.pop(0)
        self.f.after(12 if stream else 1, self._pump)

    # ------------------------------------------------------------ start
    def start(self):
        if self.started and self.mt.cap is not None:
            self.write("dim", "Lyssnar redan.\n")
            return
        port = M.port_name(self.mt.cb_port.get())
        if not port:
            self.mt.refresh_ports(force=True)
            port = M.port_name(self.mt.cb_port.get())
        if not port:
            self.write("err", "Ingen serieport hittad – anslut USB-RS485-adaptern (CH340/FTDI/CP210x).\n")
            return
        info = next((i for i in M.port_infos() if i["device"] == port), {"device": port})
        self.write("ok", f"Port {port} {AM.chip(info)}\n".replace(" \n", "\n"))
        self.btn.config(state="disabled")
        if self.mt.cap is not None:
            self.mt.stop()
        threading.Thread(target=self._detect, args=(port,), daemon=True).start()

    def _detect(self, port):
        def scan(bauds, pars, stops, sec):
            return L.live_scan(port, sec, bauds=bauds, parities=pars, stops=stops)

        def say(s, end="\n"):
            self.write("ok" if not s.startswith(("Söker", "Kollar")) else None, s + end)
        try:
            res = AM.stepwise_detect(scan, say)
        except Exception as e:
            res = None
            self.write("err", f"Detektering misslyckades: {e}\n")
        self.f.after(0, lambda: self._detected(res))

    def _detected(self, res):
        self.btn.config(state="normal")
        if res:
            b, p, s = res
            self.mt.cb_baud.set(str(b))
            self.mt.cb_par.set(p)
            self.mt.cb_stop.set(str(s))
        else:
            self.write("dim", "Startar ändå med nuvarande inställning – kontrollera A/B och att bussen är aktiv.\n")
        if self.mt.cap is None:
            self.mt.toggle()
        self.started = True
        self.btn.config(text="↻ Sök igen")
        self.write(None, "Börjar läsa trafiken. Vad kan jag hjälpa dig med?\n")
        if self.ai:
            try:
                self.ai.after_detect()
            except Exception as e:
                self.write("err", f"Agentläge: {e}\n")
        if self.ai and not self.ai.key():
            self.write("dim", "(Agentdialog kräver Cursor- eller OpenRouter-nyckel i AI-fliken. "
                              "Lokala kommandon: 'fokusera på adress 5', 'status'.)\n")

    # ------------------------------------------------------------ inmatning
    def submit(self):
        text = self.ent.get().strip()
        if not text:
            return
        self.ent.delete(0, "end")
        self.write("user", "› " + text + "\n")
        low = text.lower()
        if low in ("start", "starta", "kör"):
            return self.start()
        focus = AM.parse_focus(text)
        if focus:
            try:
                self.mt.pv.set_filter(" or ".join(f"addr=={a}" for a in focus))
            except Exception:
                pass
            self.write("ok", f"Fokus: adress {', '.join(map(str, focus))} (paketlistan filtrerad).\n")
        if low in ("status", "läge"):
            st = self._status()
            self.write("dim", st + "\n")
            return
        if self.ai is None:
            self.write("err", "AI-fliken är inte laddad.\n")
            return
        self.ai.console_input(text, focus)

    def _status(self):
        mon = self.mt.mon
        if not mon:
            return "Lyssnar inte."
        b, p, s = self.mt.line()
        return (f"{M.port_name(self.mt.cb_port.get())} {b} 8{p}{s}: {mon.crc_ok_frames} giltiga ramar, "
                f"{mon.bad_frames} trasiga, UART-fel {sum(mon.line_err_total.values())}.")
