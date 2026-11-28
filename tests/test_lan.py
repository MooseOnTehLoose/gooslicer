import hashlib
import json
import os
import socket

import pytest

from elegoo_lan import discovery, protocol, upload, ws
from elegoo_lan.protocol import Cmd

from fake_printer import MAINBOARD, UdpResponder, UploadServer, WsServer, discovery_reply


# ---- constants observed in the binary --------------------------------------
def test_observed_constants():
    assert protocol.DISCOVERY_PROBE == b"M99999"
    assert protocol.DISCOVERY_PORT == 3000
    assert protocol.WS_PORT == 3030 and protocol.WS_PATH == "/websocket"
    assert protocol.UPLOAD_PATH == "/uploadFile/upload"
    assert protocol.UPLOAD_CHUNK == 0x100000 and protocol.UPLOAD_RETRIES == 3


# ---- envelope parity (spec 2a / 2b) -----------------------------------------
def test_ws_envelope_shape():
    m = protocol.build_ws_request(Cmd.START_PRINT, "MB1", "C1", {"Filename": "a.goo"},
                                  request_id="r" * 32, now=1700000000.9)
    assert m == {"Id": "C1", "Topic": "sdcp/request/MB1", "Data": {
        "Cmd": 128, "Data": {"Filename": "a.goo", "StartLayer": 0}, "RequestID": "r" * 32,
        "MainboardID": "MB1", "TimeStamp": 1700000000, "From": 0}}


def test_ws_request_id_format():
    rid = protocol.build_ws_request(0, "MB", "C")["Data"]["RequestID"]
    assert len(rid) == 32 and "-" not in rid and int(rid, 16) >= 0


@pytest.mark.parametrize("cmd,args,expected", [
    (Cmd.START_PRINT, {"Filename": "f", "StartLayer": 3}, {"Filename": "f", "StartLayer": 3}),
    (Cmd.RENAME, {"Name": "n"}, {"Name": "n"}),
    (Cmd.CANCEL_TRANSFER, {"Uuid": "u", "FileName": "f"}, {"Uuid": "u", "FileName": "f"}),
    (Cmd.FILE_LIST, {"Url": "/local"}, {"Url": "/local"}),
    (Cmd.DELETE_FILES, {"FileList": ["a"]}, {"FileList": ["a"], "FolderList": []}),
    (Cmd.HISTORY_DETAIL, {"Id": ["t1"]}, {"Id": ["t1"]}),
    (Cmd.VIDEO_STREAM, {"Enable": 1}, {"Enable": 1}),
    (Cmd.TIMELAPSE, {}, {"Enable": 0}),
    (Cmd.PAUSE, {"junk": 1}, {}),
    (Cmd.STATUS, None, {}),
    (999, {"x": 1}, {}),
])
def test_payload_table(cmd, args, expected):
    assert protocol.build_payload(cmd, args) == expected


# ---- discovery parsing --------------------------------------------------------
def test_parse_ignores_echo_and_garbage():
    assert protocol.parse_discovery_reply(b"M99999") is None
    assert protocol.parse_discovery_reply(b"\xff\xfe") is None
    assert protocol.parse_discovery_reply(b'{"NoId":1}') is None
    assert protocol.parse_discovery_reply(b"[1,2]") is None


@pytest.mark.parametrize("nested", [True, False])
def test_parse_reply_layouts(nested):
    p = protocol.parse_discovery_reply(discovery_reply("10.0.0.5", nested), "10.0.0.5")
    assert p.id == "conn-1" and p.mainboard_id == MAINBOARD and p.ip == "10.0.0.5"
    assert p.resolution == (11520, 5120) and p.protocol_version == "V3.0.0"


def test_broadcast_matches_ip_or_not_mask():
    assert discovery.broadcast_for("192.168.1.37", "255.255.255.0") == "192.168.1.255"
    assert discovery.broadcast_for("10.1.2.3", "255.255.0.0") == "10.1.255.255"
    assert discovery.broadcast_for("172.16.5.9", "255.255.255.252") == "172.16.5.11"


def test_topic_classification():
    assert protocol.classify_topic("sdcp/response/X") == "response"
    assert protocol.classify_topic("sdcp/status/X") == "status"
    assert protocol.classify_topic("sdcp/attributes/X") == "attributes"
    assert protocol.classify_topic("sdcp/notice/X") == "notice"
    assert protocol.classify_topic("/sdcp/status/X") is None


