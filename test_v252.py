"""Regressionstester för v2.5.2 (QA-pass inför riktig RS485-felsökning)."""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "qa"))


def test_eventstore_bounded_and_global_index():
    import packet_view as PV
    st = PV.EventStore(keep=100)
    for i in range(10):
        st.extend(list(range(i * 50, i * 50 + 50)))
    assert len(st) == 500
    assert len(st.items) <= 100
    assert not st.has(0) and st.has(499)
    assert st[499] == 499 and st[-1] == 499
    assert st[st.base] == st.base
    try:
        st[0]
        raise AssertionError("borde kasta IndexError")
    except IndexError:
        pass
    idx = list(st.indexed())
    assert idx[0] == (st.base, st.base) and idx[-1] == (499, 499)
    assert st[-3:] == [497, 498, 499]


def test_monitor_resync_clears_half_frame():
    import modbus_monitor as M
    mon = M.Monitor(19200, "E", 1) if _takes_args(M.Monitor) else M.Monitor()
    mon.seg = bytearray(b"\x01\x03")
    mon.pending = object()
    mon.resync()
    assert len(mon.seg) == 0 and mon.pending is None and mon.seg_bounds == []


def _takes_args(cls):
    import inspect
    ps = [p for p in inspect.signature(cls.__init__).parameters.values()
          if p.name != "self" and p.default is p.empty]
    return bool(ps)


def test_config_corrupt_backed_up_and_null_fields():
    import config as C
    old = C.CONFIG_PATH
    with tempfile.TemporaryDirectory() as d:
        try:
            C.CONFIG_PATH = Path(d) / "config.json"
            C.CONFIG_PATH.write_text("{trasig", encoding="utf-8")
            cfg = C.load_config()
            assert isinstance(cfg, dict)
            assert any(n.startswith("config.json.trasig-") for n in os.listdir(d))
            dk = [k for k, v in C.DEFAULT_CONFIG.items() if isinstance(v, dict)]
            data = {k: None for k in dk}
            C.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")
            cfg = C.load_config()
            for k in dk:
                assert isinstance(cfg[k], dict), k
            C.CONFIG_PATH.write_text("[1,2]", encoding="utf-8")
            assert isinstance(C.load_config(), dict)
        finally:
            C.CONFIG_PATH = old


def test_stepwise_no_stop_scan_8N_gives_2():
    import agent_mode as AM
    import linecheck as L
    import modbus_monitor as M
    bt = M.simulate(4, 19200, "N", 2, slaves=(1, 2), return_bytes=True)
    out, calls = [], []

    def scan(bauds, pars, stops, sec):
        calls.append(tuple(stops) if stops else None)
        return L.sim_scan(bt, (19200, "N", 2), bauds=bauds, parities=pars, stops=stops)
    res = AM.stepwise_detect(scan, lambda s, end="\n": out.append(s + end))
    assert res == (19200, "N", 2), res
    assert "Stoppbitar: 2" in "".join(out)


def test_virtual_bus_realtime_detect_57600_8E1():
    import agent_mode as AM
    import linecheck as L
    import modbus_monitor as M
    import qa_bus as Q
    saved = (M.open_port, M.port_infos, M.list_ports)
    try:
        holder = {"bus": Q.VirtualBus((57600, "E", 1), seg_s=4, seeds=1)}
        Q.install(holder)
        assert M.list_ports() == [Q.QA_PORT]

        def scan(bauds, pars, stops, sec):
            return L.live_scan(Q.QA_PORT, 0.6, bauds=bauds, parities=pars, stops=stops)
        res = AM.stepwise_detect(scan, lambda s, end="\n": None)
        assert res == (57600, "E", 1), res
        assert holder["bus"].written == []          # passiv: aldrig sänt
        holder["bus"].busy = True
        try:
            M.open_port(Q.QA_PORT, 57600, "E", 1)
            raise AssertionError("upptagen port borde kasta")
        except PermissionError:
            pass
    finally:
        M.open_port, M.port_infos, M.list_ports = saved


def test_clash_no_false_positive_on_neighbour_during_live_watch():
    """E2E visade 'Misstänkt adresskrock på adress 2 (70 %)' strax efter start när tvillingen satt på adress 1."""
    import modbus_monitor as M
    import addrclash as X
    twin = {1: {"both": 0.25, "after": 0.25, "only": 0.3, "delay": 0.012}}
    for baud, p, s in [(57600, "E", 1), (19200, "N", 2), (9600, "O", 1)]:
        for seed in (1, 2, 3):
            f = dict(M.DEMO_FAULTS)
            f["stop"] = {5: 6.0}
            f["twin"] = twin
            c = M.simulate(40, baud, p, s, faults=f, seed=seed)
            mon = M.Monitor(baud, p, s)
            flagged, nxt = set(), c[0][0] + 3
            for t, d in c:
                mon.feed(t, d)
                mon.tick(t)
                if t >= nxt:
                    nxt += 3
                    flagged |= set(X.suspects(mon.events))
            assert flagged == {1}, (baud, seed, flagged)


if __name__ == "__main__":
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f()
                print("ok  ", n)
            except Exception as e:
                fails += 1
                print("FAIL", n, repr(e)[:300])
    sys.exit(1 if fails else 0)
