"""
Modbus RTU-master (Modbus Poll-liknande): läs/skriv FC 1-6, 15, 16, adress-scanner och pollning.

SÄNDER PÅ BUSSEN. Avstängt som standard: Master kräver armed=True, och UI:t kräver att
användaren aktiverar sändning för sessionen efter en varning. Sändning på en buss i drift
kan krocka med PLC:n (två masters) och störa maskinen.
"""

import struct
import threading
import time

import modbus_monitor as M

WARNING = ("Sändning på en buss i drift kan störa: två masters på samma RS485-buss krockar, och "
           "skrivningar ändrar värden i motordrivarna. Använd helst när PLC:n är stoppad/frånkopplad.")


class NotArmed(RuntimeError):
    pass


def build_request(slave, fc, addr, qty=1, values=None):
    """Bygger en RTU-fråga med CRC. values: lista för FC15/16, värde för FC5/6."""
    slave, fc, addr = int(slave), int(fc), int(addr)
    if not (0 <= slave <= 247):
        raise ValueError("slavadress 0-247")
    if not (0 <= addr <= 0xFFFF):
        raise ValueError("registeradress 0-65535")
    if fc in (1, 2, 3, 4):
        qty = int(qty)
        lim = 2000 if fc in (1, 2) else 125
        if not 1 <= qty <= lim:
            raise ValueError(f"antal 1-{lim}")
        body = struct.pack(">BBHH", slave, fc, addr, qty)
    elif fc == 5:
        v = values if not isinstance(values, (list, tuple)) else values[0]
        body = struct.pack(">BBHH", slave, fc, addr, 0xFF00 if int(v) else 0)
    elif fc == 6:
        v = values if not isinstance(values, (list, tuple)) else values[0]
        body = struct.pack(">BBHH", slave, fc, addr, int(v) & 0xFFFF)
    elif fc == 15:
        bits = [1 if int(x) else 0 for x in values]
        nb = (len(bits) + 7) // 8
        data = bytearray(nb)
        for i, b in enumerate(bits):
            if b:
                data[i // 8] |= 1 << (i % 8)
        body = struct.pack(">BBHHB", slave, fc, addr, len(bits), nb) + bytes(data)
    elif fc == 16:
        regs = [int(x) & 0xFFFF for x in values]
        if not 1 <= len(regs) <= 123:
            raise ValueError("1-123 register")
        body = struct.pack(">BBHHB", slave, fc, addr, len(regs), 2 * len(regs)) + b"".join(
            struct.pack(">H", r) for r in regs)
    else:
        raise ValueError("funktionskod stöds ej (1-6, 15, 16)")
    return M.with_crc(body)


def expected_len(req):
    fc = req[1]
    if fc in (1, 2):
        n = (req[4] << 8) | req[5]
        return 5 + (n + 7) // 8
    if fc in (3, 4):
        n = (req[4] << 8) | req[5]
        return 5 + 2 * n
    return 8


def parse_response(req, resp):
    """-> {"ok", "values", "exception", "text"}"""
    if not resp:
        return {"ok": False, "values": None, "exception": None, "text": "inget svar (timeout)"}
    if not M.crc_ok(resp):
        return {"ok": False, "values": None, "exception": None, "text": f"CRC-fel i svar: {resp.hex(' ')}"}
    if resp[0] != req[0]:
        return {"ok": False, "values": None, "exception": None, "text": f"svar från fel adress {resp[0]}"}
    if resp[1] & 0x80:
        code = resp[2] if len(resp) > 2 else None
        return {"ok": False, "values": None, "exception": code,
                "text": f"exception {code}: {M.EXC_NAMES.get(code, '?')}"}
    fc = req[1]
    if fc in (3, 4):
        vals = [(resp[3 + 2 * i] << 8) | resp[4 + 2 * i] for i in range(resp[2] // 2)]
    elif fc in (1, 2):
        n = (req[4] << 8) | req[5]
        vals = [(resp[3 + i // 8] >> (i % 8)) & 1 for i in range(n)]
    else:
        vals = None
    return {"ok": True, "values": vals, "exception": None, "text": "OK" if vals is None else f"{vals}"}


def format_values(vals, start=0, fmt="u16"):
    """Visa register som u16/s16/hex/u32/s32/float32 (big-endian ordpar)."""
    if vals is None:
        return []
    out = []
    if fmt in ("u32", "s32", "f32"):
        for i in range(0, len(vals) - 1, 2):
            raw = struct.pack(">HH", vals[i], vals[i + 1])
            v = struct.unpack({"u32": ">I", "s32": ">i", "f32": ">f"}[fmt], raw)[0]
            out.append((start + i, round(v, 6) if fmt == "f32" else v))
        return out
    for i, v in enumerate(vals):
        if fmt == "s16":
            v = v - 0x10000 if v & 0x8000 else v
        elif fmt == "hex":
            v = f"0x{v:04X}"
        out.append((start + i, v))
    return out


class SerialLink:
    """pyserial-port för master (sänder!)."""

    def __init__(self, port, baud, parity, stop):
        import serial
        self.s = serial.serial_for_url(port, baudrate=baud, parity=parity, stopbits=stop, bytesize=8,
                                       timeout=0.005, write_timeout=1)

    def write(self, b):
        self.s.reset_input_buffer()
        self.s.write(b)
        self.s.flush()

    def read(self):
        return self.s.read(512)

    def close(self):
        self.s.close()


class SharedLink:
    """Sänd via en pågående lyssning (Capture) - då ser monitorn både våra frågor och svaren.
    Ekon av egen fråga (vissa adaptrar) filtreras bort ur master-läsningen."""

    def __init__(self, capture, mon=None):
        self.cap = capture.caps[0] if hasattr(capture, "caps") else capture
        self.mon = mon
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.last_tx = b""
        self.cap.taps.append(self._tap)

    def _tap(self, d):
        with self.lock:
            self.buf += d

    def write(self, b):
        with self.lock:
            self.buf.clear()
        self.last_tx = bytes(b)
        if self.mon is not None:
            self.mon.feed(time.perf_counter(), bytes(b))
        self.cap.write(b)

    def read(self):
        with self.lock:
            d, self.buf = bytes(self.buf), bytearray()
        if self.last_tx and d.startswith(self.last_tx):
            d = d[len(self.last_tx):]
            self.last_tx = b""
        return d

    def close(self):
        try:
            self.cap.taps.remove(self._tap)
        except ValueError:
            pass


class Master:
    def __init__(self, link, baud=19200, parity="E", stop=1, timeout=0.5, armed=False, on_traffic=None):
        if not armed:
            raise NotArmed("Sändning är inte aktiverad. " + WARNING)
        self.link = link
        self.timeout = timeout
        self.on_traffic = on_traffic      # (t, bytes, riktning 'tx'/'rx')
        self.ct = M.char_time(baud, parity, stop)
        self.gap = M.t35(baud, parity, stop)
        self.lock = threading.Lock()
        self.count = {"sent": 0, "ok": 0, "timeout": 0, "exc": 0, "crc": 0}

    def transact(self, req, timeout=None):
        to = self.timeout if timeout is None else timeout
        with self.lock:
            t0 = time.perf_counter()
            self.link.write(req)
            self.count["sent"] += 1
            if self.on_traffic:
                self.on_traffic(t0, bytes(req), "tx")
            if req[0] == 0:          # broadcast: inget svar
                time.sleep(max(self.gap, 0.01))
                return None, None
            want = expected_len(req)
            buf = bytearray()
            t_send_end = max(t0 + len(req) * self.ct, time.perf_counter())
            deadline = t_send_end + to
            while time.perf_counter() < deadline:
                d = self.link.read()
                if d:
                    buf += d
                    if len(buf) >= 2 and buf[1] & 0x80 and len(buf) >= 5:
                        break
                    if len(buf) >= want:
                        break
                else:
                    time.sleep(0.001)
            rtt = max(0.0, time.perf_counter() - t_send_end)
            resp = bytes(buf) or None
            if resp and self.on_traffic:
                self.on_traffic(time.perf_counter(), resp, "rx")
            r = parse_response(req, resp)
            key = "ok" if r["ok"] else ("timeout" if resp is None else ("exc" if r["exception"] else "crc"))
            self.count[key] += 1
            time.sleep(self.gap)
            return resp, rtt

    def request(self, slave, fc, addr, qty=1, values=None, timeout=None):
        req = build_request(slave, fc, addr, qty, values)
        resp, rtt = self.transact(req, timeout)
        r = parse_response(req, resp)
        r.update({"req": req.hex(" "), "resp": resp.hex(" ") if resp else "",
                  "rtt_ms": None if rtt is None or resp is None else round(rtt * 1000, 1)})
        return r

    def scan(self, addrs=range(1, 248), fc=3, reg=0, timeout=0.1, progress=None, stop_flag=None):
        """Frågar varje adress. Svar ELLER exception = enhet finns."""
        found = []
        for a in addrs:
            if stop_flag and stop_flag():
                break
            if progress:
                progress(a)
            r = self.request(a, fc, reg, 1, timeout=timeout)
            if r["ok"] or r["exception"] is not None:
                found.append({"slav": a, "svar": r["text"], "rtt_ms": r["rtt_ms"]})
        return found

    def close(self):
        try:
            self.link.close()
        except Exception:
            pass


class Poller:
    """Pollar en lista frågor periodiskt i egen tråd."""

    def __init__(self, master, items, interval=1.0, on_result=None):
        self.master, self.items, self.interval, self.on_result = master, items, interval, on_result
        self.stop_ev = threading.Event()
        self.th = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.th.start()

    def _run(self):
        while not self.stop_ev.is_set():
            for it in self.items:
                if self.stop_ev.is_set():
                    return
                try:
                    r = self.master.request(**it)
                except Exception as e:
                    r = {"ok": False, "text": str(e)}
                if self.on_result:
                    self.on_result(it, r)
            self.stop_ev.wait(self.interval)

    def stop(self):
        self.stop_ev.set()


class SimSlaveLink:
    """Simulerad buss med slavar (för test). Svarar som riktiga slavar på FC 1-6, 15, 16."""

    def __init__(self, slaves=(1, 2, 3), dead=(), exc=None, delay=0.002):
        self.regs = {s: {i: (s * 100 + i) & 0xFFFF for i in range(0, 300)} for s in slaves}
        self.coils = {s: {i: 0 for i in range(0, 64)} for s in slaves}
        self.dead = set(dead)
        self.exc = exc or {}
        self.out = bytearray()
        self.written = []

    def write(self, b):
        self.written.append(bytes(b))
        a, fc = b[0], b[1]
        if a not in self.regs or a in self.dead or not M.crc_ok(b):
            return
        if fc in self.exc.get(a, ()):
            self.out += M.with_crc(bytes([a, fc | 0x80, 1]))
            return
        addr = (b[2] << 8) | b[3]
        q = (b[4] << 8) | b[5]
        R, Cc = self.regs[a], self.coils[a]
        if fc in (3, 4):
            if addr + q > 300:
                self.out += M.with_crc(bytes([a, fc | 0x80, 2]))
                return
            data = b"".join(struct.pack(">H", R[addr + i]) for i in range(q))
            self.out += M.with_crc(bytes([a, fc, len(data)]) + data)
        elif fc in (1, 2):
            nb = (q + 7) // 8
            d = bytearray(nb)
            for i in range(q):
                if Cc.get(addr + i):
                    d[i // 8] |= 1 << (i % 8)
            self.out += M.with_crc(bytes([a, fc, nb]) + bytes(d))
        elif fc == 5:
            Cc[addr] = 1 if q == 0xFF00 else 0
            self.out += bytes(b)
        elif fc == 6:
            R[addr] = q
            self.out += bytes(b)
        elif fc == 15:
            for i in range(q):
                Cc[addr + i] = (b[7 + i // 8] >> (i % 8)) & 1
            self.out += M.with_crc(bytes(b[:6]))
        elif fc == 16:
            for i in range(q):
                R[addr + i] = (b[7 + 2 * i] << 8) | b[8 + 2 * i]
            self.out += M.with_crc(bytes(b[:6]))
        else:
            self.out += M.with_crc(bytes([a, fc | 0x80, 1]))

    def read(self):
        d, self.out = bytes(self.out), bytearray()
        return d

    def close(self):
        pass
