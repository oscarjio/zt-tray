"""
Tk-gränssnitt. Allt Tk körs i EN egen tråd (UiThread); övriga trådar ber om
fönster via ui.call(fn). Det gör att popup-rutorna kommer upp direkt.
"""

import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import config as C
import icons
import winint

NPCAP_URL = "https://npcap.com/#download"

MODE_TEXT = {
    "static": "Statisk IP",
    "dhcp": "DHCP",
    "apipa": "DHCP – ingen server svarar",
    "down": "Ingen kabel / länk nere",
    "none": "Ingen IP",
    "unknown": "Inget kort valt",
}


class UiThread:
    def __init__(self, app):
        self.app = app
        self.q = queue.Queue()
        self.root = None
        self.ready = threading.Event()
        self.main_win = None
        self.dark = winint.is_dark_mode()
        threading.Thread(target=self._run, daemon=True, name="tk").start()
        self.ready.wait(10)

    def call(self, fn, *a, **kw):
        self.q.put((fn, a, kw))

    def _run(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(C.APP_NAME)
        try:
            import sv_ttk
            sv_ttk.set_theme("dark" if self.dark else "light")
        except Exception:
            try:
                ttk.Style().theme_use("vista")
            except Exception:
                pass
        st = ttk.Style()
        base = ("Segoe UI", 10)
        st.configure("Title.TLabel", font=("Segoe UI Semibold", 15))
        st.configure("Sub.TLabel", font=("Segoe UI", 9), foreground="#8a8a8a")
        st.configure("Big.TLabel", font=("Segoe UI Semibold", 12))
        st.configure("Preset.TButton", font=base, padding=(10, 10))
        st.configure("Preset.Accent.TButton", font=base, padding=(10, 10))
        self._icon_ref = None
        try:
            from PIL import ImageTk
            self._icon_ref = ImageTk.PhotoImage(icons.render("dhcp", size=64))
            self.root.iconphoto(True, self._icon_ref)
        except Exception:
            pass
        self.ready.set()
        self._poll()
        self.root.mainloop()

    def _poll(self):
        try:
            while True:
                fn, a, kw = self.q.get_nowait()
                try:
                    fn(*a, **kw)
                except Exception as e:
                    C.log(f"UI-fel: {e!r}")
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    # ---------------------------------------------------------- publika

    def open_main(self, tab=None):
        self.call(self._open_main, tab)

    def _open_main(self, tab):
        if self.main_win is None or not self.main_win.alive():
            self.main_win = MainWindow(self)
        self.main_win.show(tab)

    def ask_parts(self, preset, callback):
        self.call(lambda: AskPartsDialog(self, preset, callback))

    def manual_ip(self):
        self.call(lambda: ManualIpDialog(self))

    def info(self, title, msg):
        self.call(lambda: messagebox.showinfo(title, msg, parent=self._top()))

    def _top(self):
        if self.main_win and self.main_win.alive():
            return self.main_win.win
        return self.root

    def place_near_pointer(self, win, w, h):
        win.update_idletasks()
        px, py = win.winfo_pointerxy()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = min(max(px - w // 2, 8), sw - w - 8)
        y = min(max(py - h - 24, 8), sh - h - 56)
        win.geometry(f"{w}x{h}+{x}+{y}")


# ================================================================ dialoger

class AskPartsDialog:
    """Liten ruta som frågar efter '?'-oktetterna. Enter = byt direkt."""

    def __init__(self, ui, preset, callback):
        self.ui, self.preset, self.cb = ui, preset, callback
        app = ui.app
        parts = C.preset_parts(preset)
        qs = C.preset_questions(preset)
        win = self.win = tk.Toplevel(ui.root)
        win.title(preset.get("name", "IP"))
        win.attributes("-topmost", True)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=preset.get("name", ""), style="Big.TLabel").pack(anchor="w")
        ttk.Label(frm, text=f"på {app.iface_name or '?'}", style="Sub.TLabel").pack(anchor="w", pady=(0, 10))

        row = ttk.Frame(frm)
        row.pack(anchor="w")
        self.entries = {}
        last = app.cfg.get("last_values", {})
        first = None
        for i, p in enumerate(parts):
            if i:
                ttk.Label(row, text=".", font=("Segoe UI Semibold", 14)).pack(side="left")
            if p == "?":
                e = ttk.Entry(row, width=4, justify="center", font=("Segoe UI Semibold", 13))
                v = last.get(f"{preset.get('name')}#{i}", "")
                e.insert(0, str(v))
                e.pack(side="left", padx=2)
                self.entries[i] = e
                first = first or e
            else:
                ttk.Label(row, text=p, font=("Segoe UI Semibold", 14)).pack(side="left", padx=2)
        ttk.Label(row, text=f"  /{preset.get('prefix', 24)}", style="Sub.TLabel").pack(side="left")

        self.err = ttk.Label(frm, text="", foreground="#d9534f")
        self.err.pack(anchor="w", pady=(6, 0))

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Byt", style="Accent.TButton", command=self.ok).pack(side="right")
        ttk.Button(btns, text="Avbryt", command=win.destroy).pack(side="right", padx=6)

        win.bind("<Return>", lambda e: self.ok())
        win.bind("<KP_Enter>", lambda e: self.ok())
        win.bind("<Escape>", lambda e: win.destroy())
        ui.place_near_pointer(win, 300, 190)
        win.lift()
        win.focus_force()
        if first:
            first.focus_set()
            first.select_range(0, "end")

    def ok(self):
        answers = {i: e.get().strip() for i, e in self.entries.items()}
        try:
            ip, mask, gw = C.resolve_preset(self.preset, answers)
        except ValueError as ex:
            self.err.config(text=str(ex))
            return
        self.win.destroy()
        self.cb(answers, ip, mask, gw)


class ManualIpDialog:
    def __init__(self, ui):
        self.ui = ui
        app = ui.app
        win = self.win = tk.Toplevel(ui.root)
        win.title("Manuell IP")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Manuell IP", style="Big.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, text=f"på {app.iface_name or '?'}", style="Sub.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))
        last = app.cfg.get("last_values", {})
        self.ip = self._row(frm, 2, "IP-adress", last.get("manual#ip", ""))
        self.mask = self._row(frm, 3, "Mask / prefix", last.get("manual#mask", "24"))
        self.gw = self._row(frm, 4, "Gateway (valfri)", last.get("manual#gw", ""))
        self.err = ttk.Label(frm, text="", foreground="#d9534f")
        self.err.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        b = ttk.Frame(frm)
        b.grid(row=6, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Avbryt", command=win.destroy).pack(side="left", padx=6)
        ttk.Button(b, text="Byt", style="Accent.TButton", command=self.ok).pack(side="left")
        win.bind("<Return>", lambda e: self.ok())
        win.bind("<Escape>", lambda e: win.destroy())
        ui.place_near_pointer(win, 340, 270)
        win.lift()
        win.focus_force()
        self.ip.focus_set()
        self.ip.select_range(0, "end")

    def _row(self, frm, r, label, val):
        ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=3)
        e = ttk.Entry(frm, width=20)
        e.insert(0, val)
        e.grid(row=r, column=1, sticky="we", pady=3, padx=(10, 0))
        return e

    def ok(self):
        import ipaddress
        ip, m, gw = self.ip.get().strip(), self.mask.get().strip(), self.gw.get().strip()
        try:
            ipaddress.IPv4Address(ip)
            mask = C.prefix_to_mask(m.lstrip("/")) if m.lstrip("/").isdigit() else str(ipaddress.IPv4Address(m))
            C.mask_to_prefix(mask)
            if gw:
                ipaddress.IPv4Address(gw)
        except Exception as ex:
            self.err.config(text=f"Ogiltigt: {ex}")
            return
        self.ui.app.remember({"manual#ip": ip, "manual#mask": m, "manual#gw": gw})
        self.win.destroy()
        self.ui.app.apply_static(ip, mask, gw, label="Manuell")


# ================================================================ huvudfönster

class MainWindow:
    TABS = ("quick", "discover", "presets", "zt", "uttag", "modbus", "settings")

    def __init__(self, ui):
        self.ui = ui
        self.app = ui.app
        win = self.win = tk.Toplevel(ui.root)
        win.title(f"ZT-Tray {C.APP_VERSION}")
        win.geometry("820x720")
        win.minsize(720, 600)
        win.protocol("WM_DELETE_WINDOW", self.close)

        # --- statusrad överst
        head = ttk.Frame(win, padding=(18, 14, 18, 6))
        head.pack(fill="x")
        self.status_canvas = tk.Canvas(head, width=44, height=44, highlightthickness=0, bd=0)
        self.status_canvas.pack(side="left")
        txt = ttk.Frame(head)
        txt.pack(side="left", padx=12)
        self.lbl_status = ttk.Label(txt, text="", style="Title.TLabel")
        self.lbl_status.pack(anchor="w")
        self.lbl_sub = ttk.Label(txt, text="", style="Sub.TLabel")
        self.lbl_sub.pack(anchor="w")
        self._status_img = None

        self.nb = ttk.Notebook(win)
        self.nb.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        self.tabs = {}
        for key, title, builder in (
            ("quick", "Snabbyte", self._build_quick),
            ("discover", "Hitta PLC", self._build_discover),
            ("presets", "IP-profiler", self._build_presets),
            ("zt", "ZeroTier", self._build_zt),
            ("uttag", "Uttag", self._build_uttag),
            ("modbus", "Modbus", self._build_modbus),
            ("settings", "Inställningar", self._build_settings),
        ):
            f = ttk.Frame(self.nb, padding=14)
            self.nb.add(f, text=title)
            self.tabs[key] = f
            builder(f)

        self._alive = True
        self._tick()

    def alive(self):
        try:
            return self._alive and bool(self.win.winfo_exists())
        except Exception:
            return False

    def show(self, tab=None):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        if tab in self.tabs:
            self.nb.select(self.tabs[tab])
            if tab == "discover" and self.disc_ok:
                self.start_discovery()

    def close(self):
        self._alive = False
        try:
            if getattr(self, "modbus", None):
                self.modbus.stop()
        except Exception:
            pass
        self.win.destroy()

    # ---------------------------------------------------------- status

    def _tick(self):
        if not self.alive():
            return
        s = self.app.snapshot()
        try:
            from PIL import ImageTk
            img = icons.render(s["mode"], s["vpn"], s["busy"], size=44,
                               share=s.get("share") or "", live=bool(s.get("share_live")))
            self._status_img = ImageTk.PhotoImage(img)
            self.status_canvas.delete("all")
            self.status_canvas.create_image(22, 22, image=self._status_img)
        except Exception:
            pass
        ip = f"  {s['ip']}/{s['prefix']}" if s["ip"] else ""
        share = s.get("share") or ""
        if share:
            kind = {"net": "Nätuttag", "usb": "USB-uttag", "com": "COM-uttag"}.get(share, "Uttag")
            state = "igång" if s.get("share_live") else "väntar"
            self.lbl_status.config(text=f"{kind} {state}")
        else:
            self.lbl_status.config(text=f"{MODE_TEXT.get(s['mode'], s['mode'])}{ip}")
        sub = f"{s['iface'] or 'Inget kort valt'}"
        if share:
            nic = MODE_TEXT.get(s["mode"], s["mode"])
            sub = f"{nic}{ip}  ·  {sub}"
        if s.get("desc"):
            sub += f"  ·  {s['desc']}"
        if s["vpn"]:
            sub += f"   ·   VPN: ZeroTier → {s['site']}"
        self.lbl_sub.config(text=sub)
        self._refresh_quick(s)
        self._refresh_uttag()
        self.win.after(1000, self._tick)

    # ---------------------------------------------------------- Snabbyte

    def _build_quick(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Nätverkskort").pack(side="left")
        self.cmb_iface = ttk.Combobox(top, state="readonly", width=46)
        self.cmb_iface.pack(side="left", padx=10)
        self.cmb_iface.bind("<<ComboboxSelected>>", self._iface_chosen)
        ttk.Button(top, text="↻", width=3, command=self._fill_ifaces).pack(side="left")

        ttk.Label(f, text="Klicka för att byta direkt. '?' frågas varje gång.",
                  style="Sub.TLabel").pack(anchor="w", pady=(14, 6))
        self.quick_grid = ttk.Frame(f)
        self.quick_grid.pack(fill="x")

        bottom = ttk.Frame(f)
        bottom.pack(fill="x", pady=(18, 0))
        ttk.Button(bottom, text="Tillbaka till DHCP", style="Accent.TButton",
                   command=self.app.apply_dhcp).pack(side="left")
        ttk.Button(bottom, text="Manuell IP…", command=self.ui.manual_ip).pack(side="left", padx=8)
        ttk.Button(bottom, text="Nätverksanslutningar",
                   command=self.app.open_ncpa).pack(side="right")

        # Förklaring av ikonens färger
        leg = ttk.Frame(f)
        leg.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Separator(leg).pack(fill="x", pady=(0, 10))
        ttk.Label(leg, text="Ikonen i aktivitetsfältet", style="Sub.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(leg)
        row.pack(anchor="w")
        self._legend_imgs = []
        from PIL import ImageTk
        for mode, vpn, text in (("static", False, "Statisk IP"), ("dhcp", False, "DHCP"),
                                ("apipa", False, "DHCP utan svar"), ("down", False, "Ingen kabel"),
                                ("dhcp", True, "ZeroTier ansluten")):
            img = ImageTk.PhotoImage(icons.render(mode, vpn, size=22))
            self._legend_imgs.append(img)
            ttk.Label(row, image=img, text=f" {text}", compound="left").pack(side="left", padx=(0, 18))
        ttk.Label(leg, text="Delat uttag", style="Sub.TLabel").pack(anchor="w", pady=(8, 4))
        row_w = ttk.Frame(leg)
        row_w.pack(anchor="w")
        row_l = ttk.Frame(leg)
        row_l.pack(anchor="w", pady=(4, 0))
        for target, text, kind, live in (
            (row_w, "Nät väntar", "net", False),
            (row_w, "USB väntar", "usb", False),
            (row_w, "COM väntar", "com", False),
            (row_l, "Nät igång", "net", True),
            (row_l, "USB igång", "usb", True),
            (row_l, "COM igång", "com", True),
        ):
            img = ImageTk.PhotoImage(icons.render(share=kind, live=live, size=22))
            self._legend_imgs.append(img)
            ttk.Label(target, image=img, text=f" {text}", compound="left").pack(side="left", padx=(0, 16))

        self._fill_ifaces()
        self._build_quick_buttons()

    def _fill_ifaces(self):
        ads = self.app.adapters(refresh=True)
        self._iface_names = [a.name for a in ads]
        vals = []
        for a in ads:
            ip = a.primary_ip
            tag = f"{ip[0]}/{ip[1]}" if ip else ("ingen kabel" if not a.up else "ingen IP")
            vals.append(f"{a.name}   —   {tag}" + ("   (virtuellt)" if a.virtual else ""))
        self.cmb_iface["values"] = vals
        if self.app.iface_name in self._iface_names:
            self.cmb_iface.current(self._iface_names.index(self.app.iface_name))

    def _iface_chosen(self, _e=None):
        i = self.cmb_iface.current()
        if 0 <= i < len(self._iface_names):
            self.app.set_interface(self._iface_names[i])

    def _build_quick_buttons(self):
        for w in self.quick_grid.winfo_children():
            w.destroy()
        self.quick_btns = []
        cols = 3
        for n, p in enumerate(self.app.cfg.get("ip_presets", [])):
            b = ttk.Button(self.quick_grid, text=self._preset_btn_text(p, False),
                           style="Preset.TButton", command=lambda p=p: self.app.apply_preset(p))
            b.grid(row=n // cols, column=n % cols, sticky="we", padx=4, pady=4)
            self.quick_btns.append((p, b))
        for c in range(cols):
            self.quick_grid.columnconfigure(c, weight=1, uniform="q")

    def _preset_btn_text(self, p, active):
        mark = "●  " if active else ""
        return f"{mark}{p.get('name')}\n{C.preset_label(p)}"

    def _refresh_quick(self, s):
        for p, b in getattr(self, "quick_btns", []):
            act = bool(s["mode"] == "static" and s["ip"] and C.preset_matches(p, s["ip"], s["prefix"]))
            t = self._preset_btn_text(p, act)
            if b.cget("text") != t:
                b.config(text=t, style="Preset.Accent.TButton" if act else "Preset.TButton")

    # ---------------------------------------------------------- Hitta PLC

    def _build_discover(self, f):
        ok, why = self.app.discovery_available()
        self.disc_ok = ok
        intro = ("Sök nätet sveper det nät kortet redan har, plus profilerna, och listar alla som svarar. "
                 "Sök kabel är den snabba vägen mot en ensam PLC. Siemens, FX5 och Beijer märks via MAC-adressen. "
                 "FX5-träffen är Mitsubishis prefix, så andra Mitsubishi-enheter kan få samma märke.")
        ttk.Label(f, text=intro, wraplength=720, justify="left").pack(anchor="w")

        if not ok:
            box = ttk.Frame(f, padding=(0, 14))
            box.pack(fill="x")
            ttk.Label(box, text=f"Inte tillgängligt: {why}", foreground="#d9822b").pack(anchor="w")
            ttk.Label(box, text="Installera Npcap (gratis för eget bruk). Starta sedan om ZT-Tray.",
                      style="Sub.TLabel").pack(anchor="w", pady=(2, 8))
            ttk.Button(box, text="Hämta Npcap…", command=lambda: winint.open_path(NPCAP_URL)).pack(anchor="w")

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(12, 6))
        self.btn_search = ttk.Button(bar, text="Sök kabel", command=lambda: self.start_discovery(False))
        self.btn_search.pack(side="left")
        self.btn_net = ttk.Button(bar, text="Sök nätet", style="Accent.TButton",
                                  command=lambda: self.start_discovery(True))
        self.btn_net.pack(side="left", padx=(8, 0))
        self.disc_status = ttk.Label(bar, text="", style="Sub.TLabel")
        self.disc_status.pack(side="left", padx=12)
        self.disc_prog = ttk.Progressbar(bar, mode="indeterminate", length=140)

        act = ttk.Frame(f)
        act.pack(side="bottom", fill="x", pady=(8, 0))
        self.btn_use = ttk.Button(act, text="Sätt min IP i samma nät", style="Accent.TButton",
                                  command=self._disc_use)
        self.btn_use.pack(side="left")
        self.btn_copy = ttk.Button(act, text="Kopiera IP", command=self._disc_copy)
        self.btn_copy.pack(side="left", padx=8)

        cols = ("ip", "mac", "vendor", "how")
        self.disc_tree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, t, w in (("ip", "IP-adress", 140), ("mac", "MAC", 150),
                        ("vendor", "Enhet", 200), ("how", "Hittad via", 150)):
            self.disc_tree.heading(c, text=t)
            self.disc_tree.column(c, width=w, anchor="w")
        self.disc_tree.pack(fill="both", expand=True)
        self.disc_tree.bind("<Double-1>", lambda e: self._disc_use())

        if not ok:
            for w in (self.btn_search, self.btn_net, self.btn_use, self.btn_copy):
                w.state(["disabled"])
            self.disc_tree.state(["disabled"])

    def start_discovery(self, network=False):
        if not self.disc_ok or getattr(self, "_disc_running", False):
            return
        self._disc_running = True
        self.btn_search.state(["disabled"])
        self.btn_net.state(["disabled"])
        for i in self.disc_tree.get_children():
            self.disc_tree.delete(i)
        self.disc_prog.pack(side="right")
        self.disc_prog.start(12)
        if network:
            nets = self.app.network_subnets()
            where = ", ".join(f"{n}.0/24" for n in nets[:4]) or "inget känt nät"
            more = "…" if len(nets) > 4 else ""
            self.disc_status.config(text=f"Söker nätet på {self.app.iface_name}: {where}{more}")
        else:
            self.disc_status.config(text=f"Söker kabeln på {self.app.iface_name}…")

        def upd(found):
            self.ui.call(self._disc_show, found)

        def done(found, err):
            self.ui.call(self._disc_done, found, err)

        self.app.run_discovery(upd, done, network=network)

    def _disc_show(self, found):
        if not self.alive():
            return
        for i in self.disc_tree.get_children():
            self.disc_tree.delete(i)
        ranked = sorted(found.items(), key=lambda kv: (not kv[1].kind, -kv[1].count))
        for ip, r in ranked:
            self.disc_tree.insert("", "end", iid=ip,
                                  values=(ip, r.mac, r.vendor or "–", ", ".join(sorted(r.how))))
        kids = self.disc_tree.get_children()
        if kids and not self.disc_tree.selection():
            self.disc_tree.selection_set(kids[0])

    def _disc_done(self, found, err):
        self._disc_running = False
        if not self.alive():
            return
        self.disc_prog.stop()
        self.disc_prog.pack_forget()
        self.btn_search.state(["!disabled"])
        self.btn_net.state(["!disabled"])
        if err:
            self.disc_status.config(text=f"Fel: {err}")
            return
        self._disc_show(found)
        n = len(found)
        named = sum(1 for r in found.values() if r.kind)
        extra = f", varav {named} Siemens/FX5/Beijer" if named else ""
        self.disc_status.config(text="Inget svar — kontrollera kabel/länk och sök igen."
                                if n == 0 else f"{n} enhet(er){extra}. Dubbelklicka för att ansluta.")

    def _disc_selected(self):
        sel = self.disc_tree.selection()
        return sel[0] if sel else None

    def _disc_use(self):
        ip = self._disc_selected()
        if not ip or not self.disc_ok:
            return
        self.app.join_device_subnet(ip)

    def _disc_copy(self):
        ip = self._disc_selected()
        if ip:
            self.win.clipboard_clear()
            self.win.clipboard_append(ip)
            self.disc_status.config(text=f"{ip} kopierad")

    # ---------------------------------------------------------- IP-profiler

    def _build_presets(self, f):
        ttk.Label(f, text="Använd '?' för det som ska frågas vid varje byte, t.ex. nät 10.20.? och värd 200.",
                  style="Sub.TLabel").pack(anchor="w", pady=(0, 8))
        body = ttk.Frame(f)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        cols = ("name", "addr", "gw")
        self.pr_tree = ttk.Treeview(left, columns=cols, show="headings", height=10, selectmode="browse")
        for c, t, w in (("name", "Namn", 150), ("addr", "Datorns adress", 170), ("gw", "Gateway", 110)):
            self.pr_tree.heading(c, text=t)
            self.pr_tree.column(c, width=w, anchor="w")
        self.pr_tree.pack(fill="both", expand=True)
        self.pr_tree.bind("<<TreeviewSelect>>", self._pr_select)
        lb = ttk.Frame(left)
        lb.pack(fill="x", pady=(8, 0))
        for t, cmd in (("Ny", self._pr_new), ("Ta bort", self._pr_delete),
                       ("▲", lambda: self._pr_move(-1)), ("▼", lambda: self._pr_move(1))):
            ttk.Button(lb, text=t, command=cmd, width=8 if len(t) > 1 else 3).pack(side="left", padx=(0, 6))

        right = ttk.Frame(body, padding=(16, 0, 0, 0))
        right.pack(side="left", fill="y")
        self.pr_fields = {}
        for r, (k, label, hint) in enumerate((
                ("name", "Namn", "visas i menyn"),
                ("network", "Nät", "t.ex. 192.168.5 eller 10.20.?"),
                ("host", "Värd", "datorns sista del, t.ex. 200 eller ?"),
                ("prefix", "Prefix", "24 = 255.255.255.0"),
                ("gateway", "Gateway", "tomt = ingen"))):
            lr = ttk.Frame(right)
            lr.grid(row=r * 2, column=0, sticky="w", pady=(6 if r else 0, 0))
            ttk.Label(lr, text=label).pack(side="left")
            ttk.Label(lr, text=f"  {hint}", style="Sub.TLabel").pack(side="left")
            e = ttk.Entry(right, width=30)
            e.grid(row=r * 2 + 1, column=0, sticky="we")
            e.bind("<FocusOut>", lambda ev: self._pr_preview())
            e.bind("<KeyRelease>", lambda ev: self._pr_preview())
            self.pr_fields[k] = e
        self.pr_preview = ttk.Label(right, text="", style="Sub.TLabel")
        self.pr_preview.grid(row=20, column=0, sticky="w", pady=(12, 0))
        ttk.Button(right, text="Spara profil", style="Accent.TButton",
                   command=self._pr_save).grid(row=21, column=0, sticky="w", pady=(12, 0))

        self.presets = [dict(p) for p in self.app.cfg.get("ip_presets", [])]
        self._pr_fill()

    def _pr_fill(self, select=0):
        self.pr_tree.delete(*self.pr_tree.get_children())
        for i, p in enumerate(self.presets):
            self.pr_tree.insert("", "end", iid=str(i),
                                values=(p.get("name"), C.preset_label(p), p.get("gateway") or "–"))
        if self.presets:
            select = max(0, min(select, len(self.presets) - 1))
            self.pr_tree.selection_set(str(select))
            self.pr_tree.see(str(select))

    def _pr_idx(self):
        s = self.pr_tree.selection()
        return int(s[0]) if s else None

    def _pr_select(self, _e=None):
        i = self._pr_idx()
        if i is None:
            return
        p = self.presets[i]
        for k, e in self.pr_fields.items():
            e.delete(0, "end")
            e.insert(0, str(p.get(k, "")))
        self._pr_preview()

    def _pr_form(self):
        d = {k: e.get().strip() for k, e in self.pr_fields.items()}
        h = d["host"]
        d["host"] = int(h) if h.isdigit() else h
        d["prefix"] = int(d["prefix"]) if d["prefix"].isdigit() else 24
        return d

    def _pr_preview(self):
        d = self._pr_form()
        qs = C.preset_questions(d)
        try:
            ip, mask, gw = C.resolve_preset(d, {i: "1" for i in qs})
            txt = f"Blir: {C.preset_label(d)}  ({mask})" + (f", gw {gw}" if gw else "")
            if qs:
                txt += f"  — frågar {len(qs)} fält"
            self.pr_preview.config(text=txt, foreground="")
            return True
        except Exception as ex:
            self.pr_preview.config(text=f"Fel: {ex}", foreground="#d9534f")
            return False

    def _pr_save(self):
        i = self._pr_idx()
        if i is None or not self._pr_preview():
            return
        d = self._pr_form()
        if not d["name"]:
            d["name"] = C.preset_label(d)
        self.presets[i] = d
        self._pr_commit(i)

    def _pr_new(self):
        self.presets.append({"name": "Nytt nät", "network": "192.168.1", "host": 200, "prefix": 24, "gateway": ""})
        self._pr_commit(len(self.presets) - 1)

    def _pr_delete(self):
        i = self._pr_idx()
        if i is None:
            return
        if messagebox.askyesno("Ta bort", f"Ta bort '{self.presets[i].get('name')}'?", parent=self.win):
            del self.presets[i]
            self._pr_commit(i)

    def _pr_move(self, d):
        i = self._pr_idx()
        if i is None or not (0 <= i + d < len(self.presets)):
            return
        self.presets[i], self.presets[i + d] = self.presets[i + d], self.presets[i]
        self._pr_commit(i + d)

    def _pr_commit(self, sel):
        self.app.update_cfg(lambda cfg: cfg.__setitem__("ip_presets", [dict(p) for p in self.presets]))
        self._pr_fill(sel)
        self._build_quick_buttons()

    # ---------------------------------------------------------- ZeroTier

    def _build_zt(self, f):
        import ztctl
        if not ztctl.installed():
            ttk.Label(f, text="ZeroTier One verkar inte vara installerat (hittar inte zerotier-one_x64.exe).",
                      foreground="#d9822b").pack(anchor="w", pady=(0, 8))
        body = ttk.Frame(f)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self.zt_tree = ttk.Treeview(left, columns=("name", "net"), show="headings", height=10, selectmode="browse")
        self.zt_tree.heading("name", text="Plats")
        self.zt_tree.heading("net", text="Fjärrnät")
        self.zt_tree.column("name", width=200)
        self.zt_tree.column("net", width=140)
        self.zt_tree.pack(fill="both", expand=True)
        self.zt_tree.bind("<<TreeviewSelect>>", self._zt_select)
        lb = ttk.Frame(left)
        lb.pack(fill="x", pady=(8, 0))
        ttk.Button(lb, text="Ny", width=8, command=self._zt_new).pack(side="left", padx=(0, 6))
        ttk.Button(lb, text="Ta bort", width=8, command=self._zt_delete).pack(side="left", padx=(0, 6))
        ttk.Button(lb, text="Anslut", style="Accent.TButton", command=self._zt_connect).pack(side="right")
        ttk.Button(lb, text="Koppla från", command=self.app.disconnect_site).pack(side="right", padx=6)

        right = ttk.Frame(body, padding=(16, 0, 0, 0))
        right.pack(side="left", fill="y")
        self.zt_fields = {}
        for r, (k, label) in enumerate((("name", "Namn"), ("network_id", "ZeroTier Network ID"),
                                        ("router_zt_ip", "Routerns ZT-IP"),
                                        ("remote_subnet", "Fjärrsubnät"), ("subnet_mask", "Subnätmask"))):
            ttk.Label(right, text=label).grid(row=r * 2, column=0, sticky="w", pady=(6 if r else 0, 0))
            e = ttk.Entry(right, width=28)
            e.grid(row=r * 2 + 1, column=0, sticky="we")
            self.zt_fields[k] = e
        ttk.Button(right, text="Spara plats", style="Accent.TButton",
                   command=self._zt_save).grid(row=20, column=0, sticky="w", pady=(14, 0))
        self.sites = [dict(s) for s in self.app.cfg.get("sites", [])]
        self._zt_fill()

    def _zt_fill(self, select=0):
        self.zt_tree.delete(*self.zt_tree.get_children())
        for i, s in enumerate(self.sites):
            self.zt_tree.insert("", "end", iid=str(i),
                                values=(s.get("name"), f"{s.get('remote_subnet')}"))
        if self.sites:
            select = max(0, min(select, len(self.sites) - 1))
            self.zt_tree.selection_set(str(select))

    def _zt_idx(self):
        s = self.zt_tree.selection()
        return int(s[0]) if s else None

    def _zt_select(self, _e=None):
        i = self._zt_idx()
        if i is None:
            return
        for k, e in self.zt_fields.items():
            e.delete(0, "end")
            e.insert(0, self.sites[i].get(k, ""))

    def _zt_save(self):
        i = self._zt_idx()
        if i is None:
            return
        d = {k: e.get().strip() for k, e in self.zt_fields.items()}
        if not d["name"] or not d["network_id"] or not d["router_zt_ip"]:
            messagebox.showerror("Saknas", "Namn, Network ID och routerns ZT-IP måste fyllas i.", parent=self.win)
            return
        self.sites[i] = d
        self._zt_commit(i)

    def _zt_new(self):
        self.sites.append({"name": "Ny plats", "network_id": "", "router_zt_ip": "",
                           "remote_subnet": "192.168.10.0", "subnet_mask": "255.255.255.0"})
        self._zt_commit(len(self.sites) - 1)

    def _zt_delete(self):
        i = self._zt_idx()
        if i is None:
            return
        if messagebox.askyesno("Ta bort", f"Ta bort '{self.sites[i].get('name')}'?", parent=self.win):
            del self.sites[i]
            self._zt_commit(i)

    def _zt_commit(self, sel):
        self.app.update_cfg(lambda cfg: cfg.__setitem__("sites", [dict(s) for s in self.sites]))
        self._zt_fill(sel)

    def _zt_connect(self):
        i = self._zt_idx()
        if i is not None:
            self.app.connect_site(self.sites[i])

    # ---------------------------------------------------------- Uttag

    def _build_uttag(self, f):
        import netif
        app = self.app
        u = app.uttag_cfg()
        intro = ("Kabeln mot PLC:n sitter i en dator (Uttag). TIA Portal eller GX Works "
                 "körs på en annan dator som också har ZT-Tray (Verktyg): en kollega eller "
                 "en RDP. Ramarna går över ZeroTier, så du slipper installera programmet lokalt. "
                 "IP-adressen sätts bara på verktygssidan.")
        ttk.Label(f, text=intro, wraplength=740, justify="left").pack(anchor="w")
        ttk.Label(f, text="Nät delar Ethernet. USB flyttar en USB-enhet till verktygssidan. "
                          "COM delar en serieport. Välj inte kortet som har internet eller RDP.",
                  style="Sub.TLabel", wraplength=740).pack(anchor="w", pady=(4, 2))
        ttk.Label(f, text="När du delar ett nätverksuttag syns datornamnet hos de andra på samma "
                          "ZeroTier-nät. De skriver din PIN för att få koppla på.",
                  style="Sub.TLabel", wraplength=740).pack(anchor="w", pady=(0, 8))

        role = ttk.Frame(f)
        role.pack(fill="x")
        self.ut_role = tk.StringVar(value=u.get("role") or "uttag")
        ttk.Radiobutton(role, text="Uttag hos kund — kabeln sitter här", value="uttag",
                        variable=self.ut_role, command=self._ut_role).pack(side="left")
        ttk.Radiobutton(role, text="Verktyg — TIA / GX Works körs här", value="verktyg",
                        variable=self.ut_role, command=self._ut_role).pack(side="left", padx=(16, 0))

        mode_row = ttk.Frame(f)
        mode_row.pack(fill="x", pady=(8, 0))
        ttk.Label(mode_row, text="Läge").pack(side="left")
        self.ut_mode = ttk.Combobox(mode_row, state="readonly", width=16,
                                    values=("Nätverk", "USB", "COM"))
        self.ut_mode.pack(side="left", padx=8)
        _mode_label = {"net": "Nätverk", "usb": "USB", "com": "COM"}.get(u.get("mode") or "net", "Nätverk")
        self.ut_mode.set(_mode_label)
        self.ut_mode.bind("<<ComboboxSelected>>", lambda e: self._ut_apply_mode())

        self.ut_mode_host = ttk.Frame(f)
        self.ut_mode_host.pack(fill="x")
        self.ut_net_box = ttk.Frame(self.ut_mode_host)
        self.ut_usb_box = ttk.Frame(self.ut_mode_host)
        self.ut_com_box = ttk.Frame(self.ut_mode_host)

        row = ttk.Frame(self.ut_net_box)
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="Kort").pack(side="left")
        self.ut_iface = ttk.Combobox(row, state="readonly", width=42)
        self.ut_iface.pack(side="left", padx=8)
        self.ut_iface.bind("<<ComboboxSelected>>", self._ut_iface)
        ttk.Button(row, text="↻", width=3, command=self._ut_fill_ifaces).pack(side="left")
        ttk.Button(row, text="Lägg till kort…", command=netif.open_hardware_wizard).pack(side="left", padx=(8, 0))

        iprow = ttk.Frame(self.ut_net_box)
        iprow.pack(fill="x", pady=(8, 0))
        ttk.Label(iprow, text="IP på verktygskortet").pack(side="left")
        self.ut_ip = ttk.Entry(iprow, width=16)
        self.ut_ip.insert(0, u.get("tool_ip") or "")
        self.ut_ip.pack(side="left", padx=(8, 4))
        ttk.Label(iprow, text="/").pack(side="left")
        self.ut_prefix = ttk.Entry(iprow, width=4)
        self.ut_prefix.insert(0, str(u.get("tool_prefix") or 24))
        self.ut_prefix.pack(side="left", padx=(4, 8))
        ttk.Button(iprow, text="Sätt IP", command=self._ut_set_ip).pack(side="left")
        self.ut_ip_hint = ttk.Label(iprow, text="", style="Sub.TLabel")
        self.ut_ip_hint.pack(side="left", padx=8)

        self.ut_pin_row = ttk.Frame(self.ut_net_box)
        self.ut_pin_row.pack(fill="x", pady=(8, 0))
        ttk.Label(self.ut_pin_row, text="PIN").pack(side="left")
        self.ut_pin = ttk.Entry(self.ut_pin_row, width=10, show="*")
        self.ut_pin.insert(0, u.get("pin") or "")
        self.ut_pin.pack(side="left", padx=8)
        self.ut_pin.bind("<FocusOut>", lambda e: self._ut_save_pin())
        ttk.Label(self.ut_pin_row, text="4–8 siffror. Samma kod anges på datorn som kopplar på.",
                  style="Sub.TLabel").pack(side="left")

        usb = self.ut_usb_box
        usb_row = ttk.Frame(usb)
        usb_row.pack(fill="x", pady=(10, 0))
        ttk.Label(usb_row, text="USB-enhet").pack(side="left")
        self.ut_usb = ttk.Combobox(usb_row, state="readonly", width=52)
        self.ut_usb.pack(side="left", padx=8)
        ttk.Button(usb_row, text="↻", width=3, command=self._ut_usb_refresh).pack(side="left")
        ttk.Button(usb_row, text="Installera usbipd", command=self._ut_install_usbipd).pack(side="left", padx=(8, 0))
        ttk.Button(usb_row, text="Hämta USB-klient", command=self._ut_open_usb_client).pack(side="left", padx=(8, 0))
        self.ut_usb_hint = ttk.Label(usb, text="Uttagssidan binder enheten med usbipd. Verktygssidan kopplar in den med usbip-win2.",
                                     style="Sub.TLabel", wraplength=720)
        self.ut_usb_hint.pack(anchor="w", pady=(4, 0))
        self._usb_ids = []

        com = self.ut_com_box
        com_row = ttk.Frame(com)
        com_row.pack(fill="x", pady=(10, 0))
        ttk.Label(com_row, text="COM").pack(side="left")
        self.ut_com = ttk.Combobox(com_row, state="readonly", width=12)
        self.ut_com.pack(side="left", padx=8)
        ttk.Button(com_row, text="↻", width=3, command=self._ut_com_refresh).pack(side="left")
        ttk.Label(com_row, text="Baud").pack(side="left", padx=(12, 0))
        self.ut_baud = ttk.Combobox(com_row, state="readonly", width=8,
                                    values=("9600", "19200", "38400", "57600", "115200"))
        self.ut_baud.set(str(u.get("com_baud") or 9600))
        self.ut_baud.pack(side="left", padx=4)
        ttk.Label(com_row, text="Paritet").pack(side="left", padx=(8, 0))
        self.ut_parity = ttk.Combobox(com_row, state="readonly", width=3, values=("N", "E", "O"))
        self.ut_parity.set(u.get("com_parity") or "N")
        self.ut_parity.pack(side="left", padx=4)
        ttk.Label(com_row, text="Data").pack(side="left", padx=(8, 0))
        self.ut_data = ttk.Combobox(com_row, state="readonly", width=3, values=("8", "7"))
        self.ut_data.set(str(u.get("com_data") or 8))
        self.ut_data.pack(side="left", padx=4)
        ttk.Label(com_row, text="Stopp").pack(side="left", padx=(8, 0))
        self.ut_stopb = ttk.Combobox(com_row, state="readonly", width=3, values=("1", "2"))
        self.ut_stopb.set(str(u.get("com_stop") or 1))
        self.ut_stopb.pack(side="left", padx=4)
        self.ut_com_hint = ttk.Label(
            com, text="Verktygssidan behöver com0com. Baud, paritet och stoppbitar skickas till uttagsdatorn.",
            style="Sub.TLabel", wraplength=720)
        self.ut_com_hint.pack(anchor="w", pady=(4, 0))
        self._com_ports = []

        peer = ttk.Frame(f)
        peer.pack(fill="x", pady=(8, 0))
        ttk.Label(peer, text="ZeroTier-IP").pack(side="left")
        self.ut_peer = ttk.Entry(peer, width=18)
        self.ut_peer.insert(0, u.get("peer_ip") or "")
        self.ut_peer.pack(side="left", padx=8)
        ttk.Button(peer, text="Koppla på…", command=self._ut_connect_ask).pack(side="left")
        ttk.Label(peer, text="Listan fylls när någon delar ett nätverksuttag.",
                  style="Sub.TLabel").pack(side="left", padx=8)

        cols = ("name", "iface", "ip", "state")
        self.ut_tree = ttk.Treeview(f, columns=cols, show="headings", height=4)
        for c, t, w in (("name", "Dator", 200), ("iface", "Uttag", 200),
                        ("ip", "ZeroTier-IP", 150), ("state", "Läge", 90)):
            self.ut_tree.heading(c, text=t)
            self.ut_tree.column(c, width=w, anchor="w")
        self.ut_tree.pack(fill="both", expand=True, pady=(8, 0))
        self.ut_tree.bind("<Double-1>", lambda e: self._ut_connect_ask())

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(8, 0))
        self.ut_btn = ttk.Button(bar, text="Starta", style="Accent.TButton", command=self._ut_toggle)
        self.ut_btn.pack(side="left")
        self.ut_status = ttk.Label(bar, text="Stoppad", style="Sub.TLabel")
        self.ut_status.pack(side="left", padx=12)
        self._ut_sig = None
        self._ut_fill_ifaces()
        self._ut_com_refresh()
        self._ut_apply_mode()
        self._ut_role()

    def _ut_fill_ifaces(self):
        import netif
        ads = netif.outlet_adapters(self.app.adapters(refresh=True))
        self._ut_names = [a.name for a in ads]
        vals = []
        for a in ads:
            ip = a.primary_ip
            tag = f"{ip[0]}/{ip[1]}" if ip else ("ingen kabel" if not a.up else "ingen IP")
            extra = "  (virtuellt)" if a.virtual else ""
            vals.append(f"{a.name}   —   {tag}{extra}")
        self.ut_iface["values"] = vals
        want = self.app.uttag_cfg().get("interface") or ""
        if want in self._ut_names:
            self.ut_iface.current(self._ut_names.index(want))

    def _ut_iface(self, _e=None):
        i = self.ut_iface.current()
        if 0 <= i < len(getattr(self, "_ut_names", [])):
            self.app.uttag_save(interface=self._ut_names[i])

    def _ut_mode_value(self):
        return {"Nätverk": "net", "USB": "usb", "COM": "com"}.get(self.ut_mode.get(), "net")

    def _ut_apply_mode(self):
        mode = self._ut_mode_value()
        self.app.uttag_save(mode=mode)
        for box in (self.ut_net_box, self.ut_usb_box, self.ut_com_box):
            box.pack_forget()
        {"net": self.ut_net_box, "usb": self.ut_usb_box, "com": self.ut_com_box}[mode].pack(fill="x")
        if mode == "com" and self.ut_role.get() == "verktyg":
            self.ut_com_hint.config(text="com0com skapar en lokal COM-port när du startar. Öppna den i TIA. Bauden skickas till uttagsdatorn.")
        elif mode == "com":
            self.ut_com_hint.config(text="Den fysiska porten delas. Verktygssidan bestämmer baud, paritet och stoppbitar.")

    def _ut_role(self):
        role = self.ut_role.get()
        self.app.uttag_save(role=role)
        if role == "verktyg":
            self.ut_ip_hint.config(text="Den här adressen ser PLC:n. Sätt den i samma nät som PLC:n.")
            if hasattr(self, "ut_pin_row"):
                self.ut_pin_row.pack_forget()
        else:
            self.ut_ip_hint.config(text="Här sätts ingen IP. Kortet släpper sin adress medan delningen pågår.")
            if hasattr(self, "ut_pin_row"):
                self.ut_pin_row.pack(fill="x", pady=(8, 0))
        if hasattr(self, "ut_mode"):
            self._ut_apply_mode()

    def _ut_save_pin(self):
        if not hasattr(self, "ut_pin") or self.ut_role.get() != "uttag":
            return
        self.app.uttag_save(pin=self.ut_pin.get().strip())

    def _ut_usb_selected(self):
        i = self.ut_usb.current()
        if 0 <= i < len(self._usb_ids):
            return self._usb_ids[i]
        return ""

    def _ut_usb_fill(self, rows):
        if not self.alive():
            return
        self._usb_ids = [r["busid"] for r in rows]
        vals = []
        for r in rows:
            state = f"  ({r['state']})" if r.get("state") else ""
            vals.append(f"{r['busid']}  {r.get('device') or ''}{state}")
        self.ut_usb["values"] = vals
        want = self.app.uttag_cfg().get("usb_busid") or ""
        if want in self._usb_ids:
            self.ut_usb.current(self._usb_ids.index(want))
        elif vals:
            self.ut_usb.current(0)
        self.ut_usb_hint.config(text=f"{len(rows)} USB-enhet(er)." if rows else "Inga USB-enheter syntes.")

    def _ut_usb_refresh(self):
        import usbshare
        role = self.ut_role.get()
        peer = self.ut_peer.get().strip()

        def worker():
            try:
                rows = usbshare.list_remote(peer) if role == "verktyg" else usbshare.list_local()
            except Exception as e:
                self.ui.call(lambda msg=str(e): self.ut_usb_hint.config(text=msg[:200]))
                return
            self.ui.call(self._ut_usb_fill, rows)

        self.ut_usb_hint.config(text="Hämtar enheter…")
        threading.Thread(target=worker, daemon=True).start()

    def _ut_install_usbipd(self):
        import ztctl

        def worker():
            rc, out, err = ztctl.run(
                ["winget", "install", "--exact", "--disable-interactivity",
                 "--accept-package-agreements", "--accept-source-agreements",
                 "dorssel.usbipd-win"], timeout=180)
            self.app.notify(((out or err) or f"winget rc={rc}")[:200])

        self.app.notify("Installerar usbipd…")
        threading.Thread(target=worker, daemon=True).start()

    def _ut_open_usb_client(self):
        import usbshare
        import webbrowser
        webbrowser.open(usbshare.CLIENT_URL)

    def _ut_com_selected(self):
        i = self.ut_com.current()
        if 0 <= i < len(self._com_ports):
            return self._com_ports[i]
        return ""

    def _ut_com_refresh(self):
        import serialshare
        ports = serialshare.list_com_ports()
        self._com_ports = ports
        self.ut_com["values"] = ports
        want = (self.app.uttag_cfg().get("com_port") or "").upper()
        if want in ports:
            self.ut_com.current(ports.index(want))
        elif ports:
            self.ut_com.current(0)

    def _ut_selected_share(self):
        sel = self.ut_tree.selection()
        if not sel:
            return "", "", ""
        ip = sel[0]
        vals = self.ut_tree.item(ip, "values") or ()
        name = vals[0] if len(vals) > 0 else ""
        iface = vals[1] if len(vals) > 1 else ""
        return ip, name, iface

    def _ut_connect_ask(self):
        import uttag
        eng = self.app.uttag_engine
        if eng and eng.running and eng.role == "uttag":
            messagebox.showinfo(
                "Koppla på",
                "Du delar redan ett uttag. Stoppa det innan du kopplar på någon annan.",
                parent=self.win)
            return
        self._ut_iface()
        if not (self.app.uttag_cfg().get("interface") or "").strip():
            messagebox.showinfo(
                "Koppla på",
                "Välj först vilket nätverkskort programmet ska använda.",
                parent=self.win)
            return
        ip, name, iface = self._ut_selected_share()
        if not ip:
            ip = self.ut_peer.get().strip()
        if not ip:
            messagebox.showinfo(
                "Koppla på",
                "Välj ett uttag i listan, eller skriv dess ZeroTier-IP.",
                parent=self.win)
            return
        who = name or ip
        st = self.app.uttag_snapshot()
        if st.get("running") and st.get("peer_ip") == ip:
            messagebox.showinfo("Koppla på", f"Du är redan kopplad till {who}.", parent=self.win)
            return
        card = f" ({iface})" if iface and iface != "Nätverk" else ""
        win = tk.Toplevel(self.win)
        win.title("Koppla på")
        win.transient(self.win)
        win.resizable(False, False)
        win.grab_set()
        box = ttk.Frame(win, padding=16)
        box.pack(fill="both")
        ttk.Label(box, text=f"{who} har valt att dela sitt nätverksuttag{card}.",
                  wraplength=380).pack(anchor="w")
        ttk.Label(box, text="Vill du koppla dig på?", wraplength=380).pack(anchor="w", pady=(4, 10))
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="PIN").pack(side="left")
        ent = ttk.Entry(row, width=12, show="*")
        ent.pack(side="left", padx=8)
        ent.focus_set()

        def close():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()

        def ok(_e=None):
            pin = ent.get().strip()
            if not uttag.valid_pin(pin):
                messagebox.showerror("PIN", "PIN ska vara 4 till 8 siffror.", parent=win)
                return
            close()
            self._ut_connect_go(ip, pin, name)

        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="Koppla på", style="Accent.TButton", command=ok).pack(side="left")
        ttk.Button(bar, text="Avbryt", command=close).pack(side="left", padx=8)
        win.bind("<Return>", ok)
        win.bind("<Escape>", lambda e: close())
        win.protocol("WM_DELETE_WINDOW", close)

    def _ut_connect_go(self, ip, pin, name):
        self.ut_peer.delete(0, "end")
        self.ut_peer.insert(0, ip)
        self.ut_role.set("verktyg")
        self.ut_mode.set("Nätverk")
        self._ut_role()
        self.app.uttag_connect(ip, pin, name)

    def _ut_set_ip(self):
        self.app.uttag_apply_tool_ip(self.ut_ip.get().strip(), self.ut_prefix.get().strip())

    def _ut_toggle(self):
        if self.app.uttag_snapshot()["running"]:
            self.app.uttag_stop()
            return
        self._ut_iface()
        try:
            baud = int(self.ut_baud.get())
            data = int(self.ut_data.get())
            stop = int(self.ut_stopb.get())
        except ValueError:
            baud, data, stop = 9600, 8, 1
        fields = dict(
            peer_ip=self.ut_peer.get().strip(), role=self.ut_role.get(),
            mode=self._ut_mode_value(), usb_busid=self._ut_usb_selected(),
            com_port=self._ut_com_selected(), com_baud=baud, com_data=data,
            com_parity=self.ut_parity.get() or "N", com_stop=stop)
        if fields["role"] == "uttag" and fields["mode"] == "net":
            pin = self.ut_pin.get().strip()
            import uttag
            if not uttag.valid_pin(pin):
                messagebox.showerror(
                    "PIN",
                    "Ange en PIN med 4 till 8 siffror. Den som kopplar på måste skriva samma kod.",
                    parent=self.win)
                return
            fields["pin"] = pin
        self.app.uttag_save(**fields)
        self.app.uttag_start()

    def _refresh_uttag(self):
        if not hasattr(self, "ut_status"):
            return
        st = self.app.uttag_snapshot()
        counts = f"   ·   {st['tx']} ut, {st['rx']} in" if st["running"] else ""
        shares = self.app.share_list() if hasattr(self.app, "share_list") else []
        if st["running"]:
            self.ut_status.config(text=st["status"] + counts)
        else:
            extra = f"  ·  {len(shares)} delade uttag" if shares else "  ·  inga delade uttag syns"
            self.ut_status.config(text="Stoppad" + extra)
        self.ut_btn.config(text="Stoppa" if st["running"] else "Starta")
        peer = st.get("peer_ip") or ""
        rows = []
        for p in shares:
            state = "kopplad" if p["ip"] == peer else "delar"
            rows.append((p["ip"], p.get("name") or "?", p.get("iface") or "Nätverk", state))
        sig = tuple(rows)
        if sig != self._ut_sig:
            self._ut_sig = sig
            self.ut_tree.delete(*self.ut_tree.get_children())
            for ip, name, iface, state in rows:
                self.ut_tree.insert("", "end", iid=ip, values=(name, iface, ip, state))

    # ---------------------------------------------------------- Inställningar

    def _build_modbus(self, f):
        try:
            import modbus_tab
            self.modbus = modbus_tab.ModbusTab(f, self)
        except Exception as e:
            C.log(f"Modbus-fliken kunde inte byggas: {e!r}")
            ttk.Label(f, text=f"Modbus-monitorn kunde inte startas: {e}").pack(anchor="w")

    def _build_settings(self, f):
        app = self.app
        self.v_auto = tk.BooleanVar(value=app.autostart_state())
        self.v_pin = tk.BooleanVar(value=bool(app.cfg["settings"].get("pin_tray", True)))
        self.v_notif = tk.BooleanVar(value=bool(app.cfg["settings"].get("notifications", True)))

        ttk.Label(f, text="Start och aktivitetsfält", style="Big.TLabel").pack(anchor="w")
        ttk.Checkbutton(f, text="Starta ZT-Tray automatiskt när jag loggar in",
                        variable=self.v_auto, style="Switch.TCheckbutton",
                        command=lambda: app.set_autostart(self.v_auto.get())).pack(anchor="w", pady=(8, 2))
        pin = ttk.Checkbutton(f, text="Fäst ikonen i aktivitetsfältet (alltid synlig)",
                              variable=self.v_pin, style="Switch.TCheckbutton",
                              command=lambda: app.set_pin(self.v_pin.get()))
        pin.pack(anchor="w", pady=2)
        if not winint.pin_supported():
            pin.state(["disabled"])
            ttk.Label(f, text="Kräver Windows 11 22H2 eller senare. Dra annars ut ikonen från ^-pilen.",
                      style="Sub.TLabel").pack(anchor="w", padx=(46, 0))
        ttk.Checkbutton(f, text="Visa notiser vid byte",
                        variable=self.v_notif, style="Switch.TCheckbutton",
                        command=lambda: app.update_cfg(lambda c: c["settings"].__setitem__(
                            "notifications", self.v_notif.get()))).pack(anchor="w", pady=2)

        ttk.Separator(f).pack(fill="x", pady=14)
        ttk.Label(f, text="Hitta PLC", style="Big.TLabel").pack(anchor="w")
        g = ttk.Frame(f)
        g.pack(anchor="w", pady=(8, 0))
        ttk.Label(g, text="Söktid (s)").grid(row=0, column=0, sticky="w")
        self.sp_dur = ttk.Spinbox(g, from_=2, to=30, width=5)
        self.sp_dur.set(app.cfg["discovery"].get("duration", 5))
        self.sp_dur.grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(g, text="Extra nät att svepa").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.ent_nets = ttk.Entry(g, width=58)
        self.ent_nets.insert(0, ", ".join(app.cfg["discovery"].get("extra_subnets", [])))
        self.ent_nets.grid(row=1, column=1, sticky="w", padx=10, pady=(8, 0))
        ttk.Label(g, text="Profilernas nät svepas alltid. Vanliga PLC-adresser i alla 192.168.x testas också.",
                  style="Sub.TLabel").grid(row=2, column=1, sticky="w", padx=10)
        ttk.Button(g, text="Spara", command=self._save_disc).grid(row=3, column=1, sticky="w", padx=10, pady=(8, 0))

        ttk.Separator(f).pack(fill="x", pady=14)
        b = ttk.Frame(f)
        b.pack(anchor="w")
        ttk.Button(b, text="Öppna config.json", command=lambda: winint.open_path(C.CONFIG_PATH)).pack(side="left")
        ttk.Button(b, text="Läs om config", command=self._reload).pack(side="left", padx=8)
        ttk.Button(b, text="Visa logg", command=lambda: winint.open_path(C.LOG_PATH)).pack(side="left")

    def _save_disc(self):
        try:
            dur = max(2, min(30, int(float(self.sp_dur.get()))))
        except ValueError:
            dur = 5
        nets = [n.strip().rstrip(".") for n in self.ent_nets.get().replace(";", ",").split(",") if n.strip()]

        def upd(cfg):
            cfg["discovery"]["duration"] = dur
            cfg["discovery"]["extra_subnets"] = nets
        self.app.update_cfg(upd)

    def _reload(self):
        self.app.reload_cfg()
        self.close()
        self.ui.open_main("settings")
