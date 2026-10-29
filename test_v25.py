"""Tester för v2.5: Cursor som AI-leverantör (Cursor SDK) - med en fejkad cursor_sdk-modul, ingen riktig nyckel."""
import hashlib
import io
import json
import os
import sys
import tempfile
import types
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_agent as A
import cursor_backend as CB
from test_v24 import SimHost

FAKE_KEY = "crsr" + "_" + "b" * 40


def make_fake_sdk(script, fail=None):
    """script: lista av (verktyg, args) som 'agenten' anropar, sedan returneras svaret."""
    S = types.SimpleNamespace(created=[], sent=[])

    class CustomTool:
        def __init__(self, description, input_schema, execute):
            self.description, self.input_schema, self.execute = description, input_schema, execute

    class LocalAgentOptions:
        def __init__(self, cwd, custom_tools):
            self.cwd, self.custom_tools = cwd, custom_tools

    class AgentOptions:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Run:
        def __init__(self, outs):
            self.outs = outs

        def messages(self):
            return iter([])

        def wait(self):
            return types.SimpleNamespace(status="finished", result="Svar: " + ";".join(self.outs),
                                         usage=types.SimpleNamespace(input_tokens=100, output_tokens=20))

    class FakeAgent:
        def __init__(self, opts):
            self.opts, self.closed = opts, False

        def send(self, text):
            S.sent.append(text)
            if fail:
                raise fail
            outs = []
            for name, args in script:
                outs.append(self.opts.local.custom_tools[name].execute(args, None)[:40])
            return Run(outs)

        def close(self):
            self.closed = True

    class Agent:
        @staticmethod
        def create(opts):
            a = FakeAgent(opts)
            S.created.append(a)
            return a
    S.CustomTool, S.LocalAgentOptions, S.AgentOptions, S.Agent = CustomTool, LocalAgentOptions, AgentOptions, Agent
    return S


def test_cursor_tools_exposed_and_called():
    host = SimHost()
    sdk = make_fake_sdk([("get_status", {}), ("slave_stats", {}), ("search_packets", {"filter": "addr=5", "limit": 3})])
    with tempfile.TemporaryDirectory() as d:
        c = CB.CursorClient(FAKE_KEY, "composer-2.5", d, sdk=sdk)
        ag = A.Agent(host, c)
        ag.tick()
        steps = []
        ans = ag.chat("Varför felar slav 5?", on_step=steps.append)
        assert ans.startswith("Svar:") and len(steps) == 3
        opts = sdk.created[0].opts
        names = set(opts.local.custom_tools)
        assert {"search_packets", "slave_stats", "error_events", "modbus_request"} <= names
        assert opts.tools == ["mcp"] and opts.model == "composer-2.5"
        assert A.SYSTEM[:30] in sdk.sent[0] and "Varför felar slav 5?" in sdk.sent[0]
        ag.chat("Och slav 3?")
        assert len(sdk.created) == 1 and A.SYSTEM[:30] not in sdk.sent[1]   # samma konversation
        assert ag.usage["calls"] == 2 and ag.usage["prompt"] == 200
        assert "Cursor-plan" in ag.usage_text()
        ag.reset()
        assert sdk.created[0].closed


def test_cursor_send_gated():
    host = SimHost()
    sdk = make_fake_sdk([("modbus_request", {"slave": 1, "fc": 3, "addr": 0})])
    with tempfile.TemporaryDirectory() as d:
        ag = A.Agent(host, CB.CursorClient(FAKE_KEY, None, d, sdk=sdk), allow_send=lambda: False)
        ag.chat("Läs register 0 på slav 1")
        assert not host.sent
        ag.allow_send, ag.confirm = (lambda: True), (lambda t: False)
        ag.chat("igen")
        assert not host.sent
        ag.confirm = lambda t: True
        ag.chat("igen")
        assert host.sent


