"""v2.5.3: COM-port/portuppräkning får aldrig frysa GUI:t (vitt fönster)."""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import modbus_monitor as M


def test_hanging_enumeration_does_not_block():
    orig, cache = M._port_infos_raw, M.PORTS
    M._port_infos_raw = lambda: time.sleep(30) or []
    M.PORTS = M._PortCache()
    try:
        t = time.perf_counter()
        assert M.port_infos() == []
        for _ in range(20):
            M.port_infos()
        assert time.perf_counter() - t < 1.0, "port_infos blockerade"
    finally:
        M._port_infos_raw, M.PORTS = orig, cache


def test_hanging_open_times_out_and_late_port_closed():
    closed = threading.Event()

    class P:
        def close(self):
            closed.set()
    orig = M.open_port
    M.open_port = lambda *a: (time.sleep(1.0), P())[1]
    try:
        cap = M.Capture(M.Monitor(19200, "E", 1), "COM99", 19200, "E", 1)
        t = time.perf_counter()
        try:
            cap.start(timeout=0.3)
            raise AssertionError("borde ge TimeoutError")
        except TimeoutError as e:
            assert "hänger" in M.explain_open_error("COM99", e)
        assert time.perf_counter() - t < 0.8
        assert closed.wait(3), "sent öppnad port stängdes inte"
    finally:
        M.open_port = orig


def test_error_texts():
    e = PermissionError(13, "could not open port 'COM5': PermissionError(13, 'Access is denied.', None, 5)")
    assert "UPPTAGEN" in M.explain_open_error("COM5", e) and "Access is denied" in M.explain_open_error("COM5", e)
    e = FileNotFoundError(2, "could not open port 'COM7': FileNotFoundError(2, 'The system cannot find the file specified.')")
    assert "FINNS INTE" in M.explain_open_error("COM7", e)
    e = OSError(5, "Kan inte öppna COM3 (fel 5)")
    assert "UPPTAGEN" in M.explain_open_error("COM3", e)


def test_gui_stays_responsive_during_hanging_port():
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as e:
        print("hoppar över Tk-test:", e)
        return
    orig, cache = M._port_infos_raw, M.PORTS
    M._port_infos_raw = lambda: time.sleep(30) or []
    M.PORTS = M._PortCache()
    ticks = []
    try:
        root.withdraw()

        def tick():
            ticks.append(time.perf_counter())
            M.port_infos()          # som GUI-flikarnas poll
            root.after(50, tick)
        root.after(0, tick)
        root.after(1500, root.quit)
        root.mainloop()
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        assert len(ticks) >= 10, f"GUI hann bara {len(ticks)} varv"
        assert max(gaps) < 0.5, f"GUI frös {max(gaps):.2f} s"
    finally:
        root.destroy()
        M._port_infos_raw, M.PORTS = orig, cache


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("OK", n)
    print("ALLA OK")
