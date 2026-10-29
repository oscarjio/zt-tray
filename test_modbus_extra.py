import unittest
import modbus_monitor as M


def run(faults, dur=12.0, timeout=0.5):
    ch = M.simulate(dur, faults=faults)
    mon = M.Monitor(19200, "E", 1, resp_timeout=timeout)
    M.replay(ch, mon)
    return mon


class TestExtra(unittest.TestCase):
    def test_clean_has_no_late_or_collision(self):
        mon = run({})
        kinds = {e.kind for e in mon.events}
        self.assertNotIn("late", kinds)
        self.assertNotIn("collision", kinds)
        self.assertNotIn("timeout", kinds)

    def test_late_answer(self):
        mon = run({"late": {3: 0.2}})
        late = [e for e in mon.events if e.kind == "late"]
        self.assertTrue(late)
        self.assertTrue(all(e.asked == 3 and e.responder == 3 and e.rtt > 0.5 for e in late))
        self.assertEqual(M.ev_status(late[0]), "late")
        r = {x["slav"]: x for x in mon.stats_rows()}[3]
        self.assertGreater(r["sena"], 0)
        self.assertIn("sent svar", M.cause_text(r))

    def test_collision(self):
        mon = run({"collide": {2: 0.3}}, dur=20)
        self.assertTrue(any(e.kind == "collision" for e in mon.events))
        r = {x["slav"]: x for x in mon.stats_rows()}
        self.assertGreater(sum(x["krockar"] for x in r.values()), 0)

    def test_asked_and_responder(self):
        mon = run({"timeout": {4: 0.3}})
        to = [e for e in mon.events if e.kind in ("timeout", "early")]
        self.assertTrue(to and all(e.asked == 4 and e.responder is None for e in to))
        to = [e for e in mon.events if e.kind in ("timeout", "early")]
        resp = [e for e in mon.events if e.kind == "resp"][0]
        self.assertEqual(resp.asked, resp.responder)
        self.assertIsNotNone(resp.req)

    def test_filter(self):
        mon = run({"timeout": {4: 0.3}, "exception": {2: 0.3}})
        ev = list(mon.events)
        self.assertTrue(all(e.asked == 4 or e.slave == 4 for e in filter(M.parse_filter("addr==4"), ev)))
        self.assertTrue(all(e.kind in ("timeout", "early") for e in filter(M.parse_filter("timeout"), ev)))
        self.assertTrue(all(M.ev_status(e) != "ok" for e in filter(M.parse_filter("status!=ok"), ev)))
        self.assertTrue(all(e.fc == 3 for e in filter(M.parse_filter("fc==3 && addr==1"), ev)))
        regs = list(filter(M.parse_filter("reg==101"), ev))
        self.assertTrue(regs and all(e.fc in (3, 4) for e in regs))
        both = list(filter(M.parse_filter("addr==1 || addr==2"), ev))
        self.assertEqual({e.asked for e in both}, {1, 2})
        self.assertTrue(list(filter(M.parse_filter("exc"), ev)))
        with self.assertRaises(ValueError):
            M.parse_filter("banan==3")

    def test_find_stops(self):
        mon = run({"stop": {5: 6.0}, "timeout": {5: 0.2}}, dur=12)
        st = M.find_stops(mon.events, quiet=2.0)
        self.assertTrue(st)
        self.assertEqual(st[0]["first_fail"], 5)
        self.assertEqual(st[0]["addr"], 5)

    def test_device_states(self):
        mon = run({"stop": {5: 3.0}}, dur=10)
        rows = mon.stats_rows()
        last = mon.events[-1].t
        d = {a: c for a, n, c, t in M.device_states(rows, {1: "LD2", 5: "SW1A4080 ny", 9: "motor 3"}, last)}
        self.assertEqual(d[1], "green")
        self.assertEqual(d[5], "red")
        self.assertEqual(d[9], "grey")
        self.assertIn(2, d)  # okänd adress visas också

    def test_link_state(self):
        self.assertEqual(M.link_state(0, 0, 0)[0], "grey")
        self.assertEqual(M.link_state(500, 0, 40)[0], "red")
        self.assertEqual(M.link_state(500, 100, 1)[0], "green")

    def test_port_pick(self):
        infos = [{"device": "COM1", "rs485": False, "usb": False},
                 {"device": "COM4", "rs485": False, "usb": True},
                 {"device": "COM7", "rs485": True, "usb": True}]
        self.assertEqual(M.pick_port(infos)["device"], "COM7")
        self.assertEqual(M.pick_port(infos, "com4")["device"], "COM4")
        self.assertEqual(M.pick_port(infos[:2])["device"], "COM4")
        self.assertEqual(M.port_name("COM7 \u2013 USB-RS485 [FTDI]"), "COM7")
        self.assertIsNone(M.pick_port([]))


if __name__ == "__main__":
    unittest.main()
