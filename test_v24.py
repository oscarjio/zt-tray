"""Tester för v2.4: linjediagnos (baud/paritet/stopp/A-B), master, AI-agent (mock + ev. riktig LLM)."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_agent as A
import linecheck as L
import modbus_master as MM
import modbus_monitor as M

TX = (19200, "E", 1)


def _bt(**kw):
    return M.simulate(4, *TX, slaves=(1, 2, 3), return_bytes=True, **kw)


def _diag(current=(19200, "E", 1), **kw):
    bt = _bt()
    res = L.sim_scan(bt, TX, **kw)
    b = res[0]
    rx = (b["baud"], b["parity"], b["stop"])
    ch, er = L.capture_line(bt, TX, rx, **kw)
    mon = L.feed(M.Monitor(*rx), ch, er)
    return L.diagnose(res, current=current, stats_rows=mon.stats_rows())


# ---------------- linjediagnos
def test_correct_setting_found():
    d = _diag()
    assert d["best"]["baud"] == 19200 and d["best"]["parity"] == "E"
    assert d["confidence"] >= 90


def test_baud_too_high_and_too_low():
    t = L.report_text(_diag(current=(38400, "E", 1)))
    assert "för HÖG" in t
    t = L.report_text(_diag(current=(9600, "E", 1)))
    assert "för LÅG" in t


def test_wrong_parity_flagged():
    t = L.report_text(_diag(current=(19200, "N", 1)))
    assert "paritet" in t.lower()


def test_ab_swapped():
    d = _diag(invert=True)
    assert d["best"] is None or d["best"]["frames"] < 3
    assert "OMKASTADE" in d["verdict"]


def test_single_slave_wrong_parity():
    d = _diag(slave_cfg={2: (19200, "N", 1)})
    assert d["best"]["parity"] == "E"
    assert any("Slav 2" in w for w in d["warnings"])
    assert not any("Slav 1" in w or "Slav 3" in w for w in d["warnings"])
    assert d["confidence"] < 90


def test_no_traffic():
    d = L.diagnose(L.sim_scan([], TX))
    assert "Ingen trafik" in d["verdict"]


def test_fault_sim_late_collision_intermittent():
    ch = M.simulate(30, *TX, faults={"late": {4: 0.1}, "collide": {5: 0.05}, "timeout": {2: 0.05}})
    mon = M.Monitor(*TX)
    M.replay(ch, mon)
    rows = {r["slav"]: r for r in mon.stats_rows()}
    assert rows[4]["sena"] > 0
    assert rows[5]["krockar"] > 0
    assert rows[2]["timeout"] > 0


# ---------------- master
def test_master_requires_arming():
    try:
        MM.Master(MM.SimSlaveLink())
        assert False
    except MM.NotArmed:
        pass


def test_master_fcs_and_scan():
    link = MM.SimSlaveLink(slaves=(1, 3), exc={3: (4,)})
    m = MM.Master(link, armed=True, timeout=0.03)
    assert m.request(1, 3, 10, 3)["values"] == [110, 111, 112]
    assert m.request(1, 6, 5, values=[42])["ok"]
    assert m.request(1, 16, 20, values=[1, 2, 3])["ok"]
    assert m.request(1, 3, 20, 3)["values"] == [1, 2, 3]
    assert m.request(1, 5, 2, values=[1])["ok"]
    assert m.request(1, 15, 8, values=[1, 1, 0, 1])["ok"]
    assert m.request(1, 1, 0, 12)["values"][2] == 1
    assert m.request(1, 1, 8, 4)["values"] == [1, 1, 0, 1]
    assert m.request(3, 4, 0, 1)["exception"] == 1
    assert m.request(2, 3, 0, 1)["text"].startswith("inget svar")
    found = [d["slav"] for d in m.scan(range(1, 6), timeout=0.02)]
    assert found == [1, 3]
    assert MM.format_values([0x4148, 0], fmt="f32") == [(0, 12.5)]
    assert MM.format_values([0xFFFF], fmt="s16") == [(0, -1)]


# ---------------- AI-agent
class SimHost:
    def __init__(self):
        ch = M.simulate(20, *TX, faults={"timeout": {5: 0.2}, "crc": {3: 0.05}})
        self.m = M.Monitor(*TX)
        M.replay(ch, self.m)
        self.base = time.time() - ch[-1][0]
        self.sent = []
        self.run = True

    def mon(self):
        return self.m

    def to_wall(self, t):
        return self.base + t

    def running(self):
        return self.run

    def names(self):
        return {"5": "Motor 5"}

    def status(self):
        return {"linje": "SIM 19200 8E1", "körs": self.run, "giltiga_ramar": self.m.crc_ok_frames}

    def start(self):
        self.run = True
        return {"körs": True}

    def stop(self):
        self.run = False
        return {"körs": False}

    def set_line(self, b, p, s):
        return {"linje": f"{b} 8{p}{s}"}

    def autodetect(self, seconds):
        return {"rapport": "sim"}

    def set_trap(self, slaves, pre, post):
        return {"felfälla": slaves}

    def master_request(self, *a):
        self.sent.append(a)
        return {"ok": True}

    def master_scan(self, a, b):
        return {"hittade": []}


class MockLLM:
    model = A.DEFAULT_MODEL

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat(self, messages, tools=None):
        self.seen.append((messages, [t["function"]["name"] for t in tools or []]))
        step = self.script.pop(0)
        if isinstance(step, str):
            return {"role": "assistant", "content": step}, {"prompt_tokens": 100, "completion_tokens": 20}
        calls = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                 for i, (n, a) in enumerate(step)]
        return {"role": "assistant", "content": None, "tool_calls": calls}, {"prompt_tokens": 50, "completion_tokens": 5}


def test_agent_tools_mock():
    host = SimHost()
    llm = MockLLM([[("get_status", {}), ("slave_stats", {})],
                   [("search_packets", {"filter": "addr=5 status=timeout", "limit": 5}),
                    ("error_events", {"since_min": 60, "slave": 5}), ("incidents", {})],
                   [("packets_around", {"time": "-5", "window_s": 3}), ("registers", {"slave": 1})],
                   "Slav 5 missar svar."])
    ag = A.Agent(host, llm)
    ag.tick()
    ans = ag.chat("Varför felar slav 5?")
    assert ans == "Slav 5 missar svar."
    tool_msgs = [m for m in ag.history if m["role"] == "tool"]
    assert len(tool_msgs) == 7
    st = json.loads(tool_msgs[1]["content"])
    s5 = [r for r in st if r["slav"] == 5][0]
    assert (s5["timeout"] + s5["sena"] + s5["krockar"]) > 0 and s5["namn"] == "Motor 5"
    sp = json.loads(tool_msgs[2]["content"])
    assert sp["träffar"] > 0 and len(sp["visar_senaste"]) <= 5
    ee = json.loads(tool_msgs[3]["content"])
    assert ee["antal"] > 0
    assert all(len(m["content"]) <= A.MAX_TOOL_CHARS + 60 for m in tool_msgs)
    assert "modbus_request" not in llm.seen[0][1]
    assert ag.usage["calls"] == 4 and ag.usage["cost"] > 0


def test_agent_send_gating():
    host = SimHost()
    allow = {"v": False}
    answers = []
    ag = A.Agent(host, MockLLM([]), allow_send=lambda: allow["v"],
                 confirm=lambda d: answers.append(d) or False)
    r = json.loads(ag.run_tool("modbus_request", {"slave": 1, "fc": 3, "addr": 0}))
    assert "inte tillåten" in r["fel"] and not host.sent
    allow["v"] = True
    assert "modbus_request" in [t["function"]["name"] for t in ag.tools()]
    r = json.loads(ag.run_tool("modbus_request", {"slave": 1, "fc": 3, "addr": 0}))
    assert "nekade" in r["fel"] and not host.sent and answers
    ag.confirm = lambda d: True
    json.loads(ag.run_tool("modbus_request", {"slave": 1, "fc": 3, "addr": 0}))
    assert host.sent


def test_auto_mode_and_trouble_mark():
    host = SimHost()
    llm = MockLLM([[("incidents", {"since_min": 60})], "Trolig orsak: slav 5."])
    ag = A.Agent(host, llm)
    ag.start_auto("fel 3 ggr per halvtimme")
    ag.tick()
    assert ag.journal.incidents
    ans = ag.chat("nu strular det")
    assert "slav 5" in ans
    sysmsg = llm.seen[0][0][0]["content"]
    assert "AUTOMATLÄGE" in sysmsg and "3 ggr" in sysmsg
    assert any("ANVÄNDAREN" in x for _t, x in ag.journal.marks)


def test_key_never_leaks():
    k = "sk" + "-or-v1-" + "a" * 40
    c = A.LLMClient(k, "http://127.0.0.1:9", "x", timeout=2)
    assert k not in repr(c)
    try:
        c.chat([{"role": "user", "content": "hej"}])
    except A.LLMError as e:
        assert k not in str(e)
    assert k not in A.redact(f"error {k}", "")
    assert A.mask(k).count("a") <= 4


def test_real_llm_if_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key or os.environ.get("ZT_SKIP_REAL_LLM"):
        print("skip real LLM")
        return
    host = SimHost()
    ag = A.Agent(host, A.LLMClient(key))
    ag.tick()
    ans = ag.chat("Vilken slav har problem och vad är trolig orsak? Svara kort.")
    assert "5" in ans, ans
    assert ag.usage["calls"] >= 2
    print("REAL LLM:", ans[:300].replace("\n", " "), "|", ag.usage_text())


if __name__ == "__main__":
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try:
                f()
                print("ok  ", n)
            except Exception as e:
                fails += 1
                print("FAIL", n, repr(e)[:300])
    sys.exit(1 if fails else 0)