# ---- websocket framing ----------------------------------------------------------
@pytest.mark.parametrize("n", [0, 5, 125, 126, 65535, 65536])
def test_frame_roundtrip(n):
    payload = os.urandom(n)
    a, b = socket.socketpair()
    with a, b:
        a.sendall(ws.encode_frame(ws.OP_TEXT, payload, mask=True))
        fin, op, data = ws.read_frame(b)
    assert fin and op == ws.OP_TEXT and data == payload


def test_accept_key_rfc_example():
    assert ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="  # RFC 6455 §1.3


# ---- upload body ----------------------------------------------------------------
def test_chunk_ranges():
    assert upload.chunk_ranges(0) == []
    assert upload.chunk_ranges(1) == [(0, 1)]
    M = 1 << 20
    assert upload.chunk_ranges(2 * M + 5) == [(0, M), (M, M), (2 * M, 5)]


def test_md5_lowercase(tmp_path):
    f = tmp_path / "a.goo"; f.write_bytes(b"hello")
    assert upload.file_md5_hex(str(f)) == hashlib.md5(b"hello").hexdigest()


# ---- loopback integration ---------------------------------------------------------
@pytest.mark.parametrize("nested", [True, False])
def test_discover_loopback(nested):
    r = UdpResponder(nested); r.start()
    try:
        found = discovery.discover(0.5, targets=["127.0.0.1"], port=r.port)
    finally:
        r.sock.close()
    assert r.probes == [b"M99999"]
    assert len(found) == 1 and found[0].mainboard_id == MAINBOARD


def test_ws_request_response_loopback():
    srv = WsServer()
    try:
        with ws.SdcpWebSocket("127.0.0.1", MAINBOARD, "conn-1", port=srv.server_address[1]) as c:
            rid = c.request(Cmd.PAUSE)
            resp = c.wait_response(rid)
        assert srv.paths == ["/websocket"]
        assert resp["Data"]["RequestID"] == rid and resp["Data"]["Data"]["Ack"] == 0
        sent = srv.received[0]
        assert sent["Topic"] == f"sdcp/request/{MAINBOARD}" and sent["Data"]["Cmd"] == 129
        assert sent["Data"]["Data"] == {} and sent["Id"] == "conn-1"
        import time
        end = time.monotonic() + 2.0          # the server thread records the pong on its own time
        while not srv.pongs and time.monotonic() < end:
            time.sleep(0.01)
        assert srv.pongs == [b"hb"]
    finally:
        srv.shutdown(); srv.server_close()


def _parts_dict(chunk):
    return {name: payload for name, _fn, _ct, payload in chunk["parts"]}


def test_upload_loopback_chunks_and_fields(tmp_path):
    data = os.urandom((1 << 20) * 2 + 1234)
    f = tmp_path / "model.goo"; f.write_bytes(data)
    srv = UploadServer()
    try:
        prog = []
        uid = upload.upload("127.0.0.1", str(f), port=srv.server_address[1],
                            progress=lambda d, t: prog.append((d, t)))
    finally:
        srv.shutdown(); srv.server_close()
    assert len(srv.chunks) == 3 and all(c["path"] == "/uploadFile/upload" for c in srv.chunks)
    names = [p[0] for p in srv.chunks[0]["parts"]]
    assert names == ["TotalSize", "Uuid", "Offset", "Check", "S-File-MD5", "File"]
    md5 = hashlib.md5(data).hexdigest()
    rebuilt = b""
    for i, c in enumerate(srv.chunks):
        d = _parts_dict(c)
        assert d["TotalSize"] == str(len(data)).encode()
        assert d["Uuid"] == uid.encode() and len(uid) == 32
        assert d["Offset"] == str(i << 20).encode()
        assert d["Check"] == b"0" and d["S-File-MD5"] == md5.encode()
        file_part = c["parts"][-1]
        assert file_part[1] == "model.goo" and file_part[2] == "application/octet-stream"
        rebuilt += file_part[3]
    assert rebuilt == data
    assert prog[-1] == (len(data), len(data))


def test_upload_retries_then_succeeds(tmp_path):
    f = tmp_path / "s.goo"; f.write_bytes(b"x" * 10)
    srv = UploadServer(fail_first=2)
    try:
        upload.upload("127.0.0.1", str(f), port=srv.server_address[1])
    finally:
        srv.shutdown(); srv.server_close()
    assert len(srv.chunks) == 1


def test_upload_gives_up_after_three(tmp_path):
    f = tmp_path / "s.goo"; f.write_bytes(b"x" * 10)
    srv = UploadServer(fail_first=3)
    try:
        with pytest.raises(RuntimeError):
            upload.upload("127.0.0.1", str(f), port=srv.server_address[1])
    finally:
        srv.shutdown(); srv.server_close()
    assert srv.chunks == []


