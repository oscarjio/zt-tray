"""
Cursor som AI-leverantör för AI-fliken, via den officiella Cursor Python SDK (cursor-sdk).

Hur det fungerar (enligt cursor.com/docs/sdk/python):
- SDK:n kör Cursors agent LOKALT (LocalAgentOptions) genom en "bridge" (Node, levereras i wheel-filen).
- zt-trays verktyg (search_packets, slave_stats, ...) exponeras som SDK:ns custom_tools. Agenten anropar dem
  via en loopback-server i den här processen - trafikdatan lämnar bara datorn i den form verktygen returnerar.
- Inbyggda verktyg (shell, filredigering m.m.) stängs av: tools=["mcp"] (bara custom/MCP-verktyg).
- Kostnad: körningar debiteras mot användarens Cursor-plan (samma prissättning/pooler som IDE:n, taggat "SDK"
  i usage-dashboarden). Tokenantal visas i fliken.

cursor-sdk är proprietär och ~48 MB (Windows-wheel) - den buntas INTE i zt-tray.exe eller i repot. Första gången
hämtas den officiella wheel-filen från PyPI, SHA-256 kontrolleras mot PyPI:s uppgift, och den packas upp under
användarprofilen (APP_DIR/cursor-sdk/<version>). Är paketet redan installerat i Python används det.

Nyckeln (cfg["ai"]["cursor_api_key"] eller CURSOR_API_KEY) skrivs aldrig till logg/journal/felmeddelanden.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import urllib.request
import zipfile

SDK_VERSION = "1.2.0"
DEFAULT_MODEL = "composer-2.5"
MODELS = ["composer-2.5", "auto"]
PYPI_JSON = "https://pypi.org/pypi/cursor-sdk/{v}/json"


def get_key(cfg=None):
    k = ((cfg or {}).get("ai") or {}).get("cursor_api_key") or ""
    return k.strip() or os.environ.get("CURSOR_API_KEY", "").strip()


def _plat_tag():
    if sys.platform == "win32":
        return "win_amd64"
    if sys.platform == "darwin":
        import platform
        return "macosx_11_0_arm64" if platform.machine() == "arm64" else "macosx_11_0_x86_64"
    import platform
    return "manylinux2014_aarch64" if platform.machine() in ("aarch64", "arm64") else "manylinux2014_x86_64"


def sdk_dir(app_dir):
    return os.path.join(str(app_dir), "cursor-sdk", SDK_VERSION)


def _http_get(url, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": "zt-tray"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def install_sdk(app_dir, progress=lambda s: None, fetch=_http_get):
    """Hämtar officiell cursor-sdk-wheel från PyPI, verifierar SHA-256 och packar upp. Returnerar katalogen."""
    dst = sdk_dir(app_dir)
    if os.path.isfile(os.path.join(dst, "cursor_sdk", "__init__.py")):
        return dst
    progress("Hämtar Cursor SDK-information från PyPI …")
    meta = json.loads(fetch(PYPI_JSON.format(v=SDK_VERSION)).decode("utf-8"))
    tag = _plat_tag()
    cand = [u for u in meta.get("urls", []) if u.get("packagetype") == "bdist_wheel" and tag in u.get("filename", "")]
    if not cand:
        raise RuntimeError(f"Ingen cursor-sdk {SDK_VERSION}-wheel för {tag} på PyPI.")
    u = cand[0]
    if not u.get("url", "").startswith("https://files.pythonhosted.org/"):
        raise RuntimeError("Oväntad nedladdningsadress för cursor-sdk - avbryter.")
    progress(f"Laddar ner {u['filename']} ({u.get('size', 0) // 1_000_000} MB) …")
    data = fetch(u["url"])
    if hashlib.sha256(data).hexdigest() != u["digests"]["sha256"]:
        raise RuntimeError("SHA-256 för cursor-sdk stämmer inte med PyPI - avbryter.")
    progress("Packar upp …")
    tmp = dst + ".tmp"
    os.makedirs(tmp, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if n.startswith("cursor_sdk/") and ".." not in n and not n.startswith("/"):
                z.extract(n, tmp)
    os.replace(tmp, dst)
    progress("Cursor SDK installerad.")
    return dst


def _patch_bridge_launch(mod_bridge):
    """Windows: kör node.exe direkt (inte via .cmd) och utan konsolfönster."""
    if sys.platform != "win32" or getattr(mod_bridge, "_zt_patched", False):
        return
    real = subprocess.Popen

    def popen(argv, *a, **kw):
        try:
            if isinstance(argv, (list, tuple)) and argv and str(argv[0]).lower().endswith("cursor-sdk-bridge.cmd"):
                b = os.path.dirname(str(argv[0]))
                node = os.path.join(b, "node.exe")
                js = os.path.normpath(os.path.join(b, "..", "dist", "bin", "cursor-sdk-bridge.js"))
                if os.path.isfile(node) and os.path.isfile(js):
                    argv = [node, js] + list(argv[1:])
        except Exception:
            pass
        kw.setdefault("creationflags", 0x08000000)  # CREATE_NO_WINDOW
        return real(argv, *a, **kw)

    # SDK:ns synkrona bridge-start läser stderr med selectors, vilket inte fungerar för pipes på Windows
    # (WinError 10038). Ersätt med en trådläsare som också fortsätter tömma stderr så att bridgen aldrig blockerar.
    if hasattr(mod_bridge, "_read_discovery") and hasattr(mod_bridge, "parse_discovery_line"):
        def _read_discovery(process, timeout):
            import queue
            q = queue.Queue()

            def pump():
                try:
                    for line in process.stderr:
                        q.put(line)
                except Exception:
                    pass
                q.put(None)
            threading.Thread(target=pump, daemon=True, name="cursor-bridge-stderr").start()
            import time as _t
            deadline = _t.monotonic() + timeout
            while _t.monotonic() < deadline:
                try:
                    line = q.get(timeout=0.2)
                except Exception:
                    continue
                if line is None:
                    raise RuntimeError(f"Cursor-bridgen avslutades innan start (status {process.poll()})")
                d = mod_bridge.parse_discovery_line(line)
                if d is not None:
                    threading.Thread(target=lambda: [None for _ in iter(q.get, None)], daemon=True).start()
                    return d
            raise RuntimeError("Tidsgräns: Cursor-bridgen startade inte")
        mod_bridge._read_discovery = _read_discovery

    class _SP:
        def __getattr__(self, n):
            return getattr(subprocess, n)
    sp = _SP()
    sp.Popen = popen
    mod_bridge.subprocess = sp
    mod_bridge._zt_patched = True


def load_sdk(app_dir, progress=lambda s: None, allow_download=True):
    """Importerar cursor_sdk: installerat paket, annars nedladdad wheel under APP_DIR."""
    try:
        if not getattr(sys, "frozen", False):
            import cursor_sdk  # noqa: F401
            import cursor_sdk._bridge as br
            _patch_bridge_launch(br)
            return sys.modules["cursor_sdk"]
    except ImportError:
        pass
    d = sdk_dir(app_dir)
    if not os.path.isfile(os.path.join(d, "cursor_sdk", "__init__.py")):
        if not allow_download:
            raise RuntimeError("Cursor SDK är inte installerad.")
        install_sdk(app_dir, progress)
    if d not in sys.path:
        sys.path.insert(0, d)
    import cursor_sdk
    import cursor_sdk._bridge as br
    _patch_bridge_launch(br)
    return cursor_sdk


class CursorClient:
    """Används som 'client' i ai_agent.Agent. Agent.chat delegerar hit (run_agent)."""
    provider = "cursor"

    def __init__(self, api_key, model=DEFAULT_MODEL, app_dir=".", sdk=None, progress=lambda s: None):
        self._key = api_key
        self.model = model or DEFAULT_MODEL
        self.app_dir = str(app_dir)
        self._sdk = sdk
        self._progress = progress
        self._agent = None
        self._first = True
        self._lock = threading.Lock()

    def __repr__(self):
        return f"CursorClient(model={self.model!r}, key=***)"

    def sdk(self):
        if self._sdk is None:
            self._sdk = load_sdk(self.app_dir, self._progress)
        return self._sdk

    def _tools(self, agent, on_step):
        S = self.sdk()
        out = {}
        for t in agent.all_tools():
            fn = t["function"]
            name = fn["name"]

            def ex(args, ctx=None, _n=name):
                a = dict(args or {})
                if on_step[0]:
                    on_step[0](f"→ {_n}({json.dumps(a, ensure_ascii=False)[:120]})")
                return agent.run_tool(_n, a)
            out[name] = S.CustomTool(description=fn.get("description", ""),
                                     input_schema=fn.get("parameters") or {"type": "object", "properties": {}},
                                     execute=ex)
        return out

    def _create(self, agent, on_step):
        S = self.sdk()
        ws = os.path.join(self.app_dir, "cursor-arbetsyta")
        os.makedirs(ws, exist_ok=True)
        local = S.LocalAgentOptions(cwd=ws, custom_tools=self._tools(agent, on_step))
        try:
            return S.Agent.create(S.AgentOptions(model=self.model, api_key=self._key, local=local, tools=["mcp"]))
        except Exception as e:
            if "BadRequest" not in type(e).__name__:
                raise
            # äldre/annan tolkning av tools-listan: stäng av skal och filredigering i stället
            return S.Agent.create(S.AgentOptions(model=self.model, api_key=self._key, local=local,
                                                 disallowed_tools=["shell"]))

    def run_agent(self, agent, prompt_text, on_step=None, system=""):
        with self._lock:
            if self._agent is None:
                hook = [on_step]
                try:
                    self._agent = self._create(agent, hook)
                except Exception as e:
                    import ai_agent
                    raise ai_agent.LLMError(self._clean(e)) from None
                self._hook = hook
                self._first = True
            self._hook[0] = on_step
            msg = ((system + "\n\n") if self._first else "") + prompt_text
            self._first = False
            try:
                run = self._agent.send(msg)
                text_parts = []
                for m in run.messages():
                    if getattr(m, "type", None) == "assistant":
                        for b in getattr(m.message, "content", []) or []:
                            if getattr(b, "type", None) == "text":
                                text_parts.append(b.text)
                res = run.wait()
            except Exception as e:
                import ai_agent
                raise ai_agent.LLMError(self._clean(e)) from None
            u = getattr(res, "usage", None)
            usage = {"prompt_tokens": getattr(u, "input_tokens", 0) if u else 0,
                     "completion_tokens": getattr(u, "output_tokens", 0) if u else 0}
            status = getattr(res, "status", "finished")
            text = (getattr(res, "result", "") or "".join(text_parts)).strip()
            if status != "finished":
                text = (text + f"\n(Cursor-körningen slutade med status {status})").strip()
            return text, usage

    def _clean(self, e):
        s = f"{type(e).__name__}: {e}"
        if self._key:
            s = s.replace(self._key, "***")
        return s[:500]

    def close(self):
        a, self._agent = self._agent, None
        if a is not None:
            try:
                a.close()
            except Exception:
                pass