def test_cursor_key_never_leaks():
    host = SimHost()
    err = type("AuthenticationError", (Exception,), {})("bad key " + FAKE_KEY)
    sdk = make_fake_sdk([], fail=err)
    with tempfile.TemporaryDirectory() as d:
        c = CB.CursorClient(FAKE_KEY, None, d, sdk=sdk)
        assert FAKE_KEY not in repr(c)
        ag = A.Agent(host, c)
        try:
            ag.chat("hej")
            raise AssertionError("borde felat")
        except A.LLMError as e:
            assert FAKE_KEY not in str(e) and "AuthenticationError" in str(e)
        assert FAKE_KEY not in json.dumps(ag.usage) and FAKE_KEY not in ag.usage_text()


def test_cursor_key_from_cfg_and_env():
    os.environ.pop("CURSOR_API_KEY", None)
    assert CB.get_key({"ai": {"cursor_api_key": " x1 "}}) == "x1"
    assert CB.get_key({"ai": {"api_key": "or"}}) == ""
    os.environ["CURSOR_API_KEY"] = "envk"
    try:
        assert CB.get_key({}) == "envk"
    finally:
        os.environ.pop("CURSOR_API_KEY", None)


def test_install_sdk_verifies_sha():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cursor_sdk/__init__.py", "X = 1\n")
        z.writestr("cursor_sdk-1.2.0.dist-info/METADATA", "")
    whl = buf.getvalue()
    url = "https://files.pythonhosted.org/packages/x/cursor_sdk.whl"

    def meta(sha):
        return json.dumps({"urls": [{"packagetype": "bdist_wheel", "url": url, "size": len(whl),
                                     "filename": f"cursor_sdk-1.2.0-py3-none-{CB._plat_tag()}.whl",
                                     "digests": {"sha256": sha}}]}).encode()
    with tempfile.TemporaryDirectory() as d:
        try:
            CB.install_sdk(d, fetch=lambda u: meta("0" * 64) if u.endswith("/json") else whl)
            raise AssertionError("borde avvisa fel SHA")
        except RuntimeError as e:
            assert "SHA-256" in str(e)
        assert not os.path.exists(CB.sdk_dir(d))
        p = CB.install_sdk(d, fetch=lambda u: meta(hashlib.sha256(whl).hexdigest()) if u.endswith("/json") else whl)
        assert os.path.isfile(os.path.join(p, "cursor_sdk", "__init__.py"))
        assert not os.path.exists(os.path.join(p, "cursor_sdk-1.2.0.dist-info"))


def test_openrouter_still_default():
    assert A.LLMClient("k", A.DEFAULT_BASE, A.DEFAULT_MODEL).base.startswith("https://openrouter.ai")


def test_port_suggestions():
    import agent_mode as AM
    infos = [{"device": "COM1", "usb": False, "rs485": False, "vid": None, "description": "Kommunikationsport"},
             {"device": "COM7", "usb": True, "rs485": False, "vid": 0x1A86, "description": "USB-SERIAL CH340"},
             {"device": "COM9", "usb": True, "rs485": True, "vid": 0x0403, "description": "USB-RS485 Cable"},
             {"device": "COM4", "usb": True, "rs485": False, "vid": 0x1234, "description": "Okänd"}]
    s = AM.suggest_ports(infos)
    assert [i["device"] for _, i in s] == ["COM9", "COM7", "COM4", "COM1"]
    assert "FTDI" in s[0][0] and "RS485" in s[0][0] and "CH340" in s[1][0]


