"""
QA-rigg: riktiga huvudfönstret (Start/Modbus/Sändare/AI-flikarna ur ui.MainWindow) mot en virtuell
RS485-buss (qa_bus). Kör från källkod, rör inte installerad zt-tray, sänder aldrig på riktiga portar.

  python qa\\qa_harness.py e2e   [utkatalog]      -> 3 linjeinställningar, urkoppling, upptagen port, skärmdumpar
  python qa\\qa_harness.py long  [minuter] [utkatalog]  -> långkörning med hög trafik, mäter CPU/minne/GUI-latens
"""
import ctypes
import json
import os
import sys
import time
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

import config as C           # noqa: E402
import modbus_monitor as M   # noqa: E402
import qa_bus as Q           # noqa: E402

BUS = {}
Q.install(BUS)

TWIN1 = {1: {"both": 0.25, "after": 0.25, "only": 0.3, "delay": 0.012}}


def scenario_faults():
    f = dict(M.DEMO_FAULTS)
    f["stop"] = {5: 6.0}          # adress 5 tystnar 6 s in i varje 10 s-segment
    f["twin"] = TWIN1             # två enheter på adress 1
    return f


class FakeApp:
    def __init__(self, cfg=None):
        self.cfg = cfg or {"modbus": {"log": True, "names": "1=Etikett, 2=Våg, 3=Motor 3, 4=Motor 4, 5=Skrivare, 6=IO"},
                           "ai": {}}

    def update_cfg(self, fn):
        fn(self.cfg)

    def reload_cfg(self):
        pass


def build(cfg=None):
    import ui
    root = tk.Tk()
    root.withdraw()
    try:
        import sv_ttk
        sv_ttk.set_theme("dark")
    except Exception:
        pass
    st = ttk.Style()
    st.configure("Title.TLabel", font=("Segoe UI Semibold", 15))
    st.configure("Sub.TLabel", font=("Segoe UI", 9), foreground="#8a8a8a")
    st.configure("Big.TLabel", font=("Segoe UI Semibold", 12))

    class QAUi:
        pass
    q = QAUi()
    q.app, q.root, q.dark = FakeApp(cfg), root, True

    class QAWin(ui.MainWindow):
        def _stub(self, f):
            ttk.Label(f, text="(ej med i QA-riggen)").pack()
        _build_quick = _build_discover = _build_presets = _build_zt = _build_uttag = _stub
        _build_serial = _build_settings = _stub

        def _tick(self):
            pass
    w = QAWin(q)
    w.win.title(f"ZT-Tray {C.APP_VERSION}  [QA – virtuell buss]")
    w.win.geometry("1500x950+20+20")
    return root, w


# ------------------------------------------------------------------ mätning (Windows, utan psutil)
class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


def _proc():
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    return ctypes.c_void_p(k.GetCurrentProcess())


def mem_mb():
    try:
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        h = _proc()
        ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
        return round(pmc.PrivateUsage / 1e6, 1), round(pmc.WorkingSetSize / 1e6, 1)
    except Exception:
        return None, None


def cpu_s():
    t = os.times()
    return t.user + t.system


def gdi_user():
    try:
        h = _proc()
        return ctypes.windll.user32.GetGuiResources(h, 0), ctypes.windll.user32.GetGuiResources(h, 1)
    except Exception:
        return None, None


def shot(w, path):
    try:
        from PIL import ImageGrab
        w.win.attributes("-topmost", True)
        w.win.lift()
        w.win.update()
        time.sleep(0.4)
        w.win.update()
        x, y = w.win.winfo_rootx(), w.win.winfo_rooty()
        full = ImageGrab.grab()   # fysiska pixlar; appen är DPI-omedveten (logiska koord.)
        k = full.width / max(1, ctypes.windll.user32.GetSystemMetrics(0))
        im = full.crop(tuple(int(v * k) for v in (x, y, x + w.win.winfo_width(), y + w.win.winfo_height())))
        im.save(path)
        w.win.attributes("-topmost", False)
        return path
    except Exception as e:
        return f"skärmdump misslyckades: {e!r}"


class Heart:
    """GUI-latens: hur sent ett after(100) faktiskt körs."""

    def __init__(self, root):
        self.root, self.max, self.n, self.sum = root, 0.0, 0, 0.0
        self._next = time.perf_counter() + 0.1
        root.after(100, self._beat)

    def _beat(self):
        now = time.perf_counter()
        late = max(0.0, now - self._next)
        self.max, self.n, self.sum = max(self.max, late), self.n + 1, self.sum + late
        self._next = now + 0.1
        self.root.after(100, self._beat)

    def take(self):
        r = (round(self.max * 1000, 1), round(self.sum / max(1, self.n) * 1000, 1))
        self.max, self.n, self.sum = 0.0, 0, 0.0
        return r


def state(w):
    stt, mt = w.start_tab, w.modbus
    rows = [stt.tv.item(i, "values") for i in stt.tv.get_children()]
    return {"banner": stt.banner.cget("text"), "detalj": stt.lbl_det.cget("text"),
            "linje": (mt.mon.baud, mt.mon.parity, mt.mon.stop) if mt.mon else None,
            "kör": mt.cap is not None, "tappad": bool(getattr(mt, "_lost", None)),
            "adressrader": [list(r) for r in rows],
            "stopp": [str(s)[:160] for s in getattr(mt, "stops", [])][:5],
            "konsol": mt.console.txt.get("1.0", "end")[-1500:] if mt.console else "",
            "paket_pv": len(mt.pv.events), "paket_rader": len(mt.pv.tv.get_children()),
            "hälsa": stt.lbl_h.cget("text") if hasattr(stt, "lbl_h") else ""}


