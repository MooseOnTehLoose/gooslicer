import time

import pytest

from elegoo_goo import goo
from elegoo_lan import client as C, discovery, upload as U
from elegoo_lan.protocol import Printer
from sim_printer import SimPrinter


def wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def sim():
    s = SimPrinter(layer_period=0.02)
    yield s
    s.close()


@pytest.fixture
def session(sim):
    (p,) = discovery.discover(0.3, targets=["127.0.0.1"], port=sim.udp_port)
    sess = C.PrinterSession(p, port=sim.ws_port, http_port=sim.http_port, refresh_s=0.2)
    events = []
    sess.on_connection = lambda ok, msg: events.append(ok)
    sess.start()
    assert wait(lambda: sess.connected and sess.state.attributes)
    yield sess
    sess.close()


def small_goo(tmp_path, layers=5, name="cube.goo"):
    h = goo.Header(machine_name="ELEGOO Saturn 4 Ultra 16K", machine_type="ELEGOO Saturn 4 Ultra 16K",
                   resolution_x=15120, resolution_y=6230, layer_count=layers, layer_height=0.05)
    from gooslicer.raster import RasterSpec, rasterize_rle
    empty = rasterize_rle([], RasterSpec(15120, 6230, 14.0, 19.0))
    path = tmp_path / name
    goo.write(str(path), h, (goo.Layer(goo.default_layer_def(h, i), empty) for i in range(layers)))
    return path


def test_discovery_and_live_status(sim, session):
    assert session.printer.machine_name == "Saturn 4 Ultra 16K"
    assert session.state.attributes["Resolution"] == "15120x6230"
    assert wait(lambda: session.state.status)
    assert session.state.summary() == "Idle" and not session.state.is_busy
    n = len([c for c in sim.commands if c["Data"]["Cmd"] == 0])
    assert wait(lambda: len([c for c in sim.commands if c["Data"]["Cmd"] == 0]) >= n + 2)  # periodic refresh


def test_upload_print_pause_resume_stop(sim, session, tmp_path):
    path = small_goo(tmp_path, layers=400)
    got = []
    session.upload(str(path), progress=lambda d, t: got.append((d, t)))
    assert sim.files["cube.goo"] == path.read_bytes() and got[-1][0] == got[-1][1]
    assert [f["name"] for f in session.list_files()] == ["/local/cube.goo"]
    session.start_print("cube.goo")
    assert wait(lambda: session.state.is_printing and session.state.print_info.get("CurrentLayer", 0) > 2)
    assert 0 < session.state.progress < 1 and session.state.remaining_s > 0
    with pytest.raises(C.SessionError, match="busy"):
        session.start_print("cube.goo")
    session.pause()
    assert wait(lambda: session.state.is_paused)
    assert "Paused" in session.state.summary()
    session.resume()
    assert wait(lambda: not session.state.is_paused)
    session.stop_print()
    assert wait(lambda: not session.state.is_printing)
    assert session.state.print_status == 8


def test_print_runs_to_completion(sim, session, tmp_path):
    session.upload(str(small_goo(tmp_path, layers=10)))
    session.start_print("cube.goo")
    assert wait(lambda: session.state.print_status == 9)
    assert session.state.progress == 1.0


def test_missing_file_and_delete(sim, session, tmp_path):
    with pytest.raises(C.SessionError, match="not found"):
        session.start_print("nope.goo")
    session.upload(str(small_goo(tmp_path)))
    session.delete_files(["/local/cube.goo"])
    assert session.list_files() == []


def test_upload_cancel_sends_cancel_transfer(sim, session, tmp_path):
    path = tmp_path / "big.goo"
    path.write_bytes(b"x" * (3 * (1 << 20) + 5))
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) > 1          # cancel after the first chunk
    with pytest.raises(U.UploadCancelled):
        session.upload(str(path), cancelled=cancelled)
    assert "big.goo" not in sim.files
    assert any(c["Data"]["Cmd"] == 255 for c in sim.commands) and not sim.partial


def test_reconnects_after_drop(sim, session):
    states = []
    session.on_connection = lambda ok, msg: states.append(ok)
    for c in list(sim.clients):
        c.close()
    assert wait(lambda: states[:1] == [False] and session.connected, 15)


def test_incompatible_printer_refused():
    old = Printer(name="Old", machine_name="Saturn 3 Ultra", protocol_version="V1.0.0", ip="127.0.0.1")
    with pytest.raises(Exception, match="no longer"):
        C.PrinterSession(old).start()


def test_request_without_connection_fails_fast():
    s = C.PrinterSession(Printer(protocol_version="V3.0.0", ip="127.0.0.1"))
    with pytest.raises(C.SessionError, match="not connected"):
        s.pause()


def test_file_check_against_printer():
    st = C.PrinterState(attributes={"Resolution": "15120x6230", "MachineName": "Saturn 4 Ultra 16K",
                                    "SupportFileType": ["CTB", "GOO"]})
    pr = Printer(machine_name="Saturn 4 Ultra 16K")
    ok = goo.Header(machine_type="ELEGOO Saturn 4 Ultra 16K", resolution_x=15120, resolution_y=6230)
    assert C.check_file_for_printer(ok, st, pr) == []
    wrong = goo.Header(machine_type="ELEGOO Saturn 4 Ultra", resolution_x=11520, resolution_y=5120)
    probs = C.check_file_for_printer(wrong, st, pr)
    assert len(probs) == 2 and "11520" in probs[0]


def test_status_descriptions():
    st = C.PrinterState(status={"CurrentStatus": [1], "PrintInfo": {"Status": 42, "CurrentLayer": 5,
                                                                     "TotalLayer": 10, "CurrentTicks": 1000,
                                                                     "TotalTicks": 5000}})
    assert st.summary() == "Printing — status 42"      # unknown codes are shown, not guessed
    assert st.progress == 0.5 and st.remaining_s == 4.0


def test_video_stream_on_and_off(sim, session):
    assert session.has_camera() is True
    v = session.start_video()
    assert v.url == "rtsp://127.0.0.1:554/video" and v.ack == 0 and v.note == ""
    session.stop_video()
    assert wait(lambda: sim.video_on == [1, 0])
    # viewer limit reached: the 16K still serves its default URL, so it is passed back, not raised
    sim.video_ack = 1
    v = session.start_video()
    assert v.ack == 1 and "limit" in v.note and v.url == C.default_video_url("127.0.0.1")
    sim.camera = False
    with pytest.raises(C.SessionError, match="no camera"):
        session.start_video()
    session.request(1)
    assert wait(lambda: session.state.attributes.get("CameraStatus") == 0)
    assert session.has_camera() is False