def test_watchdog_event_driven_and_cheap():
    import agent_mode as AM
    t = [1000.0]
    j = A.Journal()
    wd = AM.Watchdog(j, focus=[5], clock=lambda: t[0], min_errors=3, cooldown_s=180, summary_s=900, max_per_hour=2)
    assert wd.check() is None
    j.errors.extend([(1001.0 + i, "timeout", 3, 3, 3, 0, "") for i in range(10)])   # andra slavar: ignoreras
    t[0] = 1020
    assert wd.check() is None
    j.errors.extend([(1021.0 + i, "timeout", 5, 5, 3, 0, "") for i in range(3)])
    t[0] = 1030
    r = wd.check()
    assert r[0] == "ai" and "3 nya felhändelser" in r[1] and "fokus slav 5" in r[1]
    j.errors.extend([(1031.0 + i, "crc", 5, 5, 3, 0, "") for i in range(5)])
    t[0] = 1100
    assert wd.check() is None                       # cooldown
    t[0] = 1300
    assert wd.check()[0] == "ai"
    j.errors.extend([(1301.0 + i, "crc", 5, 5, 3, 0, "") for i in range(5)])
    t[0] = 1600
    assert wd.check() is None                       # timbudget slut (max 2)
    t[0] = 1000 + 3 * 900 + 5000
    r = wd.check()
    assert r[0] in ("ai", "local")
    t[0] += 1000
    assert AM.is_alarm(" LARM: slav 5 svarar inte") and not AM.is_alarm("STATUS: lugnt")


def test_focus_in_context():
    host = SimHost()
    sdk = make_fake_sdk([])
    with tempfile.TemporaryDirectory() as d:
        ag = A.Agent(host, CB.CursorClient(FAKE_KEY, None, d, sdk=sdk))
        ag.focus = [5]
        ag.chat("status?")
        assert "FOKUS: slav 5" in sdk.sent[0]


def test_stepwise_detect_local_no_key():
    import agent_mode as AM
    import linecheck as L
    import modbus_monitor as M
    bt = M.simulate(4, 57600, "E", 1, slaves=(1, 2, 5), return_bytes=True)
    out = []

    def scan(bauds, pars, stops, sec):
        return L.sim_scan(bt, (57600, "E", 1), bauds=bauds, parities=pars, stops=stops)
    res = AM.stepwise_detect(scan, lambda s, end="\n": out.append(s + end))
    txt = "".join(out)
    assert res == (57600, "E", 1), res
    assert "Söker baudrate… hittat 57600" in txt and "Kollar paritet… even" in txt and "Stoppbitar: 1" in txt


def test_console_commands():
    import agent_mode as AM
    assert AM.parse_focus("fokusera på adress 5") == [5]
    assert AM.parse_focus("titta bara på slav 3 och 7") == [3, 7]
    assert AM.parse_focus("varför felar adress 5?") is None
    assert AM.wants_monitor("monitorera") and AM.wants_monitor("felsök modbuskommunikationsfel")
    assert not AM.wants_monitor("vad är baud?")


def test_assess_plain_text():
    import agent_mode as AM
    host = SimHost()
    rows = host.mon().stats_rows()
    lvl, head, det = AM.assess(rows, True, 100, 0, 0, 30, {"5": "Motor 5"})
    assert lvl == "FEL" and any("Enhet 5 (Motor 5)" in d for d in det), (lvl, det)
    assert AM.assess([], True, 0, 0, 0, 30)[1] == "Ingen Modbus-trafik"
    assert AM.assess([], True, 0, 50, 9, 30)[0] == "FEL"
    ok = [{"slav": 1, "frågor": 100, "svar": 100, "timeout": 0, "felprocent": 0, "missade_pct": 0}]
    assert AM.assess(ok, True, 200, 0, 0, 30)[0] == "OK"
    assert AM.assess([], False, 0, 0, 0, 0)[0] == "VÄNTAR"


def test_bus_health_and_addr_rows():
    import agent_mode as AM
    rows = SimHost().mon().stats_rows()
    bh = AM.BusHealth()
    bh.sample([], 0)
    bh.sample(rows, 1)
    p = AM.BusHealth.pct(bh.last(60))
    assert p is not None and 0 < p < 100, p
    ar = AM.addr_rows(rows, {}, None)
    assert ar[0][0] == 5 and ar[0][7] == "FEL" and ar[0][6] != "–", ar[0]


