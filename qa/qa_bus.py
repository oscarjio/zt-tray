"""
QA: virtuell RS485-buss + virtuell USB-adapter (ersätter COM-port när com0com/admin saknas).

Bussen kör en simulerad master + slavar (modbus_monitor.simulate) med RIKTIG UART-tidtagning:
varje byte kodas till en bitvåg (start/data/paritet/stopp) med bussens inställning och avkodas sedan
som en UART inställd på adapterns inställning (linecheck.encode_wave/decode_wave). Fel baud/paritet
ger alltså samma skräp och ram-/paritetsfel som en riktig adapter. Bytes levereras i realtid i
USB-block (1 ms) och kan "dras ur" (OSError) eller vara "upptagna" (PermissionError).
"""
import bisect
import threading
import time

import linecheck as L
import modbus_monitor as M

QA_PORT = "COM7"
QA_INFO = {"device": QA_PORT, "description": "USB-SERIAL CH340 (QA virtuell)", "manufacturer": "wch.cn",
           "product": "", "vid": 0x1A86, "pid": 0x7523, "serial": "", "usb": True, "rs485": False}


class VirtualBus:
    def __init__(self, tx=(19200, "E", 1), faults=None, slaves=(1, 2, 3, 4, 5, 6), seg_s=10.0, seeds=3,
                 poll_gap=0.005, slave_cfg=None, invert=False):
        self.tx = tx
        self.slave_cfg = slave_cfg
        self.invert = invert
        self.t0 = time.perf_counter()
        self.unplugged = False
        self.busy = False
        self.segs = []
        for k in range(seeds):
            bt = M.simulate(duration=seg_s * 0.85, baud=tx[0], parity=tx[1], stop=tx[2], slaves=slaves,
                            faults=faults or {}, seed=11 + k, poll_gap=poll_gap, return_bytes=True)
            length = (bt[-1][0] if bt else seg_s) + 0.03
            self.segs.append((bt, length))
        self._cache = {}
        self._lk = threading.Lock()
        self.opened = 0
        self.written = []

    def seg(self, k):
        return self.segs[k % len(self.segs)]

    def capture(self, k, rx):
        key = (k % len(self.segs), tuple(rx))
        with self._lk:
            c = self._cache.get(key)
        if c is None:
            bt, _ln = self.seg(k)
            ch, er = L.capture_line(bt, self.tx, rx, self.slave_cfg, self.invert)
            c = ([x[0] for x in ch], ch, sorted(er))
            with self._lk:
                self._cache[key] = c
        return c

    def now(self):
        return time.perf_counter() - self.t0

    def locate(self, t):
        """Segmentnummer + segmentets starttid för busstid t."""
        k, base = 0, 0.0
        while True:
            ln = self.seg(k)[1]
            if base + ln > t:
                return k, base
            base += ln
            k += 1


class VirtualPort:
    def __init__(self, bus, rx):
        if bus.unplugged:
            raise OSError(f"could not open port '{QA_PORT}': FileNotFoundError(2, 'Det går inte att hitta filen.')")
        if bus.busy:
            raise PermissionError(13, f"could not open port '{QA_PORT}': PermissionError(13, 'Åtkomst nekad.')")
        self.bus, self.rx = bus, tuple(rx)
        bus.opened += 1
        self.k, self.base = bus.locate(bus.now())
        ts, _ch, er = bus.capture(self.k, self.rx)
        t = bus.now() - self.base
        self.ci = bisect.bisect_right(ts, t)
        self.ei = bisect.bisect_right([e[0] for e in er], t)
        self.err = {"frame": 0, "parity": 0, "overrun": 0, "break": 0}
        self.closed = False

    def read(self):
        if self.closed:
            raise OSError("port closed")
        if self.bus.unplugged:
            raise OSError("ClearCommError failed (PermissionError(13, 'Enheten känns inte igen.', None, 22))")
        time.sleep(0.002)
        now = self.bus.now()
        out = bytearray()
        while True:
            ts, ch, er = self.bus.capture(self.k, self.rx)
            rel = now - self.base
            while self.ci < len(ch) and ts[self.ci] <= rel:
                out += ch[self.ci][1]
                self.ci += 1
            while self.ei < len(er) and er[self.ei][0] <= rel:
                self.err[er[self.ei][1]] = self.err.get(er[self.ei][1], 0) + 1
                self.ei += 1
            ln = self.bus.seg(self.k)[1]
            if rel >= ln:
                self.base += ln
                self.k += 1
                self.ci = self.ei = 0
                continue
            break
        return bytes(out)

    def errors(self):
        return dict(self.err)

    def write(self, b):
        self.bus.written.append(bytes(b))

    def close(self):
        self.closed = True


def install(bus_holder):
    """Patcha modbus_monitor så att COM7 är den virtuella adaptern. bus_holder: dict {"bus": VirtualBus}."""
    orig_open = M.open_port

    def open_port(name, baud=19200, parity="E", stop=1, *a, **kw):
        if str(name).upper() == QA_PORT:
            return VirtualPort(bus_holder["bus"], (int(baud), parity, int(stop)))
        return orig_open(name, baud, parity, stop, *a, **kw)

    def port_infos():
        b = bus_holder.get("bus")
        return [] if (b is None or b.unplugged or bus_holder.get("hidden")) else [dict(QA_INFO)]

    M.open_port = open_port
    M.port_infos = port_infos
    M.list_ports = lambda: [i["device"] for i in port_infos()]
