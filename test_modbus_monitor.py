"""Enhetstester för modbus_monitor (ingen hårdvara). Kör: python -m unittest test_modbus_monitor"""
import os
import tempfile
import unittest

import modbus_monitor as M


def run(chunks, **kw):
    m = M.Monitor(kw.pop("baud", 19200), kw.pop("parity", "E"), kw.pop("stop", 1), **kw)
    M.replay(chunks, m)
    return m


def kinds(m, k):
    return [e for e in m.events if e.kind == k]


class Crc(unittest.TestCase):
    def test_known(self):
        self.assertEqual(M.with_crc(bytes.fromhex("010300000001"))[-2:], bytes.fromhex("840a"))
        self.assertTrue(M.crc_ok(bytes.fromhex("01030000000184 0a".replace(" ", ""))))


class Clean(unittest.TestCase):
    def test_all_ok_various_usb_latency(self):
        for lat in (0.001, 0.004, 0.016):
            ch = M.simulate(5, usb_latency=lat)
            m = run(ch)
            bad = [e for e in m.events if e.kind in M.ERROR_KINDS]
            self.assertEqual(bad, [], f"latency {lat}: {[e.line() for e in bad[:3]]}")
            for r in m.stats_rows():
                self.assertEqual(r["frågor"], r["svar"])
                self.assertGreater(r["frågor"], 10)

    def test_9600_no_parity(self):
        m = run(M.simulate(5, baud=9600, parity="N", stop=2), baud=9600, parity="N", stop=2)
        self.assertEqual([e for e in m.events if e.kind in M.ERROR_KINDS], [])

    def test_rtt(self):
        m = run(M.simulate(5, resp_delay=0.010, usb_latency=0.001))
        r = m.stats_rows()[0]
        self.assertTrue(8 < r["medel"] < 13, r)


class Faults(unittest.TestCase):
    def test_timeout(self):
        m = run(M.simulate(10, faults={"timeout": {5: 0.2}}), resp_timeout=0.05)
        st = {r["slav"]: r for r in m.stats_rows()}
        self.assertGreater(st[5]["timeout"], 0)
        self.assertEqual(st[4]["timeout"], 0)

    def test_crc(self):
        m = run(M.simulate(10, faults={"crc": {5: 0.2}}))
        self.assertTrue(kinds(m, "crc"))
        self.assertTrue(all(e.slave == 5 for e in kinds(m, "crc")))

    def test_exception(self):
        m = run(M.simulate(10, faults={"exception": {3: 0.2}}))
        ex = kinds(m, "exc")
        self.assertTrue(ex and all(e.slave == 3 for e in ex))
        self.assertIn("Otillåten dataadress", ex[0].text)

    def test_dup(self):
        m = run(M.simulate(10, faults={"dup": {2: 0.2}}))
        self.assertTrue(kinds(m, "dup"))

    def test_wrong_addr(self):
        req = M.with_crc(bytes([4, 3, 0, 0, 0, 1]))
        resp = M.with_crc(bytes([7, 3, 2, 0, 5]))
        m = run([(0.01, req), (0.02, resp)])
        self.assertTrue(kinds(m, "wrongaddr"))

    def test_noise(self):
        m = run(M.simulate(10, faults={"noise": 0.3}))
        self.assertTrue(kinds(m, "junk") or kinds(m, "crc"))

    def test_early(self):
        m = run(M.simulate(10, faults={"early": {6: 0.2}}))
        self.assertTrue(kinds(m, "early"))


class LogReplayTrap(unittest.TestCase):
    def test_roundtrip_and_trap(self):
        d = tempfile.mkdtemp()
        ch = M.simulate(10, faults={"crc": {5: 0.1}})
        p = os.path.join(d, "x.zmlog")
        M.write_log(p, ch, M.make_header("SIM", 19200, "E", 1))
        st, ch2 = M.read_log(p)
        self.assertEqual(st["baud"], "19200")
        m = M.Monitor(19200, "E", 1)
        m.trap = M.ErrorTrap([5], 2, 1, d, "# h")
        M.replay(ch2, m)
        m.trap.tick(m, 1e12)
        self.assertTrue(m.trap.saved)
        _, tch = M.read_log(m.trap.saved[0])
        self.assertTrue(tch)
        M.export_csv(list(m.events), os.path.join(d, "x.csv"))
        self.assertGreater(os.path.getsize(os.path.join(d, "x.csv")), 100)

    def test_rank_prefers_strict(self):
        res = [{"baud": 19200, "parity": "N", "stop": 1, "frames": 50, "bad": 0, "uart_err": 0, "score": 0},
               {"baud": 19200, "parity": "E", "stop": 1, "frames": 50, "bad": 0, "uart_err": 0, "score": 0},
               {"baud": 9600, "parity": "E", "stop": 1, "frames": 0, "bad": 9, "uart_err": 20, "score": 0}]
        self.assertEqual(M.rank(res)[0]["parity"], "E")

    def test_score_wrong_baud(self):
        ch = M.simulate(3)
        ok, _ = M.score_bytes(ch, 19200, "E", 1)
        self.assertGreater(ok, 50)
        import random
        r = random.Random(3)
        noise = [(k * 0.001, bytes(r.randrange(256) for _ in range(4))) for k in range(2000)]
        ok3, bad3 = M.score_bytes(noise, 19200, "E", 1)
        self.assertLess(ok3, 5)


class RegisterTests(unittest.TestCase):
    def test_regmap_compare(self):
        chunks = M.simulate(5, faults={"illegal": {5: (300,)}, "regdiff": {5: 3}})
        mon = M.Monitor(19200, "E", 1)
        M.replay(chunks, mon)
        st = {(r["tabell"], r["adress"]): r["status"] for r in mon.regmap.compare(4, 5)}
        self.assertEqual(st[("hr", 100)], "lika")
        self.assertEqual(st[("hr", 103)], "OLIKA")
        self.assertEqual(st[("hr", 300)], "FEL B")
        rows = {(r["slav"], r["adress"]): r for r in mon.regmap.rows()}
        self.assertGreater(rows[(4, 100)]["läsningar"], 3)
        self.assertGreater(rows[(4, 100)]["ändringar"], 1)
        self.assertGreater(rows[(5, 300)]["fel"], 0)

    def test_text_pairs(self):
        chunks = M.simulate(3, faults={"timeout": {2: 0.5}})
        mon = M.Monitor(19200, "E", 1)
        M.replay(chunks, mon)
        ev = list(mon.events)
        self.assertTrue(any("→ Svar (" in e.text for e in ev if e.kind == "resp"))
        self.assertTrue(all("→ INGET SVAR (timeout" in e.text for e in ev if e.kind == "timeout"))

    def test_regs_csv(self):
        import tempfile, os
        chunks = M.simulate(2)
        mon = M.Monitor(19200, "E", 1)
        M.replay(chunks, mon)
        d = tempfile.mkdtemp()
        mon.regmap.export_csv(os.path.join(d, "r.csv"))
        mon.regmap.export_compare_csv(os.path.join(d, "c.csv"), 4, 5)
        txt = open(os.path.join(d, "r.csv"), encoding="utf-8-sig").read()
        self.assertIn("värde_hex", txt)


if __name__ == "__main__":
    unittest.main()