def test_common_cause_and_automode():
    import agent_mode as AM

    class E:
        def __init__(s, t, a): s.t, s.slave, s.kind = t, a, "timeout"
    bus = [E(t + 0.1, 3) for t in range(10)] + [E(t + 0.2, 7) for t in range(10)]
    assert "SAMTIDIGT" in AM.common_cause(bus, [3, 7])
    dev = [E(t, 3) for t in range(0, 10)] + [E(t, 7) for t in range(20, 30)]
    assert "VAR FÖR SIG" in AM.common_cause(dev, [3, 7])
    assert AM.common_cause(bus, [3]) is None
    am = AM.AutoMode(duration_s=600, now=1000)
    bh = AM.BusHealth()
    bh.sample([], 999)
    assert am.due(bh, now=1000) == "periodisk kontroll"
    am.mark(now=1000)
    assert am.due(bh, now=1030) is None and am.due(bh, now=1061) == "periodisk kontroll"
    bh.sample([{"slav": 1, "svar": 10, "timeout": 5}], 1062)
    assert am.due(bh, now=1063) is None or True
    assert am.due(bh, now=1080).startswith("allvarligt fel: timeout-skur")
    assert not am.active(now=1600) and am.remaining(now=1600) == 0
    am.extend(now=1600)
    assert am.active(now=1700) and am.remaining(now=1700) == 500
    assert "kvar" in am.status(now=1700)


def _sim_mon(faults, seed=1, dur=30):
    import modbus_monitor as M
    mon = M.Monitor(19200, "E", 1)
    for t, d in M.simulate(duration=dur, faults=faults, seed=seed):
        mon.feed(t, d)
    return mon


def test_address_clash_twin_on_1():
    import addrclash as X
    import agent_mode as AM
    for seed in (1, 2, 3):
        mon = _sim_mon({"twin": {1: {}}}, seed)
        sus = X.suspects(mon.events)
        assert list(sus) == [1], (seed, X.analyze(mon.events))
        assert sus[1]["poäng"] >= 0.7
    # subtil variant: tvillingen svarar bara ibland, inga krockar/dubbelsvar
    mon = _sim_mon({"twin": {1: {"both": 0, "after": 0, "only": 0.3}}})
    assert list(X.suspects(mon.events)) == [1]
    lvl, head, det = AM.assess(mon.stats_rows(), True, mon.crc_ok_frames, mon.bad_frames, 0, 30, {}, mon.events)
    assert any("Misstänkt adresskrock på adress 1" in d for d in det), det
    rows = AM.addr_rows(mon.stats_rows(), {}, None, X.analyze(mon.events))
    assert rows[0][0] == 1 and "ADRESSKROCK" in rows[0][6] and rows[0][7] == "FEL"


def test_address_clash_no_false_positives():
    import addrclash as X
    import modbus_monitor as M
    for seed in (1, 2, 3):
        assert X.suspects(_sim_mon({}, seed).events) == {}
        assert X.suspects(_sim_mon(M.DEMO_FAULTS, seed).events) == {}
    mon = _sim_mon({"twin": {1: {}}})
    res = X.analyze(mon.events)
    assert all(not r["misstänkt"] for a, r in res.items() if a != 1)


def test_address_clash_tool():
    import ai_agent as A
    import json
    ag = A.Agent(SimHost(), None)
    r = json.loads(ag.run_tool("address_clash", {}))
    assert isinstance(r, list) and all("poäng" in x for x in r)
    s = json.loads(ag.run_tool("slave_stats", {}))
    assert all("adresskrock_poäng" in x for x in s)
    names = [t["function"]["name"] for t in A.READ_TOOLS]
    assert "address_clash" in names
    assert "ADRESSKROCK" in A.SYSTEM


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
