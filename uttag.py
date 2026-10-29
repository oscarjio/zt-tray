"""
Emulerat nätverksuttag mellan två ZT-Tray-datorer.

Kabeln sitter i datorn med rollen "uttag" (hos kunden). TIA Portal, GX Works
och IP-adressen finns på datorn med rollen "verktyg" (kollega eller RDP).
Råa Ethernet-ramar (ARP, broadcast, Profinet DCP) skickas som UDP över
ZeroTier-adresserna. ZeroTiers egna brygga används inte, så kontrollern
behöver inte slå på "Allow Ethernet Bridging".

Den som delar annonserar uttaget på UDP 47923. PIN-koden ingår inte i
annonseringen. Motparten skickar ett bevis (hash av engångstal + PIN) som
unicast, och ramar släpps fram först när det stämmer.

Npcap krävs på båda sidor för själva bryggan. Realtids-IO (Profinet RT) tål
sällan fördröjningen. Programmering och nedladdning är det här läget är till för.
"""

import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import struct
import threading
import time

from config import log

PORT = 47921
SHARE_PORT = 47923
MAGIC = b"ZTUT"
VERSION = 1
HELLO = 1
HELLO_ACK = 2
FRAME = 3
BYE = 4
JOIN = 5
CHALLENGE = 6
CONNECT = 7
ACCEPT = 8
DENY = 9
_HDR = struct.Struct("!4sBBHII")  # magic, ver, typ, flags, seq, length
_MAX_FRAME = 2000
_ECHO_TTL = 1.0
_SHARE_MAGIC = b"ZTSH"
_SHARE_TTL = 12
_PIN_FAILS = 5
_PIN_LOCKOUT = 60
_JOIN_WAIT = 12


def pack_msg(typ, payload, seq=0, flags=0):
    payload = payload or b""
    return _HDR.pack(MAGIC, VERSION, typ, flags, seq & 0xFFFFFFFF, len(payload)) + payload


def unpack_msg(data):
    if not data or len(data) < _HDR.size:
        return None
    magic, ver, typ, flags, seq, length = _HDR.unpack_from(data)
    if magic != MAGIC or ver != VERSION or length > _MAX_FRAME:
        return None
    end = _HDR.size + length
    if end > len(data):
        return None
    return typ, flags, seq, data[_HDR.size:end]


def is_tunnel_frame(frame, port=PORT):
    """True om ramen är vår egen UDP-tunnel. Den får inte bryggas tillbaka."""
    if len(frame) < 14:
        return False
    off = 12
    et = int.from_bytes(frame[off:off + 2], "big")
    if et == 0x8100 and len(frame) >= 18:
        off = 16
        et = int.from_bytes(frame[off:off + 2], "big")
    if et != 0x0800:
        return False
    ip_off = off + 2
    if len(frame) < ip_off + 20:
        return False
    vihl = frame[ip_off]
    if vihl >> 4 != 4:
        return False
    ihl = (vihl & 0x0F) * 4
    if ihl < 20 or frame[ip_off + 9] != 17:
        return False
    udp = ip_off + ihl
    if len(frame) < udp + 4:
        return False
    src = int.from_bytes(frame[udp:udp + 2], "big")
    dst = int.from_bytes(frame[udp + 2:udp + 4], "big")
    return src == port or dst == port