# ------------------------------------------------------------------ E2E
def run_e2e(out):
    os.makedirs(out, exist_ok=True)
    results = {}
    lines = [(57600, "E", 1), (19200, "N", 2), (9600, "O", 1)]
    BUS["hidden"] = True       # ingen adapter vid start -> testar "vänta på adapter"
    BUS["bus"] = Q.VirtualBus(lines[0], faults=scenario_faults())
    root, w = build()
    w.show("start")
    hb = Heart(root)
    steps = []
    log = []

    def at(sec, fn):
        steps.append((sec, fn))

    def note(k, v):
        results[k] = v
        log.append(f"{time.strftime('%H:%M:%S')} {k}: {json.dumps(v, ensure_ascii=False)[:600]}")
        print(log[-1], flush=True)

    T = 0
    # 1) programmet startat utan adapter -> adaptern kopplas in -> autostart
    at(T + 4, lambda: note("utan_adapter", state(w)["detalj"]))
    at(T + 5, lambda: BUS.__setitem__("hidden", False))
    T += 5
    for n, ln in enumerate(lines):
        if n > 0:
            def switch(ln=ln):
                w.modbus.stop()
                BUS["bus"] = Q.VirtualBus(ln, faults=scenario_faults())
                w.start_tab.start()
            at(T, switch)
        at(T + 75, lambda ln=ln: note(f"läge_{ln[0]}_8{ln[1]}{ln[2]}", state(w)))
        at(T + 76, lambda ln=ln: note(f"skärmdump_{ln[0]}", shot(w, os.path.join(out, f"start_{ln[0]}_8{ln[1]}{ln[2]}.png"))))
        T += 80
    # 2) urkoppling / återinkoppling
    at(T, lambda: BUS["bus"].__setattr__("unplugged", True))
    at(T + 6, lambda: note("urkopplad", {k: state(w)[k] for k in ("banner", "detalj", "kör", "tappad")}))
    at(T + 7, lambda: note("skärmdump_urkopplad", shot(w, os.path.join(out, "start_urkopplad.png"))))
    at(T + 8, lambda: BUS["bus"].__setattr__("unplugged", False))
    at(T + 16, lambda: note("återansluten", {k: state(w)[k] for k in ("banner", "kör", "tappad", "konsol")}))
    T += 18
    # 3) upptagen port
    def busy():
        w.modbus.stop()
        BUS["bus"].busy = True
        w.start_tab.start()
    at(T, busy)
    at(T + 4, lambda: note("upptagen", state(w)["konsol"][-400:]))
    at(T + 5, lambda: BUS["bus"].__setattr__("busy", False))
    # 4) sändarlåset: Sändare får inte sända utan aktivering
    def lock():
        ms = w.master_tab
        note("sändarlås", {"armed": ms.armed, "skrivet_på_bussen": len(BUS["bus"].written)})
    at(T + 6, lock)
    at(T + 7, lambda: note("gui_latens_ms_max_medel", hb.take()))
    at(T + 8, lambda: root.quit())
    t0 = time.time()
    for sec, fn in steps:
        root.after(int(sec * 1000), fn)
    root.after(int((T + 60) * 1000), root.quit)
    root.mainloop()
    note("tid_s", round(time.time() - t0))
    with open(os.path.join(out, "e2e-resultat.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "e2e-logg.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(log))
    try:
        w.close()
        root.destroy()
    except Exception:
        pass
    return results


# ------------------------------------------------------------------ långkörning
def run_long(minutes, out):
    os.makedirs(out, exist_ok=True)
    slaves = tuple(range(1, 21))
    BUS["bus"] = Q.VirtualBus((115200, "E", 1), faults=scenario_faults(), slaves=slaves, poll_gap=0.002, seg_s=10)
    root, w = build()
    w.show("start")
    hb = Heart(root)
    rows = []
    c0, t0 = cpu_s(), time.time()
    last = [c0, t0]

    def sample():
        c, t = cpu_s(), time.time()
        cpu = (c - last[0]) / max(1e-6, t - last[1]) * 100
        last[:] = [c, t]
        mt = w.modbus
        mon = mt.mon
        priv, ws = mem_mb()
        gdi, usr = gdi_user()
        lat = hb.take()
        r = {"min": round((t - t0) / 60, 1), "cpu_pct": round(cpu, 1), "privat_mb": priv, "ws_mb": ws,
             "gdi": gdi, "user": usr, "gui_lat_max_ms": lat[0], "gui_lat_medel_ms": lat[1],
             "ramar": mon.frames_total if mon else 0, "byte": mon.bytes_total if mon else 0,
             "pv_events": len(mt.pv.events), "pv_i_minnet": len(mt.pv.events.items) if hasattr(mt.pv.events, "items") else None,
             "mon_events": len(mon.events) if mon else 0, "linje": (mon.baud, mon.parity, mon.stop) if mon else None,
             "banner": w.start_tab.banner.cget("text")[:80]}
        rows.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)
        with open(os.path.join(out, "lang-korning.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if t - t0 < minutes * 60:
            root.after(30000, sample)
        else:
            shot(w, os.path.join(out, f"start_efter_{minutes}min.png"))
            root.quit()
    root.after(30000, sample)
    root.mainloop()
    return rows


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "e2e"
    if mode == "e2e":
        run_e2e(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "ut"))
    else:
        run_long(float(sys.argv[2]) if len(sys.argv) > 2 else 30, sys.argv[3] if len(sys.argv) > 3 else os.path.join(HERE, "ut"))
