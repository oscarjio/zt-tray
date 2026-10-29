"""
ZT-Tray v2 — snabbt IP-byte på nätverkskortet + ZeroTier-platser, i aktivitetsfältet.

Vänsterklick på ikonen öppnar fönstret. Högerklick ger menyn:
    profiler (ett klick = byt), DHCP, manuell IP, Hitta PLC, ZeroTier, inställningar.
"""

import copy
import ctypes
import os
import sys
import threading
import time

# PyInstaller --windowed: stdout/stderr är None, vilket vissa bibliotek kraschar på
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import pystray
from pystray import Menu, MenuItem as Item

import config as C
import icons
import netif
import serialshare
import usbshare
import uttag
import winint
import ztctl
from config import log

MODE_SHORT = {
    "static": "Statisk IP",
    "dhcp": "DHCP",
    "apipa": "DHCP utan svar (169.254)",
    "down": "Ingen kabel",
    "none": "Ingen IP",
    "unknown": "Inget kort valt",
}


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return True


def relaunch_as_admin():
    cmd, args = winint.launch_command()
    if not getattr(sys, "frozen", False):
        args = " ".join([args] + [f'"{a}"' for a in sys.argv[1:]])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", cmd, args, str(C.APP_DIR), 1)


class App:
    def __init__(self):
        self.cfg = C.load_config()
        self.lock = threading.RLock()
        self.icon = None
        self.ui = None
        self.active_site = None
        self.busy = False
        self.uttag_engine = None
        self.usb_link = None
        self.com_link = None
        self._uttag_lock = threading.Lock()
        self.share_board = uttag.ShareBoard()
        self.share_board.start()
        self._seen_shares = None
        restored = netif.restore_pending_ipv4()
        if restored:
            log(f"Återställde IP på {restored} efter en avbruten uttagsdelning")
        self._adapters = []
        self._adapters_t = 0
        self._last_sig = None
        self.iface_name = self.cfg["settings"].get("interface", "")
        ads = self.adapters(refresh=True)
        if not self.iface_name or not netif.find_adapter(self.iface_name, ads):
            guess = netif.default_interface(ads)
            if guess and guess != self.iface_name:
                log(f"Väljer nätverkskort automatiskt: {guess}")
                self.iface_name = guess
                self.update_cfg(lambda c: c["settings"].__setitem__("interface", guess))

    # ------------------------------------------------------------ config

    def update_cfg(self, fn):
        with self.lock:
            fn(self.cfg)
            try:
                C.save_config(self.cfg)
            except Exception as e:
                log(f"Kunde inte spara config: {e}")
        self.rebuild_menu()

    def reload_cfg(self):
        with self.lock:
            self.cfg = C.load_config()
        self.rebuild_menu()

    def remember(self, values):
        self.update_cfg(lambda c: c.setdefault("last_values", {}).update(values))

    # ------------------------------------------------------------ status

    def adapters(self, refresh=False):
        if refresh or time.time() - self._adapters_t > 1.5:
            try:
                self._adapters = netif.list_adapters()
            except Exception as e:
                log(f"list_adapters: {e}")
            self._adapters_t = time.time()
        return self._adapters

    def current(self):
        return netif.find_adapter(self.iface_name, self.adapters())

    def snapshot(self):
        a = self.current()
        if a is None:
            mode, ip, prefix, desc = "unknown", None, None, ""
        else:
            mode, desc = a.mode, a.description
            pip = a.primary_ip
            ip, prefix = (pip if pip else (None, None))
        u = self.uttag_snapshot()
        sharing = bool(u.get("running"))
        return {"iface": self.iface_name if a else "", "desc": desc, "mode": mode,
                "ip": ip, "prefix": prefix, "vpn": self.active_site is not None,
                "site": self.active_site["name"] if self.active_site else "",
                "busy": self.busy, "uttag_line": u.get("short") or "",
                "share": u.get("link") if sharing else "",
                "share_live": bool(sharing and u.get("live"))}

    def tooltip(self, s):
        # Windows visar max 127 tecken
        if s["busy"]:
            line1 = f"{s['iface']}: byter IP…"
        elif s["mode"] == "unknown":
            line1 = "Inget nätverkskort valt"
        else:
            ip = f" {s['ip']}/{s['prefix']}" if s["ip"] and s["mode"] in ("static", "dhcp", "apipa") else ""
            line1 = f"{s['iface']}: {MODE_SHORT[s['mode']]}{ip}"
        line2 = f"VPN: ZeroTier → {s['site']}" if s["vpn"] else "VPN: ej ansluten"
        if s.get("uttag_line"):
            line2 += f" · uttag {s['uttag_line']}"
        return f"ZT-Tray {C.APP_VERSION}\n{line1}\n{line2}"[:127]

    def refresh_icon(self, force=False):
        if not self.icon:
            return
        s = self.snapshot()
        sig = (s["mode"], s["ip"], s["prefix"], s["vpn"], s["site"], s["busy"], s["iface"],
               s.get("uttag_line"), s.get("share"), s.get("share_live"))
        if sig == self._last_sig and not force:
            return
        self._last_sig = sig
        try:
            self.icon.icon = icons.render(
                s["mode"], s["vpn"], s["busy"],
                share=s.get("share") or "", live=bool(s.get("share_live")))
            self.icon.title = self.tooltip(s)
        except Exception as e:
            log(f"Ikonfel: {e}")
        self.rebuild_menu()

    def monitor_loop(self):
        while True:
            try:
                self.adapters(refresh=True)
                self.refresh_icon()
                self._note_new_shares()
            except Exception as e:
                log(f"monitor: {e}")
            time.sleep(1.5)

    def share_list(self):
        found = {}
        board = getattr(self, "share_board", None)
        if board:
            for s in board.shares():
                found[s["ip"]] = s
        eng = self.uttag_engine
        if eng:
            try:
                for p in eng.snapshot().get("peers") or []:
                    if p.get("sharing") and p.get("nonce") and p["ip"] not in found:
                        found[p["ip"]] = {"ip": p["ip"], "name": p["name"],
                                          "iface": p.get("iface") or "", "nonce": p["nonce"]}
            except Exception:
                pass
        return sorted(found.values(), key=lambda s: s["name"].lower())

    def _note_new_shares(self):
        shares = self.share_list()
        current = {s["ip"]: s for s in shares}
        if self._seen_shares is None:
            self._seen_shares = set()
        new_ips = [ip for ip in current if ip not in self._seen_shares]
        self._seen_shares = set(current)
        if not new_ips:
            return
        if len(new_ips) == 1:
            s = current[new_ips[0]]
            self.notify(f"{s['name']} delar sitt nätverksuttag. Öppna Uttag och ange PIN för att koppla på.")
        else:
            self.notify(f"{len(new_ips)} datorer delar nätverksuttag. Öppna Uttag för att koppla på.")

    def notify(self, msg):
        log(f"NOTIFY: {msg}")
        if self.icon and self.cfg["settings"].get("notifications", True):
            try:
                self.icon.notify(msg, "ZT-Tray")
            except Exception:
                pass

    # ------------------------------------------------------------ IP-byte

    def set_interface(self, name):
        self.iface_name = name
        self.update_cfg(lambda c: c["settings"].__setitem__("interface", name))
        self.refresh_icon(force=True)

    def _run_change(self, label, fn):
        if self.busy:
            self.notify("Ett byte pågår redan")
            return
        if not self.iface_name:
            self.notify("Välj nätverkskort först")
            return
        eng = self.uttag_engine
        if eng and eng.running and eng.role == "uttag" and eng.iface_name == self.iface_name:
            self.notify("Det här kortet delas som uttag. Stoppa uttaget innan du byter IP.")
            return

        def worker():
            self.busy = True
            self.refresh_icon(force=True)
            try:
                ok, msg, dt = fn()
            except Exception as e:
                ok, msg, dt = False, str(e), 0
            self.busy = False
            self.adapters(refresh=True)
            self.refresh_icon(force=True)
            if ok:
                self.notify(f"{self.iface_name}: {label}  ({dt:.1f} s)")
            else:
                self.notify(f"Misslyckades: {msg[:180]}")
        threading.Thread(target=worker, daemon=True).start()

    def apply_static(self, ip, mask, gw="", label=None):
        name = self.iface_name
        txt = f"{ip}/{C.mask_to_prefix(mask)}" + (f" gw {gw}" if gw else "")
        self._run_change(f"{label + ' ' if label else ''}{txt}",
                         lambda: netif.set_static(name, ip, mask, gw))

    def apply_preset(self, preset):
        qs = C.preset_questions(preset)
        if not qs:
            try:
                ip, mask, gw = C.resolve_preset(preset)
            except ValueError as e:
                self.notify(str(e))
                return
            self.apply_static(ip, mask, gw)
            return

        def answered(answers, ip, mask, gw):
            self.remember({f"{preset.get('name')}#{i}": v for i, v in answers.items()})
            self.apply_static(ip, mask, gw)
        self.ui.ask_parts(preset, answered)

    def apply_dhcp(self):
        name = self.iface_name
        self._run_change("DHCP", lambda: netif.set_dhcp(name))

    def open_ncpa(self):
        netif.open_adapter_settings()

    # ------------------------------------------------------------ Hitta PLC

    def discovery_available(self):
        if not winint.scapy_available():
            return False, "scapy saknas i bygget"
        if not winint.npcap_installed():
            return False, "Npcap är inte installerat"
        return True, ""

    def discovery_subnets(self):
        nets = []
        lv = self.cfg.get("last_values", {})
        for p in self.cfg.get("ip_presets", []):
            n = C.preset_subnet(p, lv)
            if n:
                nets.append(n)
        nets += self.cfg["discovery"].get("extra_subnets", [])
        return nets

    def network_subnets(self):
        """Nätet kortet redan sitter på, plus profilerna. Högst fyra /24 per adress."""
        import ipaddress
        nets = []
        a = self.current()
        if a:
            for ip, prefix in a.ips:
                prefix = int(prefix)
                if ip.startswith("169.254.") or prefix > 24 or prefix < 8:
                    continue
                try:
                    net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
                except ValueError:
                    continue
                if prefix >= 22:
                    parts = [".".join(str(s.network_address).split(".")[:3]) for s in net.subnets(new_prefix=24)]
                else:
                    parts = [".".join(ip.split(".")[:3])]
                nets.extend(parts)
        nets += self.discovery_subnets()
        return list(dict.fromkeys(nets))

    def run_discovery(self, on_update, on_done, network=False):
        import discovery
        a = self.current()
        own = [ip for ip, _ in a.ips] if a else []
        subnets = self.network_subnets() if network else self.discovery_subnets()

        def worker():
            try:
                found = discovery.discover(
                    self.iface_name, subnets, own_ips=own,
                    src_host=200, duration=float(self.cfg["discovery"].get("duration", 5)),
                    on_update=on_update, full_network=network)
                on_done(found, None)
            except Exception as e:
                log(f"Sökfel: {e!r}")
                on_done({}, str(e))
        threading.Thread(target=worker, daemon=True).start()

    def join_device_subnet(self, device_ip):
        import discovery
        my = discovery.suggest_own_ip(device_ip, 200)
        self.apply_static(my, "255.255.255.0", "", label=f"→ {device_ip}:")

    # ------------------------------------------------------------ ZeroTier

    def connect_site(self, site):
        def worker():
            try:
                if self.active_site and self.active_site is not site:
                    self.disconnect_site(silent=True)
                self.notify(f"Ansluter till {site['name']}…")
                if not ztctl.join(site["network_id"]):
                    self.notify("FEL: kunde inte joina ZeroTier-nätet")
                    return
                ok = False
                for _ in range(30):
                    if ztctl.is_network_ok(site["network_id"]):
                        ok = True
                        break
                    time.sleep(1)
                if not ok:
                    self.notify("Nätet auktoriserades inte inom 30 s — kolla ZeroTier Central")
                ztctl.route_delete(site["remote_subnet"])
                time.sleep(0.3)
                if ztctl.route_add(site["remote_subnet"], site["subnet_mask"], site["router_zt_ip"]):
                    self.active_site = site
                    self.notify(f"Ansluten: {site['name']}")
                else:
                    self.notify("FEL: kunde inte lägga in rutt")
                self.refresh_icon(force=True)
            except Exception as e:
                log(f"ZT-fel: {e}")
                self.notify(f"Fel: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def disconnect_site(self, silent=False):
        site = self.active_site
        if not site:
            if not silent:
                self.notify("Ingen ZeroTier-plats ansluten")
            return
        ztctl.route_delete(site["remote_subnet"])
        ztctl.leave(site["network_id"])
        self.active_site = None
        if not silent:
            self.notify(f"Frånkopplad: {site['name']}")
        self.refresh_icon(force=True)

    # ------------------------------------------------------------ emulerat uttag

    def uttag_cfg(self):
        return self.cfg.setdefault("uttag", {})

    def _active_link(self):
        mode = (self.uttag_cfg().get("mode") or "net")
        return {"usb": self.usb_link, "com": self.com_link}.get(mode, self.uttag_engine)

    def uttag_snapshot(self):
        eng = self._active_link()
        if eng:
            snap = eng.snapshot()
            # Nätbryggan har motpartslistan. USB/COM behåller den om nätet också lyssnar.
            if not snap.get("peers") and self.uttag_engine and eng is not self.uttag_engine:
                snap["peers"] = self.uttag_engine.snapshot().get("peers") or []
            snap["link"] = {"usb": "usb", "com": "com"}.get(self.uttag_cfg().get("mode") or "net", "net")
            snap.setdefault("live", False)
            return snap
        u = self.cfg.get("uttag", {})
        return {"running": False, "role": u.get("role", "uttag"), "iface": u.get("interface", ""),
                "status": "Stoppad", "peer_ip": "", "peer_name": "", "tx": 0, "rx": 0,
                "dropped": 0, "peers": [], "short": "", "live": False, "link": ""}

    def _stop_links_locked(self):
        for name in ("uttag_engine", "usb_link", "com_link"):
            eng = getattr(self, name)
            if not eng:
                continue
            try:
                eng.stop()
            except Exception as e:
                log(f"uttag stop {name}: {e}")
            setattr(self, name, None)

    def uttag_save(self, **fields):
        def upd(c):
            c.setdefault("uttag", {}).update(fields)
        self.update_cfg(upd)

    def uttag_start(self):
        u = self.uttag_cfg()
        mode = u.get("mode") or "net"
        role = u.get("role") or "uttag"
        if mode == "net":
            iface = (u.get("interface") or "").strip()
            if not iface:
                self.notify("Välj vilket nätverkskort som är uttaget")
                return
            if role == "uttag":
                if not uttag.valid_pin(u.get("pin")):
                    self.notify("Ange en PIN med 4 till 8 siffror. Den som kopplar på måste skriva samma kod.")
                    return
                a = netif.find_adapter(iface, self.adapters())
                if a and netif.is_transport_adapter(a):
                    self.notify("ZeroTier-kortet är transporten, inte uttaget. Välj kabeln mot PLC:n.")
                    return
        elif mode == "usb" and not (u.get("usb_busid") or "").strip():
            self.notify("Välj USB-enheten först")
            return
        elif mode == "com" and role == "uttag" and not (u.get("com_port") or "").strip():
            self.notify("Välj COM-porten först")
            return

        def worker():
            with self._uttag_lock:
                self._stop_links_locked()
                try:
                    eng = self._make_link(u, mode, role)
                    eng.start()
                except Exception as e:
                    log(f"uttag start ({mode}): {e}")
                    self.notify(str(e)[:220])
                    self._stop_links_locked()
                    return
                if mode == "com" and getattr(eng, "virtual_pair", None):
                    app_port, bridge = eng.virtual_pair
                    self.cfg.setdefault("uttag", {})["com_app_port"] = app_port
                    self.cfg["uttag"]["com_bridge_port"] = bridge
                    C.save_config(self.cfg)
            self.refresh_icon(force=True)
            self.notify(eng.snapshot()["status"])
        threading.Thread(target=worker, daemon=True).start()

    def _make_link(self, u, mode, role):
        peer = u.get("peer_ip") or ""
        changed = lambda: self.refresh_icon(force=True)
        if mode == "usb":
            eng = usbshare.UsbLink(role, u.get("usb_busid") or "", peer, on_change=changed)
            self.usb_link = eng
            return eng
        if mode == "com":
            eng = serialshare.ComLink(
                role, u.get("com_port") or "", peer,
                baud=u.get("com_baud") or 9600, bytesize=u.get("com_data") or 8,
                parity=u.get("com_parity") or "N", stopbits=u.get("com_stop") or 1,
                on_change=changed,
                virtual_pair=((u.get("com_app_port") or ""), (u.get("com_bridge_port") or "")))
            self.com_link = eng
            return eng
        eng = uttag.UttagEngine(
            role, (u.get("interface") or "").strip(), port=u.get("port") or uttag.PORT,
            manual_peer=peer, on_change=changed, pin=u.get("pin") or "",
            board=self.share_board)
        self.uttag_engine = eng
        return eng

    def uttag_stop(self):
        def worker():
            with self._uttag_lock:
                self._stop_links_locked()
            self.refresh_icon(force=True)
            self.notify("Uttaget är stoppat")
        threading.Thread(target=worker, daemon=True).start()

    def uttag_connect(self, ip, pin, name=""):
        """Koppla verktygssidan till ett delat nätverksuttag. PIN kollas av uttagssidan."""
        import ipaddress
        pin = str(pin or "").strip()
        ip = (ip or "").strip()
        if not uttag.valid_pin(pin):
            self.notify("PIN ska vara 4 till 8 siffror.")
            return
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            self.notify("Ogiltig IP-adress")
            return
        u = self.uttag_cfg()
        iface = (u.get("interface") or "").strip()
        if not iface:
            self.notify("Välj vilket nätverkskort programmet ska använda")
            return

        def worker():
            eng = None
            with self._uttag_lock:
                eng = self.uttag_engine
                if eng and eng.running and eng.role == "uttag":
                    self.notify("Du delar redan ett uttag. Stoppa det innan du kopplar på någon annan.")
                    return
                if not (eng and eng.running and eng.role == "verktyg"):
                    self._stop_links_locked()
                    u2 = dict(self.uttag_cfg())
                    u2["role"] = "verktyg"
                    u2["mode"] = "net"
                    u2["interface"] = iface
                    u2["peer_ip"] = ip
                    try:
                        eng = self._make_link(u2, "net", "verktyg")
                        eng.start()
                    except Exception as e:
                        log(f"uttag koppla på: {e}")
                        self.notify(str(e)[:220])
                        self._stop_links_locked()
                        return
                try:
                    eng.connect_to(ip, pin, name)
                except Exception as e:
                    self.notify(str(e)[:220])
                    return
            self.uttag_save(role="verktyg", mode="net", peer_ip=ip)
            self.refresh_icon(force=True)

        threading.Thread(target=worker, daemon=True).start()

    def uttag_set_peer(self, ip):
        ip = (ip or "").strip()
        try:
            if ip:
                import ipaddress
                ipaddress.IPv4Address(ip)
        except ValueError:
            self.notify("Ogiltig IP-adress")
            return
        self.uttag_save(peer_ip=ip)
        for eng in (self.uttag_engine, self.usb_link, self.com_link):
            if eng:
                try:
                    eng.set_manual_peer(ip)
                except ValueError:
                    self.notify("Ogiltig IP-adress")

    def uttag_apply_tool_ip(self, ip, prefix):
        u = self.uttag_cfg()
        if (u.get("role") or "uttag") != "verktyg":
            self.notify("IP sätts på verktygssidan, där TIA eller GX Works körs")
            return
        iface = (u.get("interface") or "").strip()
        if not iface:
            self.notify("Välj nätverkskortet som programvaran ska använda")
            return
        try:
            import ipaddress
            ipaddress.IPv4Address(ip)
            prefix = int(prefix)
            if not 1 <= prefix <= 30:
                raise ValueError
            mask = C.prefix_to_mask(prefix)
        except ValueError:
            self.notify("Ogiltig IP eller prefix")
            return
        self.uttag_save(tool_ip=ip, tool_prefix=prefix)

        def worker():
            ok, msg, dt = netif.set_static(iface, ip, mask, "")
            self.adapters(refresh=True)
            self.refresh_icon(force=True)
            if ok:
                self.notify(f"{iface}: {ip}/{prefix}  ({dt:.1f} s)")
            else:
                self.notify(f"Misslyckades: {msg[:180]}")
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ autostart / fäst

    def autostart_state(self):
        return winint.autostart_enabled()

    def set_autostart(self, on):
        ok, msg = winint.set_autostart(on)
        self.notify(("Autostart på" if on else "Autostart av") if ok else f"Autostart misslyckades: {msg}")
        self.rebuild_menu()

    def set_pin(self, on):
        self.update_cfg(lambda c: c["settings"].__setitem__("pin_tray", on))
        if not winint.set_pinned(on):
            if on:
                self.notify("Ikonen fästs när Windows har registrerat den (oftast inom några sekunder)")
            threading.Thread(target=self._pin_retry, args=(on,), daemon=True).start()

    def _pin_retry(self, on):
        for _ in range(20):
            time.sleep(3)
            if winint.set_pinned(on):
                log("Ikonens fästläge satt")
                return

    # ------------------------------------------------------------ meny

    def rebuild_menu(self):
        if self.icon:
            try:
                self.icon.menu = self.build_menu()
                self.icon.update_menu()
            except Exception as e:
                log(f"Menyfel: {e}")

    def build_menu(self):
        s = self.snapshot()
        ads = self.adapters()
        items = []

        head = f"{s['iface'] or 'Inget kort'}:  {MODE_SHORT[s['mode']]}"
        if s["ip"] and s["mode"] in ("static", "dhcp", "apipa"):
            head += f"  {s['ip']}/{s['prefix']}"
        items.append(Item(head, None, enabled=False))

        iface_items = []
        for a in ads:
            ip = a.primary_ip
            t = f"{a.name}   ({ip[0]})" if ip else f"{a.name}   ({'ingen kabel' if not a.up else 'ingen IP'})"
            iface_items.append(Item(t, (lambda n: lambda i, it: self.set_interface(n))(a.name),
                                    checked=(lambda n: lambda it: self.iface_name == n)(a.name), radio=True))
        if not iface_items:
            iface_items.append(Item("Inga kort hittades", None, enabled=False))
        items.append(Item("Nätverkskort", Menu(*iface_items)))
        items.append(Menu.SEPARATOR)

        for p in self.cfg.get("ip_presets", []):
            active = s["mode"] == "static" and s["ip"] and C.preset_matches(p, s["ip"], s["prefix"])
            more = "…" if C.preset_questions(p) else ""
            items.append(Item(f"{p.get('name')}{more}", (lambda p: lambda i, it: self.apply_preset(p))(p),
                              checked=(lambda a: lambda it: a)(bool(active)), radio=True,
                              enabled=not self.busy))
        items.append(Item("Manuell IP…", lambda i, it: self.ui.manual_ip(), enabled=not self.busy))
        items.append(Item("DHCP (automatisk)", lambda i, it: self.apply_dhcp(),
                          checked=(lambda a: lambda it: a)(s["mode"] in ("dhcp", "apipa")), radio=True,
                          enabled=not self.busy))
        items.append(Menu.SEPARATOR)

        ok, why = self.discovery_available()
        items.append(Item("Hitta enheter…" if ok else "Hitta enheter  (kräver Npcap)",
                          lambda i, it: self.ui.open_main("discover"), enabled=ok))

        sites = self.cfg.get("sites", [])
        zt_items = []
        for site in sites:
            zt_items.append(Item(site["name"], (lambda st: lambda i, it: self.connect_site(st))(site),
                                 checked=(lambda st: lambda it: self.active_site is not None and
                                          self.active_site.get("network_id") == st.get("network_id"))(site),
                                 radio=True))
        if not zt_items:
            zt_items.append(Item("Inga platser", None, enabled=False))
        zt_items += [Menu.SEPARATOR,
                     Item("Koppla från", lambda i, it: self.disconnect_site(), enabled=self.active_site is not None),
                     Item("Hantera platser…", lambda i, it: self.ui.open_main("zt"))]
        zt_label = f"ZeroTier  ({s['site']})" if s["vpn"] else "ZeroTier"
        items.append(Item(zt_label, Menu(*zt_items)))
        u = self.uttag_snapshot()
        if u["running"] and u.get("short"):
            ut_label = f"Uttag  ({u['short']})"
        else:
            ut_label = "Uttag…"
        items.append(Item(ut_label, lambda i, it: self.ui.open_main("uttag")))
        items.append(Menu.SEPARATOR)

        items.append(Item("Öppna ZT-Tray", lambda i, it: self.ui.open_main("quick"), default=True))
        items.append(Item("Inställningar…", lambda i, it: self.ui.open_main("settings")))
        items.append(Item(f"Version {C.APP_VERSION}", None, enabled=False))
        items.append(Item("Avsluta", lambda i, it: self.quit()))
        return Menu(*items)

    def quit(self):
        """Avsluta på riktigt. Menyn körs i ikonens meddelandeloop, så städningen
        sker i en egen tråd och processen avslutas även om loopen hänger kvar."""
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        threading.Thread(target=self._quit_and_exit, name="avsluta", daemon=True).start()

    def _quit_and_exit(self):
        def force_exit():
            log("processen avslutas")
            os._exit(0)

        watchdog = threading.Timer(20, force_exit)
        watchdog.daemon = True
        watchdog.start()
        try:
            log("avslutar")
            with self._uttag_lock:
                self._stop_links_locked()
            board = getattr(self, "share_board", None)
            if board:
                try:
                    board.stop()
                except Exception as e:
                    log(f"uttag lista stopp: {e}")
            if self.active_site:
                try:
                    self.disconnect_site(silent=True)
                except Exception as e:
                    log(f"frånkoppling vid avslut: {e}")
            if self.icon:
                try:
                    self.icon.visible = False
                except Exception:
                    pass
                try:
                    self.icon.stop()
                except Exception as e:
                    log(f"ikon stopp: {e}")
        except Exception as e:
            log(f"avslut: {e}")
        force_exit()


def single_instance():
    """Hindra två ikoner samtidigt (named mutex)."""
    if sys.platform != "win32":
        return True
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, "Global\\ZT-Tray-v2-singleton")
    return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def selftest():
    """zt-tray.exe --selftest : kontrollerar att allt i bygget går att ladda (skriver till loggen)."""
    log("=== självtest ===")
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        import sv_ttk
        sv_ttk.set_theme("light")
        from PIL import ImageTk
        ImageTk.PhotoImage(icons.render("static", True, size=44))
        r.destroy()
        log("OK: Tk + sv_ttk + ikoner")
    except Exception as e:
        log(f"FEL: Tk/tema: {e!r}")
    try:
        ads = netif.list_adapters()
        log(f"OK: {len(ads)} nätverkskort: " + ", ".join(f"{a.name} [{a.mode}]" for a in ads))
    except Exception as e:
        log(f"FEL: adaptrar: {e!r}")
    log(f"Npcap: {'ja' if winint.npcap_installed() else 'nej'}, scapy i bygget: "
        f"{'ja' if winint.scapy_available() else 'nej'}, fästa ikon stöds: {winint.pin_supported()}")
    if winint.scapy_available():
        try:
            import discovery
            discovery.load_scapy()
            log("OK: scapy laddat")
        except Exception as e:
            log(f"Info: scapy kunde inte laddas här ({e!r})")
    try:
        uttag.selftest()
        import discovery
        discovery.selftest()
        usbshare.selftest()
        serialshare.selftest()
    except Exception as e:
        log(f"FEL: uttag/discovery: {e!r}")
    log("=== självtest klart ===")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    if sys.platform == "win32" and not is_admin():
        log("Saknar admin — startar om med UAC")
        relaunch_as_admin()
        os._exit(0)
    if not single_instance():
        log("Redan igång — avslutar")
        os._exit(0)

    log("=== ZT-Tray v2 startar ===")
    app = App()
    from ui import UiThread
    app.ui = UiThread(app)

    s = app.snapshot()
    app.icon = pystray.Icon("zt-tray", icon=icons.render(s["mode"], s["vpn"]),
                            title=app.tooltip(s), menu=app.build_menu())
    threading.Thread(target=app.monitor_loop, daemon=True).start()

    if app.cfg["settings"].get("pin_tray", True) and winint.pin_supported():
        threading.Thread(target=app._pin_retry, args=(True,), daemon=True).start()
    ok, _ = app.discovery_available()
    if ok:
        import discovery
        threading.Thread(target=discovery.warmup, daemon=True).start()

    app.icon.run()


if __name__ == "__main__":
    main()