class EchoGuard:
    """Släpp ramar vi själva just skickade in på kortet, så de inte studsar."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []  # (tid, hash)

    def remember(self, frame):
        now = time.time()
        digest = hashlib.blake2s(frame, digest_size=8).digest()
        with self._lock:
            self._items.append((now, digest))
            self._drop_old(now)

    def seen(self, frame):
        now = time.time()
        digest = hashlib.blake2s(frame, digest_size=8).digest()
        with self._lock:
            self._drop_old(now)
            return any(h == digest for _, h in self._items)

    def _drop_old(self, now):
        if len(self._items) > 256 or (self._items and now - self._items[0][0] > _ECHO_TTL):
            self._items = [(t, h) for t, h in self._items if now - t <= _ECHO_TTL]


def broadcast_of(ip_prefix):
    """'10.244.17.1/24' -> '10.244.17.255'. None om det inte är ett nät."""
    try:
        net = ipaddress.IPv4Interface(ip_prefix).network
    except ValueError:
        return None
    if net.prefixlen >= 31:
        return None
    return str(net.broadcast_address)


def valid_pin(pin):
    pin = str(pin or "").strip()
    return pin.isdigit() and 4 <= len(pin) <= 8


def pin_proof(nonce, pin):
    """Hash som motparten skickar. Själva PIN-koden lämnar inte datorn."""
    raw = (str(nonce) + "\n" + str(pin or "").strip()).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def proofs_match(nonce, pin, proof):
    expect = pin_proof(nonce, pin)
    proof = str(proof or "")
    if len(proof) != len(expect):
        return False
    return hmac.compare_digest(expect, proof)


def deny_text(reason):
    return {
        "fel_pin": "Fel PIN.",
        "upptagen": "Uttaget är redan kopplat till någon annan.",
        "manga": "För många försök. Vänta en stund.",
    }.get(reason or "", "Anslutningen nekades.")


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _loads(payload):
    try:
        obj = json.loads(payload.decode("utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _hello_payload(name, role, iface, nonce=""):
    body = {"name": name, "role": role, "iface": iface}
    # Nonce är ett engångstal, inte hemligheten. PIN ligger aldrig här.
    if nonce and role == "uttag":
        body["nonce"] = nonce
        body["sharing"] = True
    return _dump(body)


def _parse_hello(payload):
    d = _loads(payload)
    role = d.get("role")
    if role not in ("uttag", "verktyg"):
        return None
    nonce = str(d.get("nonce") or "")[:64]
    sharing = bool(d.get("sharing")) and role == "uttag" and bool(nonce)
    return {"name": str(d.get("name") or "?")[:60], "role": role,
            "iface": str(d.get("iface") or "")[:60],
            "nonce": nonce if sharing else "", "sharing": sharing}


def handle_sharer(peer_ip, nonce, pin, my_name, my_iface, src_ip, typ, obj, fails, now):
    """
    Beslut på uttagssidan. Sätter peer_ip bara när PIN-beviset stämmer.
    fails är {ip: (antal, låst_till)} och uppdateras på plats.
    """
    challenge = {"nonce": nonce, "name": my_name, "iface": my_iface}
    if typ == JOIN:
        if peer_ip and peer_ip != src_ip:
            return {"peer_ip": peer_ip, "accepted": False,
                    "replies": [(DENY, {"reason": "upptagen"})], "status": None}
        return {"peer_ip": peer_ip, "accepted": False,
                "replies": [(CHALLENGE, challenge)], "status": None}
    if typ != CONNECT:
        return {"peer_ip": peer_ip, "accepted": False, "replies": [], "status": None}
    count, until = fails.get(src_ip, (0, 0.0))
    if now < until:
        return {"peer_ip": peer_ip, "accepted": False,
                "replies": [(DENY, {"reason": "manga"})], "status": None}
    if peer_ip and peer_ip != src_ip:
        return {"peer_ip": peer_ip, "accepted": False,
                "replies": [(DENY, {"reason": "upptagen"})], "status": None}
    if str(obj.get("nonce") or "") != nonce:
        return {"peer_ip": peer_ip, "accepted": False,
                "replies": [(DENY, {"reason": "nonce"}), (CHALLENGE, challenge)],
                "status": None}
    if not proofs_match(nonce, pin, obj.get("proof")):
        count += 1
        if count >= _PIN_FAILS:
            fails[src_ip] = (0, now + _PIN_LOCKOUT)
        else:
            fails[src_ip] = (count, 0.0)
        return {"peer_ip": peer_ip, "accepted": False,
                "replies": [(DENY, {"reason": "fel_pin"})],
                "status": f"Fel PIN från {src_ip}."}
    fails.pop(src_ip, None)
    who = str(obj.get("name") or "?")[:60]
    return {"peer_ip": src_ip, "peer_name": who, "accepted": True,
            "replies": [(ACCEPT, {"name": my_name, "iface": my_iface})],
            "status": None}


def handle_tool(pending, src_ip, typ, obj):
    """Beslut på verktygssidan. pending uppdateras på plats tills ACCEPT eller DENY."""
    if not pending or pending.get("ip") != src_ip:
        return {"accept": False, "clear": False, "status": None}
    if typ == CHALLENGE:
        nonce = str(obj.get("nonce") or "")[:64]
        if nonce:
            pending["nonce"] = nonce
            pending["phase"] = "connect"
            pending["next"] = 0
            if obj.get("name"):
                pending["name"] = str(obj["name"])[:60]
        return {"accept": False, "clear": False, "status": None}
    if typ == ACCEPT:
        return {"accept": True, "clear": True, "status": None,
                "peer_ip": src_ip,
                "peer_name": str(obj.get("name") or pending.get("name") or "?")[:60],
                "iface": str(obj.get("iface") or "")[:60]}
    if typ == DENY:
        reason = str(obj.get("reason") or "")
        if reason == "nonce":
            pending["phase"] = "join"
            pending["nonce"] = ""
            pending["next"] = 0
            return {"accept": False, "clear": False, "status": None}
        return {"accept": False, "clear": True, "status": deny_text(reason)}
    return {"accept": False, "clear": False, "status": None}


def pack_share(obj):
    body = _dump(obj)
    if len(body) > 500:
        raise ValueError("för stor annons")
    return _SHARE_MAGIC + struct.pack("!H", len(body)) + body


def unpack_share(data):
    if not data or len(data) < 6 or data[:4] != _SHARE_MAGIC:
        return None
    length = struct.unpack_from("!H", data, 4)[0]
    if length > 500 or 6 + length > len(data):
        return None
    obj = _loads(data[6:6 + length])
    return obj or None


def _udp_from(src_ip, dest, packet):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind((src_ip, 0))
        s.sendto(packet, dest)
    except OSError as e:
        log(f"uttag udp från {src_ip}: {e}")
    finally:
        s.close()


class ShareBoard:
    """Lyssnar efter delade nätverksuttag. Kräver inte Npcap och körs även när bryggan är av."""

    def __init__(self):
        self._lock = threading.Lock()
        self._shares = {}
        self._own = set()
        self._local_nonce = ""
        self._sock = None
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        try:
            sock.bind(("0.0.0.0", SHARE_PORT))
        except OSError as e:
            sock.close()
            log(f"uttag lista: kan inte lyssna på UDP {SHARE_PORT}: {e}")
            return
        self._sock = sock
        self._running = True
        self._allow_firewall()
        self._thread = threading.Thread(target=self._loop, name="uttag-lista", daemon=True)
        self._thread.start()
        log(f"uttag lista lyssnar på UDP {SHARE_PORT}")

    def stop(self):
        self._running = False
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def note_local(self, nonce):
        with self._lock:
            self._local_nonce = nonce or ""

    def clear_local(self):
        with self._lock:
            self._local_nonce = ""

    def send(self, obj, sources):
        """sources är lista av 'ip/prefix' från ZeroTier. Den egna annonsen filtreras bort."""
        try:
            packet = pack_share(obj)
        except ValueError as e:
            log(f"uttag lista: {e}")
            return
        own = set()
        for ippx in sources or []:
            src = str(ippx).split("/")[0]
            own.add(src)
            bcast = broadcast_of(ippx)
            if bcast:
                _udp_from(src, (bcast, SHARE_PORT), packet)
        with self._lock:
            if own:
                self._own = own

    def shares(self):
        now = time.time()
        with self._lock:
            local = self._local_nonce
            own = set(self._own)
            out = []
            dead = []
            for ip, info in self._shares.items():
                if now - info.get("last", 0) > _SHARE_TTL:
                    dead.append(ip)
                    continue
                if ip in own or (local and info.get("nonce") == local):
                    continue
                out.append({"ip": ip, "name": info.get("name", "?"),
                            "iface": info.get("iface", ""), "nonce": info.get("nonce", "")})
            for ip in dead:
                self._shares.pop(ip, None)
            out.sort(key=lambda p: p["name"].lower())
            return out

    def _loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                log(f"uttag lista: {e}")
                continue
            try:
                self._on(data, addr)
            except Exception as e:
                log(f"uttag lista: {e}")

    def _on(self, data, addr):
        obj = unpack_share(data)
        if not obj:
            return
        ip = addr[0]
        with self._lock:
            if ip in self._own:
                return
            if not obj.get("sharing"):
                self._shares.pop(ip, None)
                return
            nonce = str(obj.get("nonce") or "")[:64]
            if not nonce or nonce == self._local_nonce:
                return
            self._shares[ip] = {
                "name": str(obj.get("name") or "?")[:60],
                "iface": str(obj.get("iface") or "")[:60],
                "nonce": nonce,
                "last": time.time(),
            }

    def _allow_firewall(self):
        import ztctl
        ztctl.run(["netsh", "advfirewall", "firewall", "add", "rule",
                    f"name=ZT-Tray-uttag-lista", "dir=in", "action=allow",
                    "protocol=UDP", f"localport={SHARE_PORT}"])


class UttagEngine:
    def __init__(self, role, iface_name, port=PORT, manual_peer="", on_change=None,
                 pin="", board=None):
        if role not in ("uttag", "verktyg"):
            raise ValueError(role)
        self.role = role
        self.iface_name = iface_name
        self.port = int(port or PORT)
        self.manual_peer = (manual_peer or "").strip()
        self.on_change = on_change
        self.pin = str(pin or "").strip()
        self.board = board
        self.running = False
        self.status = "Stoppad"
        self.peer_ip = ""
        self.peer_name = ""
        self.tx = 0
        self.rx = 0
        self.dropped = 0
        self._peers = {}
        self._own_ips = set()
        self._seq = 0
        self._sock = None
        self._npf = None
        self._l2 = None
        self._echo = EchoGuard()
        self._cleared = False
        self._lock = threading.Lock()
        self._threads = []
        self._nonce = ""
        self._pending = None
        self._fails = {}

    # ------------------------------------------------------------- livscykel

    def start(self):
        if self.running:
            return
        if self.role == "uttag" and not valid_pin(self.pin):
            raise RuntimeError(
                "Ange en PIN med 4 till 8 siffror. Den som kopplar på måste skriva samma kod.")
        import winint
        if not winint.npcap_installed():
            raise RuntimeError("Npcap är inte installerat. Det behövs för att läsa ramar på kortet.")
        if not winint.scapy_available():
            raise RuntimeError("scapy saknas i den här installationen.")
        import discovery
        sc = discovery.load_scapy()
        self._npf = discovery.resolve_iface(sc, self.iface_name)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0)
        try:
            sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            sock.close()
            raise RuntimeError(f"Kan inte lyssna på UDP {self.port}: {e}") from e
        self._sock = sock
        self._allow_firewall()

        try:
            if self.role == "uttag":
                import netif
                snap = netif.snapshot_ip(self.iface_name)
                netif.save_ip_restore(snap)
                ok, msg = netif.clear_ipv4(self.iface_name)
                self._cleared = True
                log(f"uttag: släppte IP på {self.iface_name} ({'ok' if ok else msg})")
                self._nonce = secrets.token_hex(16)
                if self.board:
                    self.board.note_local(self._nonce)
            self.running = True
            if self.role == "uttag":
                self.status = f"Delar {self.iface_name}. Väntar på PIN."
            else:
                self.status = "Välj ett delat uttag och ange PIN."
            self._threads = [
                threading.Thread(target=self._recv_loop, name="uttag-recv", daemon=True),
                threading.Thread(target=self._announce_loop, name="uttag-hello", daemon=True),
                threading.Thread(target=self._capture_loop, name="uttag-cap", daemon=True),
            ]
            for t in self._threads:
                t.start()
        except Exception:
            self.running = False
            self._nonce = ""
            if self.board:
                self.board.clear_local()
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
            self._restore_ip()
            raise
        self._emit()
        log(f"uttag start: roll={self.role} kort={self.iface_name} port={self.port}")

    def stop(self):
        if not self.running and self._sock is None:
            self._restore_ip()
            return
        self.running = False
        peer = self.peer_ip
        try:
            if self._sock and peer:
                self._send(BYE, b"", (peer, self.port))
        except Exception:
            pass
        if self.role == "uttag" and self.board:
            try:
                import ztctl
                self.board.send({"sharing": False}, ztctl.my_zt_ips())
                self.board.clear_local()
            except Exception as e:
                log(f"uttag lista stopp: {e}")
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        for t in self._threads:
            t.join(timeout=2)
        self._threads = []
        l2, self._l2 = self._l2, None
        if l2:
            try:
                l2.close()
            except Exception:
                pass
        self._restore_ip()
        self.peer_ip = ""
        self.peer_name = ""
        self._pending = None
        self._nonce = ""
        self.status = "Stoppad"
        self._emit()
        log("uttag stoppad")

    def set_manual_peer(self, ip):
        """Kommer ihåg en adress för unicast. Öppnar inte bryggan."""
        ip = (ip or "").strip()
        if ip:
            ipaddress.IPv4Address(ip)
        self.manual_peer = ip
        self._emit()

    def connect_to(self, ip, pin, name=""):
        """Verktygssidan ber om att få koppla på. Ramar väntar tills PIN godkänts."""
        if self.role != "verktyg":
            raise RuntimeError("Byt till Verktyg för att koppla på ett delat uttag.")
        if not self.running:
            raise RuntimeError("Starta verktygssidan först.")
        pin = str(pin or "").strip()
        if not valid_pin(pin):
            raise RuntimeError("PIN ska vara 4 till 8 siffror.")
        ipaddress.IPv4Address(ip)
        name = str(name or "")[:60]
        old = ""
        with self._lock:
            if self.peer_ip and self.peer_ip != ip:
                old = self.peer_ip
            self.peer_ip = ""
            self.peer_name = ""
            self._pending = {
                "ip": ip, "pin": pin, "name": name or ip,
                "phase": "join", "nonce": "",
                "until": time.time() + _JOIN_WAIT, "next": 0,
            }
            self.status = f"Frågar {name or ip}…"
        if old:
            self._send(BYE, b"", (old, self.port))
        self._emit()
        log(f"uttag frågar {ip}")

    def snapshot(self):
        now = time.time()
        with self._lock:
            peers = []
            for ip, info in self._peers.items():
                if now - info.get("last", 0) > _SHARE_TTL:
                    continue
                peers.append({"ip": ip, "name": info.get("name", "?"),
                              "role": info.get("role", ""), "iface": info.get("iface", ""),
                              "nonce": info.get("nonce", ""), "sharing": bool(info.get("sharing"))})
            peers.sort(key=lambda p: (p["role"] == self.role, p["name"].lower()))
            return {
                "running": self.running,
                "role": self.role,
                "iface": self.iface_name,
                "status": self.status,
                "peer_ip": self.peer_ip,
                "peer_name": self.peer_name,
                "tx": self.tx,
                "rx": self.rx,
                "dropped": self.dropped,
                "peers": peers,
                "short": self._short_locked(),
                "live": bool(self.running and self.peer_ip),
            }

    # ------------------------------------------------------------- nät

    def _my_hello(self):
        nonce = self._nonce if self.role == "uttag" else ""
        return _hello_payload(socket.gethostname(), self.role, self.iface_name, nonce)

    def _announce_once(self):
        import ztctl
        payload = self._my_hello()
        packet = pack_msg(HELLO, payload, seq=self._next_seq())
        sources = ztctl.my_zt_ips()
        sent = False
        own = set()
        for ippx in sources:
            src = ippx.split("/")[0]
            own.add(src)
        # Sätts innan sändning, så vår egen broadcast inte räknas som en motpart.
        self._own_ips = own
        for ippx in sources:
            bcast = broadcast_of(ippx)
            src = ippx.split("/")[0]
            if not bcast:
                continue
            _udp_from(src, (bcast, self.port), packet)
            sent = True
        if self.manual_peer:
            self._send(HELLO, payload, (self.manual_peer, self.port))
            sent = True
        if self.role == "uttag" and self._nonce and self.board:
            self.board.send({
                "name": socket.gethostname(),
                "iface": self.iface_name,
                "nonce": self._nonce,
                "sharing": True,
            }, sources)
        if not sent and not self.peer_ip:
            with self._lock:
                if self.running and not self.peer_ip:
                    self.status = "Ingen ZeroTier-adress. Anslut en plats först, eller skriv motpartens ZT-IP."
            self._emit()

    def _announce_loop(self):
        tick = 0
        while self.running:
            tick += 1
            if tick % 20 == 1:
                try:
                    self._announce_once()
                except Exception as e:
                    log(f"uttag hello: {e}")
            try:
                self._drive_pending()
            except Exception as e:
                log(f"uttag pin: {e}")
            if not self.running:
                return
            time.sleep(0.1)

    def _drive_pending(self):
        job = None
        timed_out = False
        with self._lock:
            p = self._pending
            if not p:
                return
            now = time.time()
            if now > p["until"]:
                self._pending = None
                if not self.peer_ip:
                    self.status = "Fick inget svar från uttaget."
                    timed_out = True
            elif now >= p.get("next", 0):
                p["next"] = now + 0.7
                job = (p["phase"], p["ip"], p["pin"], p.get("nonce") or "")
        if timed_out:
            self._emit()
            return
        if not job:
            return
        phase, ip, pin, nonce = job
        if phase == "join":
            self._send(JOIN, _dump({"name": socket.gethostname()}), (ip, self.port))
        elif phase == "connect" and nonce:
            self._send(CONNECT, _dump({
                "name": socket.gethostname(),
                "proof": pin_proof(nonce, pin),
                "nonce": nonce,
            }), (ip, self.port))

    def _recv_loop(self):
        while self.running:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                log(f"uttag recv: {e}")
                continue
            try:
                self._on_datagram(data, addr)
            except Exception as e:
                log(f"uttag paket: {e}")

    def _on_datagram(self, data, addr):
        parsed = unpack_msg(data)
        if not parsed:
            return
        typ, _flags, _seq, payload = parsed
        ip = addr[0]
        if ip in self._own_ips:
            return
        if typ in (HELLO, HELLO_ACK):
            info = _parse_hello(payload)
            if not info:
                return
            if self._nonce and info.get("nonce") == self._nonce:
                return
            with self._lock:
                self._peers[ip] = {**info, "last": time.time()}
            if typ == HELLO:
                self._send(HELLO_ACK, self._my_hello(), (ip, self.port))
            self._emit()
            return
        if typ in (JOIN, CONNECT) and self.role == "uttag":
            self._on_sharer(ip, typ, _loads(payload))
            return
        if typ in (CHALLENGE, ACCEPT, DENY) and self.role == "verktyg":
            self._on_tool(ip, typ, _loads(payload))
            return
        if typ == BYE and ip == self.peer_ip:
            with self._lock:
                self.peer_ip = ""
                self.peer_name = ""
                if self._pending and self._pending.get("ip") == ip:
                    self._pending = None
                self.status = "Motparten kopplade från. Lyssnar igen…"
            self._emit()
            return
        if typ == FRAME and ip == self.peer_ip and payload:
            self._inject(payload)

    def _on_sharer(self, ip, typ, obj):
        replies = []
        with self._lock:
            result = handle_sharer(
                self.peer_ip, self._nonce, self.pin, socket.gethostname(), self.iface_name,
                ip, typ, obj, self._fails, time.time())
            replies = result["replies"]
            if result["accepted"]:
                self.peer_ip = result["peer_ip"]
                self.peer_name = result.get("peer_name") or ""
                self.status = f"Delar {self.iface_name} ↔ {self.peer_name} ({ip})"
                log(f"uttag motpart: {self.peer_name} {ip}")
            elif result.get("status"):
                self.status = result["status"]
        for rtyp, robj in replies:
            self._send(rtyp, _dump(robj), (ip, self.port))
        if result["accepted"] or result.get("status"):
            self._emit()

    def _on_tool(self, ip, typ, obj):
        emit = False
        with self._lock:
            result = handle_tool(self._pending, ip, typ, obj)
            if result.get("clear"):
                self._pending = None
            if result.get("accept"):
                self.peer_ip = result["peer_ip"]
                self.peer_name = result.get("peer_name") or ""
                card = result.get("iface") or self.iface_name
                self.status = f"Tar emot {card} ↔ {self.peer_name} ({ip})"
                log(f"uttag motpart: {self.peer_name} {ip}")
                emit = True
            elif result.get("status"):
                self.status = result["status"]
                emit = True
        if emit:
            self._emit()

    def _capture_loop(self):
        import discovery
        sc = discovery.load_scapy()
        while self.running:
            try:
                sc.sniff(iface=self._npf, store=0, timeout=1,
                         prn=lambda p: self._on_local_frame(bytes(p)),
                         stop_filter=lambda p: not self.running)
            except Exception as e:
                if not self.running:
                    break
                log(f"uttag sniff: {e}")
                with self._lock:
                    self.status = f"Kan inte läsa {self.iface_name}: {e}"
                self._emit()
                time.sleep(1)

    def _on_local_frame(self, frame):
        if not self.running or len(frame) < 14 or len(frame) > _MAX_FRAME:
            return
        if not self.peer_ip:
            return
        if self._echo.seen(frame) or is_tunnel_frame(frame, self.port):
            with self._lock:
                self.dropped += 1
            return
        self._send(FRAME, frame, (self.peer_ip, self.port))
        with self._lock:
            self.tx += 1

    def _inject(self, frame):
        if len(frame) < 14:
            return
        import discovery
        sc = discovery.load_scapy()
        if self._l2 is None:
            try:
                self._l2 = sc.conf.L2socket(iface=self._npf)
            except Exception as e:
                log(f"uttag inject-socket: {e}")
                return
        pkt = sc.Ether(frame)
        self._echo.remember(frame)
        wired = bytes(pkt)
        if wired != frame:
            self._echo.remember(wired)
        try:
            self._l2.send(pkt)
            with self._lock:
                self.rx += 1
        except Exception as e:
            log(f"uttag inject: {e}")

    def _send(self, typ, payload, dest):
        if not self._sock:
            return
        try:
            self._sock.sendto(pack_msg(typ, payload, seq=self._next_seq()), dest)
        except OSError as e:
            log(f"uttag send: {e}")

    def _next_seq(self):
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return self._seq

    def _allow_firewall(self):
        import ztctl
        # netsh delar själv på mellanslag, så namnet har inga.
        ztctl.run(["netsh", "advfirewall", "firewall", "add", "rule",
                    f"name=ZT-Tray-uttag-{self.port}", "dir=in", "action=allow",
                    "protocol=UDP", f"localport={self.port}"])

    def _restore_ip(self):
        if not self._cleared and self.role != "uttag":
            return
        import netif
        name = netif.restore_pending_ipv4()
        self._cleared = False
        if name:
            log(f"uttag: återställde IP på {name}")

    def _short_locked(self):
        if not self.running:
            return ""
        if self.peer_ip:
            side = "delar" if self.role == "uttag" else "tar emot"
            return f"{side} {self.peer_ip}"
        if self.role == "uttag":
            return "väntar på PIN"
        return "väntar"

    def _emit(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass


def selftest():
    raw = pack_msg(FRAME, b"\xff" * 60)
    typ, _f, _s, body = unpack_msg(raw)
    assert typ == FRAME and body == b"\xff" * 60
    assert unpack_msg(b"nej") is None
    assert unpack_msg(pack_msg(FRAME, b"x" * (_MAX_FRAME + 1))) is None

    assert not is_tunnel_frame(b"\xff" * 12 + b"\x08\x06" + b"\x00" * 28)
    udp = bytearray(14 + 20 + 8)
    udp[12:14] = b"\x08\x00"
    udp[14] = 0x45
    udp[14 + 9] = 17
    udp[14 + 20:14 + 22] = PORT.to_bytes(2, "big")
    assert is_tunnel_frame(bytes(udp))

    g = EchoGuard()
    g.remember(b"abc")
    assert g.seen(b"abc") and not g.seen(b"zzz")

    assert broadcast_of("10.244.17.1/24") == "10.244.17.255"
    assert valid_pin("1234") and valid_pin("12345678")
    assert not valid_pin("") and not valid_pin("123") and not valid_pin("abcd") and not valid_pin("123456789")

    nonce = "abc123"
    pin = "4321"
    hello = _hello_payload("Fabriken", "uttag", "Ethernet", nonce)
    assert pin.encode() not in hello
    parsed = _parse_hello(hello)
    assert parsed["sharing"] and parsed["nonce"] == nonce and parsed["name"] == "Fabriken"
    assert not _parse_hello(_hello_payload("Verktyg", "verktyg", "Ethernet", nonce))["sharing"]

    ad = pack_share({"name": "Fabriken", "iface": "Ethernet", "nonce": nonce, "sharing": True})
    assert pin.encode() not in ad
    assert unpack_share(ad)["nonce"] == nonce
    assert unpack_share(b"nej") is None

    fails = {}
    joined = handle_sharer("", nonce, pin, "Fabriken", "Ethernet", "10.9.9.2",
                           JOIN, {"name": "Kontor"}, fails, 10)
    assert joined["peer_ip"] == "" and not joined["accepted"]
    assert joined["replies"][0][0] == CHALLENGE
    pending = {"ip": "10.1.1.1", "pin": pin, "name": "Fabriken",
               "phase": "join", "nonce": "", "next": 1}
    chal = handle_tool(pending, "10.1.1.1", CHALLENGE, joined["replies"][0][1])
    assert not chal["accept"] and pending["phase"] == "connect" and pending["nonce"] == nonce

    proof = pin_proof(nonce, pin)
    good = handle_sharer("", nonce, pin, "Fabriken", "Ethernet", "10.9.9.2",
                         CONNECT, {"name": "Kontor", "proof": proof, "nonce": nonce}, fails, 11)
    assert good["accepted"] and good["peer_ip"] == "10.9.9.2" and good["peer_name"] == "Kontor"
    assert good["replies"][0][0] == ACCEPT
    done = handle_tool(pending, "10.1.1.1", ACCEPT, good["replies"][0][1])
    assert done["accept"] and done["peer_name"] == "Fabriken" and done["clear"]

    bad = handle_sharer("", nonce, pin, "Fabriken", "Ethernet", "10.9.9.3",
                        CONNECT, {"name": "C", "proof": pin_proof(nonce, "9999"), "nonce": nonce}, {}, 12)
    assert not bad["accepted"] and bad["peer_ip"] == ""
    assert bad["replies"][0][1]["reason"] == "fel_pin"

    busy = handle_sharer("10.9.9.2", nonce, pin, "Fabriken", "Ethernet", "10.9.9.3",
                         CONNECT, {"name": "C", "proof": proof, "nonce": nonce}, {}, 13)
    assert not busy["accepted"] and busy["peer_ip"] == "10.9.9.2"
    assert busy["replies"][0][1]["reason"] == "upptagen"

    stale = handle_sharer("", nonce, pin, "Fabriken", "Ethernet", "10.9.9.4",
                          CONNECT, {"name": "D", "proof": proof, "nonce": "annan"}, {}, 14)
    assert not stale["accepted"] and stale["replies"][0][1]["reason"] == "nonce"

    locked = {}
    for n in range(_PIN_FAILS):
        step = handle_sharer("", nonce, pin, "Fabriken", "Ethernet", "10.8.8.8",
                             CONNECT, {"name": "E", "proof": "nope", "nonce": nonce}, locked, 100)
        assert step["replies"][0][1]["reason"] == "fel_pin" and step["peer_ip"] == ""
    blocked = handle_sharer("", nonce, pin, "Fabriken", "Ethernet", "10.8.8.8",
                            CONNECT, {"name": "E", "proof": proof, "nonce": nonce}, locked, 120)
    assert blocked["replies"][0][1]["reason"] == "manga" and blocked["peer_ip"] == ""
    opened = handle_sharer("", nonce, pin, "Fabriken", "Ethernet", "10.8.8.8",
                           CONNECT, {"name": "E", "proof": proof, "nonce": nonce},
                           locked, 100 + _PIN_LOCKOUT)
    assert opened["accepted"] and opened["peer_ip"] == "10.8.8.8"

    pending2 = {"ip": "10.1.1.1", "pin": "0000", "name": "X", "phase": "connect", "nonce": nonce, "next": 0}
    denied = handle_tool(pending2, "10.1.1.1", DENY, {"reason": "fel_pin"})
    assert denied["clear"] and denied["status"] == "Fel PIN."
    pending3 = {"ip": "10.1.1.1", "pin": pin, "name": "X", "phase": "connect", "nonce": "gammal", "next": 0}
    retry = handle_tool(pending3, "10.1.1.1", DENY, {"reason": "nonce"})
    assert not retry["clear"] and pending3["phase"] == "join"
    assert handle_tool(pending3, "10.9.9.9", ACCEPT, {"name": "Annan"})["accept"] is False

    import ztctl
    row = ztctl.parse_listnetworks_line(
        "200 listnetworks abcdef0123456789 Exempelnät 02:1a:2b:3c:4d:5e "
        "OK PRIVATE ethernet_1 10.244.17.1/24,10.2.2.2/16")
    assert row["name"] == "Exempelnät" and row["status"] == "OK"
    assert row["ips"] == ["10.244.17.1/24", "10.2.2.2/16"]
    log("uttag selftest OK")


if __name__ == "__main__":
    selftest()
