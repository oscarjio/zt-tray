"""
Fliken Seriemonitor: generell seriekonsol (pyserial). Text/hex/båda med tidsstämplar,
radbrytning vid tystnad eller valt tecken, loggning till fil. Sändning är AV som standard
och måste aktiveras uttryckligen (varningsmärkt).
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import config as C
import modbus_monitor as M

LOG_DIR = os.path.join(str(C.APP_DIR), "serie-loggar")
MAX_LINES = 5000


def parse_hex(s):
    s = s.replace("0x", " ").replace(",", " ").replace(":", " ")
    parts = s.split()
    if len(parts) == 1 and len(parts[0]) > 2:
        p = parts[0]
        if len(p) % 2:
            raise ValueError("udda antal hexsiffror")
        parts = [p[i:i + 2] for i in range(0, len(p), 2)]
    return bytes(int(x, 16) for x in parts)


def parse_eol(s):
    """'\\n', '\\r', 'LF', 'CR', '0x03', '03' -> byte eller None."""
    s = (s or "").strip()
    if not s or s.lower() in ("ingen", "-"):
        return None
    m = {"\\n": 10, "lf": 10, "\\r": 13, "cr": 13, "etx": 3, "stx": 2}
    if s.lower() in m:
        return m[s.lower()]
    if len(s) == 1:
        return ord(s)
    return int(s.replace("0x", ""), 16)


class Splitter:
    """Delar en byteström i rader: vid tystnad >= gap_s eller efter eol-byte."""

    def __init__(self, gap_s=0.05, eol=None):
        self.gap_s, self.eol = gap_s, eol
        self.buf = bytearray()
        self.t_first = None
        self.t_last = None

    def feed(self, t, data):
        out = []
        if self.buf and self.t_last is not None and self.gap_s and t - self.t_last >= self.gap_s:
            out.append((self.t_first, bytes(self.buf)))
            self.buf = bytearray()
        for b in data:
            if not self.buf:
                self.t_first = t
            self.buf.append(b)
            if self.eol is not None and b == self.eol:
                out.append((self.t_first, bytes(self.buf)))
                self.buf = bytearray()
        self.t_last = t
        return out

    def flush(self, now):
        if self.buf and self.t_last is not None and self.gap_s and now - self.t_last >= self.gap_s:
            r = (self.t_first, bytes(self.buf))
            self.buf = bytearray()
            return [r]
        return []


def fmt(data, mode):
    hx = data.hex(" ")
    txt = "".join(chr(b) if 32 <= b < 127 else ("" if b in (10, 13) else f"<{b:02X}>") for b in data)
    if mode == "Hex":
        return hx
    if mode == "Text":
        return txt
    return f"{hx}   |{txt}|"


class SerialTab:
    def __init__(self, f, win):
        self.f = f
        self.app = win.app
        sc = self.app.cfg.get("serial") or {}
        self.ser = None
        self.th = None
        self.q = queue.Queue()
        self.stop_ev = threading.Event()
        self.logf = None
        self.t0 = time.perf_counter()
        self.rx = self.tx = 0
        self.splitter = Splitter()

        r1 = ttk.Frame(f)
        r1.pack(fill="x")
        ttk.Label(r1, text="Port").pack(side="left")
        self.cb_port = ttk.Combobox(r1, width=44)
        self.cb_port.pack(side="left", padx=(4, 2))
        ttk.Button(r1, text="⟳", width=3, command=self.refresh_ports).pack(side="left", padx=(0, 10))
        ttk.Label(r1, text="Baud").pack(side="left")
        self.cb_baud = ttk.Combobox(r1, width=7, values=["1200", "2400", "4800", "9600", "19200", "38400",
                                                          "57600", "115200", "230400"])
        self.cb_baud.set(str(sc.get("baud", 19200)))
        self.cb_baud.pack(side="left", padx=(4, 10))
        ttk.Label(r1, text="Paritet").pack(side="left")
        self.cb_par = ttk.Combobox(r1, width=4, state="readonly", values=["N", "E", "O"])
        self.cb_par.set(sc.get("parity", "E"))
        self.cb_par.pack(side="left", padx=(4, 10))
        ttk.Label(r1, text="Stopp").pack(side="left")
        self.cb_stop = ttk.Combobox(r1, width=3, state="readonly", values=["1", "2"])
        self.cb_stop.set(str(sc.get("stop", 1)))
        self.cb_stop.pack(side="left", padx=(4, 10))
        self.btn = ttk.Button(r1, text="Öppna", style="Accent.TButton", command=self.toggle)
        self.btn.pack(side="right")

        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="Visa").pack(side="left")
        self.cb_mode = ttk.Combobox(r2, width=6, state="readonly", values=["Text", "Hex", "Båda"])
        self.cb_mode.set(sc.get("mode", "Båda"))
        self.cb_mode.pack(side="left", padx=(4, 10))
        self.v_ts = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="Tidsstämpel", variable=self.v_ts).pack(side="left")
        ttk.Label(r2, text="Ny rad vid tystnad ms").pack(side="left", padx=(10, 0))
        self.sp_gap = ttk.Spinbox(r2, from_=0, to=5000, increment=5, width=6)
        self.sp_gap.set(sc.get("gap_ms", 20))
        self.sp_gap.pack(side="left", padx=4)
        ttk.Label(r2, text="eller tecken").pack(side="left")
        self.cb_eol = ttk.Combobox(r2, width=6, values=["ingen", "\\n", "\\r", "0x03"])
        self.cb_eol.set(sc.get("eol", "ingen"))
        self.cb_eol.pack(side="left", padx=4)
        self.v_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="Logga till fil", variable=self.v_log).pack(side="left", padx=(10, 0))
        ttk.Button(r2, text="Rensa", command=self.clear).pack(side="right")
        self.v_pause = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="Pausa rullning", variable=self.v_pause).pack(side="right", padx=8)

        self.lbl = ttk.Label(f, text="Stängd. Läser bara – sändning är AV.", style="Sub.TLabel")
        self.lbl.pack(anchor="w", pady=(4, 2))
        tf = ttk.Frame(f)
        tf.pack(fill="both", expand=True)
        self.txt = tk.Text(tf, font=("Consolas", 9), wrap="none")
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=ys.set)
        self.txt.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.txt.tag_configure("tx", foreground="#f0883e")
        self.txt.tag_configure("ts", foreground="#8b949e")
        self.txt.tag_configure("info", foreground="#58a6ff")

        # sändning: av som standard, tydligt märkt
        r3 = ttk.Frame(f)
        r3.pack(fill="x", pady=(6, 0))
        self.v_tx = tk.BooleanVar(value=False)
        self.chk_tx = tk.Checkbutton(r3, text="⚠ AKTIVERA SÄNDNING (stör bussen om PLC:n är master!)",
                                     variable=self.v_tx, command=self._tx_state, fg="#e5534b",
                                     selectcolor="#3a1010", activeforeground="#ff7b72")
        self.chk_tx.pack(side="left")
        self.cb_txmode = ttk.Combobox(r3, width=5, state="readonly", values=["Hex", "Text"])
        self.cb_txmode.set("Hex")
        self.cb_txmode.pack(side="left", padx=6)
        self.ent_tx = ttk.Entry(r3)
        self.ent_tx.pack(side="left", fill="x", expand=True)
        self.ent_tx.bind("<Return>", lambda e: self.send())
        self.v_crc = tk.BooleanVar(value=False)
        self.chk_crc = ttk.Checkbutton(r3, text="+Modbus-CRC", variable=self.v_crc)
        self.chk_crc.pack(side="left", padx=6)
        self.btn_tx = ttk.Button(r3, text="Sänd", command=self.send)
        self.btn_tx.pack(side="left")
        self._tx_state()

        self.refresh_ports(sc.get("port", ""))
        self._last_ports = None
        self._tick = 0
        self._poll()

    # ------------------------------------------------------------ portar
    def refresh_ports(self, prefer=None):
        infos = M.port_infos()
        labels = [M.port_label(i) for i in infos]
        self.cb_port.configure(values=labels)
        cur = M.port_name(self.cb_port.get()) if prefer is None else prefer
        best = M.pick_port(infos, cur)
        if best and not self.ser:
            self.cb_port.set(M.port_label(best))
        elif not infos and not self.ser:
            self.cb_port.set("")
        self._last_ports = [i["device"] for i in infos]

    def _tx_state(self):
        st = "normal" if self.v_tx.get() else "disabled"
        for w in (self.ent_tx, self.btn_tx, self.cb_txmode, self.chk_crc):
            w.configure(state="readonly" if (w is self.cb_txmode and st == "normal") else st)
        self.lbl.configure(text=self._status())

    def _status(self):
        s = f"Öppen {self.ser.port} {self.ser.baudrate} 8{self.ser.parity}{self.ser.stopbits}" if self.ser else "Stängd"
        s += f"   RX {self.rx} byte   TX {self.tx} byte"
        s += "   ⚠ SÄNDNING PÅ" if self.v_tx.get() else "   Läser bara – sändning är AV"
        if self.logf:
            s += f"   loggar: {self.logf.name}"
        return s

    def save_cfg(self):
        def upd(cfg):
            cfg["serial"] = {"port": M.port_name(self.cb_port.get()), "baud": int(self.cb_baud.get()),
                             "parity": self.cb_par.get(), "stop": int(self.cb_stop.get()),
                             "mode": self.cb_mode.get(), "gap_ms": float(self.sp_gap.get() or 0),
                             "eol": self.cb_eol.get()}
        try:
            self.app.update_cfg(upd)
        except Exception as e:
            C.log(f"serie: kunde inte spara: {e!r}")

    # ------------------------------------------------------------ öppna/stäng
    def toggle(self):
        if self.ser:
            self.close()
            return
        try:
            import serial
        except ImportError:
            messagebox.showerror("Seriemonitor", "pyserial saknas.", parent=self.f)
            return
        port = M.port_name(self.cb_port.get())
        try:
            self.splitter = Splitter(float(self.sp_gap.get() or 0) / 1000, parse_eol(self.cb_eol.get()))
            kw = dict(baudrate=int(self.cb_baud.get()), bytesize=8, parity=self.cb_par.get(),
                      stopbits=int(self.cb_stop.get()), timeout=0.02)
            self.ser = M.open_with_timeout(lambda: serial.Serial(port, **kw), what=port)
        except Exception as e:
            self.ser = None
            messagebox.showerror("Seriemonitor", M.explain_open_error(port, e) + "\n\n" + f"Kan inte öppna {port}:\n{e}\n\n"
                                 "Är porten upptagen (t.ex. av Modbus-fliken eller annat program)?", parent=self.f)
            return
        self.save_cfg()
        if self.v_log.get():
            os.makedirs(LOG_DIR, exist_ok=True)
            self.logf = open(os.path.join(LOG_DIR, time.strftime(f"serie_{port}_%Y%m%d-%H%M%S.txt")),
                             "a", encoding="utf-8")
        self.rx = self.tx = 0
        self.t0 = time.perf_counter()
        self.stop_ev.clear()
        self.th = threading.Thread(target=self._reader, daemon=True)
        self.th.start()
        self.btn.config(text="Stäng")
        self._append("info", f"--- öppnad {port} {self.ser.baudrate} 8{self.ser.parity}{self.ser.stopbits} "
                             f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    def close(self):
        self.stop_ev.set()
        ser, self.ser = self.ser, None
        if ser:
            try:
                ser.close()
            except Exception:
                pass
        if self.logf:
            try:
                self.logf.close()
            except Exception:
                pass
            self.logf = None
        self.btn.config(text="Öppna")
        self._append("info", "--- stängd ---")

    def _reader(self):
        ser = self.ser
        while not self.stop_ev.is_set():
            try:
                data = ser.read(max(1, ser.in_waiting or 1))
            except Exception as e:
                self.q.put(("err", time.perf_counter(), str(e)))
                return
            if data:
                self.q.put(("rx", time.perf_counter(), data))

    # ------------------------------------------------------------ sänd
    def send(self):
        if not (self.v_tx.get() and self.ser):
            return
        s = self.ent_tx.get()
        try:
            data = parse_hex(s) if self.cb_txmode.get() == "Hex" else s.encode("latin-1").decode("unicode_escape").encode("latin-1")
        except Exception as e:
            messagebox.showerror("Seriemonitor", f"Ogiltig data: {e}", parent=self.f)
            return
        if self.v_crc.get():
            c = M.crc16(data)
            data += bytes((c & 0xFF, c >> 8))
        try:
            self.ser.write(data)
        except Exception as e:
            messagebox.showerror("Seriemonitor", f"Sändfel: {e}", parent=self.f)
            return
        self.tx += len(data)
        self._line(time.perf_counter(), data, "tx")

    # ------------------------------------------------------------ visning
    def clear(self):
        self.txt.delete("1.0", "end")

    def _append(self, tag, s):
        self.txt.insert("end", s + "\n", tag)
        if self.logf:
            self.logf.write(s + "\n")
            self.logf.flush()

    def _line(self, t, data, tag=""):
        pre = ""
        if self.v_ts.get():
            pre = f"{time.strftime('%H:%M:%S')} {t - self.t0:10.4f} "
        arrow = "TX> " if tag == "tx" else "RX< "
        body = fmt(data, self.cb_mode.get())
        if pre:
            self.txt.insert("end", pre, "ts")
        self.txt.insert("end", arrow + body + "\n", tag)
        if self.logf:
            self.logf.write(pre + arrow + data.hex(" ") + "   |" + fmt(data, "Text") + "|\n")
            self.logf.flush()

    def _poll(self):
        try:
            if not self.f.winfo_exists():
                self.close()
                return
        except Exception:
            return
        try:
            n = 0
            while n < 2000:
                try:
                    kind, t, d = self.q.get_nowait()
                except queue.Empty:
                    break
                n += 1
                if kind == "err":
                    self.close()
                    messagebox.showerror("Seriemonitor", f"Porten stängdes: {d}", parent=self.f)
                    break
                self.rx += len(d)
                for ts, chunk in self.splitter.feed(t, d):
                    self._line(ts, chunk)
            for ts, chunk in self.splitter.flush(time.perf_counter()):
                self._line(ts, chunk)
            lines = int(self.txt.index("end-1c").split(".")[0])
            if lines > MAX_LINES:
                self.txt.delete("1.0", f"{lines - MAX_LINES}.0")
            if n and not self.v_pause.get():
                self.txt.see("end")
            self._tick += 1
            if self._tick % 10 == 0:
                if not self.ser:
                    devs = [i["device"] for i in M.port_infos()]
                    if devs != self._last_ports:
                        self.refresh_ports()
                self.lbl.configure(text=self._status())
        except Exception as e:
            C.log(f"serie-flik: {e!r}")
        self.f.after(100, self._poll)

    def shutdown(self):
        self.close()