# ---- regression against real-printer capture (Saturn 4 Ultra 16K, V3.0.0) -------
CAPTURE = os.path.join(os.path.dirname(__file__), "..", "captures", "discovery_reply.json")


@pytest.mark.skipif(not os.path.exists(CAPTURE), reason="needs a local printer capture (captures/ is not published)")
def test_parse_real_capture():
    import json
    with open(CAPTURE, "rb") as f:
        raw = f.read()
    p = protocol.parse_discovery_reply(raw, "192.0.2.10")
    attrs = json.loads(raw)["Data"]
    assert p.machine_name == "Saturn 4 Ultra 16K" and p.protocol_version == "V3.0.0"
    assert p.mainboard_id == attrs["MainboardID"] and len(p.mainboard_id) == 16
    assert p.ip == attrs["MainboardIP"]


# ---- compatibility gate: pre-V3 (MQTT-era) printers are unsupported -------------
@pytest.mark.parametrize("ver,major,ok", [
    ("V3.0.0", 3, True), ("v3.1", 3, True), ("V4.0.0", 4, True),
    ("V1.0.0", 1, False), ("V2.5.1", 2, False), ("", None, False), ("garbage", None, False),
])
def test_protocol_version_gate(ver, major, ok):
    assert protocol.protocol_major(ver) == major
    pr = protocol.Printer(name="P", machine_name="Saturn 3 Ultra", protocol_version=ver)
    assert protocol.is_supported(pr) is ok
    msg = protocol.compatibility_warning(pr)
    assert (msg is None) is ok
    if not ok:
        assert "no longer compatible" in msg and "Saturn 3 Ultra" in msg


def test_discover_warns_for_old_printer():
    r = UdpResponder(version="V1.0.0"); r.start()
    try:
        with pytest.warns(UserWarning, match="no longer compatible"):
            found = discovery.discover(0.5, targets=["127.0.0.1"], port=r.port)
    finally:
        r.sock.close()
    assert len(found) == 1 and not protocol.is_supported(found[0])


def test_ensure_supported_rejects_old_and_accepts_v3():
    old = UdpResponder(version="V1.0.0"); old.start()
    new = UdpResponder(version="V3.0.0"); new.start()
    try:
        with pytest.raises(protocol.IncompatiblePrinterError, match="no longer compatible"):
            discovery.ensure_supported("127.0.0.1", 0.3, port=old.port)
        assert discovery.ensure_supported("127.0.0.1", 0.3, port=new.port).protocol_version == "V3.0.0"
    finally:
        old.sock.close(); new.sock.close()


def test_cli_refuses_old_printer(monkeypatch, capsys):
    from elegoo_lan import __main__ as cli
    def fake_ensure(ip, *a, **k):
        raise protocol.IncompatiblePrinterError(protocol.compatibility_warning(
            protocol.Printer(name="Old", machine_name="Saturn 3 Ultra", protocol_version="V1.0.0")))
    monkeypatch.setattr(cli.discovery, "ensure_supported", fake_ensure)
    rc = cli.main(["cmd", "10.0.0.9", "MB", "pause"])
    err = capsys.readouterr().err
    assert rc == 2 and "WARNING" in err and "no longer compatible" in err and "--force" in err


def test_ws_frame_in_same_segment_as_handshake():
    """A frame that arrives together with the 101 response must not be lost or rejected."""
    import socket
    import threading
    lst = socket.socket(); lst.bind(("127.0.0.1", 0)); lst.listen(1)

    def serve():
        c, _ = lst.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        key = [l.split(b":", 1)[1].strip().decode() for l in req.split(b"\r\n")
               if l.lower().startswith(b"sec-websocket-key")][0]
        body = json.dumps({"Topic": "sdcp/status/x", "Status": {"CurrentStatus": [0]}}).encode()
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Accept: " + ws.accept_key(key).encode() + b"\r\n\r\n"
                  + ws.encode_frame(ws.OP_TEXT, body, mask=False))
        c.recv(1024)
        c.close()
    t = threading.Thread(target=serve, daemon=True); t.start()
    try:
        c = ws.SdcpWebSocket("127.0.0.1", MAINBOARD, port=lst.getsockname()[1])
        assert c.buffered > 0
        assert c.recv_message()["Status"]["CurrentStatus"] == [0] and c.buffered == 0
        c.close()
    finally:
        lst.close()
