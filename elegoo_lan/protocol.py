"""SDCP message construction and parsing (clean-room, from 02-lan-sdcp-spec.md)."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

DISCOVERY_PROBE = b"M99999"
DISCOVERY_PORT = 3000
WS_PORT = 3030
WS_PATH = "/websocket"
UPLOAD_PATH = "/uploadFile/upload"
UPLOAD_CHUNK = 1 << 20  # 1 MiB
UPLOAD_RETRIES = 3


class Cmd:
    STATUS = 0
    ATTRIBUTES = 1
    START_PRINT = 128
    PAUSE = 129
    STOP = 130
    RESUME = 131
    RENAME = 192
    CANCEL_TRANSFER = 255
    FILE_LIST = 258
    DELETE_FILES = 259
    HISTORY_LIST = 320
    HISTORY_DETAIL = 321
    VIDEO_STREAM = 386
    TIMELAPSE = 387


def build_payload(cmd: int, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Per-command inner `Data` object (spec §3). Unknown commands yield {}."""
    a = args or {}
    if cmd == Cmd.START_PRINT:
        return {"Filename": str(a.get("Filename", "")), "StartLayer": int(a.get("StartLayer", 0))}
    if cmd == Cmd.RENAME:
        return {"Name": str(a.get("Name", ""))}
    if cmd == Cmd.CANCEL_TRANSFER:
        return {"Uuid": str(a.get("Uuid", "")), "FileName": str(a.get("FileName", ""))}
    if cmd == Cmd.FILE_LIST:
        return {"Url": str(a.get("Url", ""))}
    if cmd == Cmd.DELETE_FILES:
        return {"FileList": list(a.get("FileList", [])), "FolderList": list(a.get("FolderList", []))}
    if cmd == Cmd.HISTORY_DETAIL:
        return {"Id": list(a.get("Id", []))}
    if cmd in (Cmd.VIDEO_STREAM, Cmd.TIMELAPSE):
        return {"Enable": int(a.get("Enable", 0))}
    return {}


def new_request_id() -> str:
    return uuid.uuid4().hex  # 32 lowercase hex chars, no dashes/braces


def build_ws_request(cmd: int, mainboard_id: str, conn_id: str, args: dict | None = None,
                     *, request_id: str | None = None, now: float | None = None) -> dict:
    """WebSocket (v3-style) envelope, spec §2a."""
    return {
        "Id": conn_id,
        "Data": {
            "Cmd": int(cmd),
            "Data": build_payload(cmd, args),
            "RequestID": request_id or new_request_id(),
            "MainboardID": mainboard_id,
            "TimeStamp": int(time.time() if now is None else now),
            "From": 0,
        },
        "Topic": f"sdcp/request/{mainboard_id}",
    }


def encode(msg: dict) -> str:
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False)


@dataclass
class Printer:
    id: str = ""
    name: str = ""
    machine_name: str = ""
    protocol_version: str = ""
    firmware_version: str = ""
    resolution: tuple[int, int] | None = None
    ip: str = ""
    mainboard_id: str = ""
    sdcp_status: int = 0
    local_sdcp_address: str = ""
    sdcp_address: str = ""
    status: dict = field(default_factory=dict)
    source_ip: str = ""


MIN_PROTOCOL_MAJOR = 3
INCOMPATIBLE_WARNING = (
    "{name} ({model}) reports SDCP ProtocolVersion {ver!r}. Older ELEGOO printers that use "
    "the MQTT-based SDCP v1/v2 transport (e.g. Saturn 3 Ultra, Mars 4 Ultra) are no longer "
    "compatible with this client; only SDCP V3.x over WebSocket is supported.")


def protocol_major(version: str) -> int | None:
    """'V3.0.0' -> 3. Returns None if unparseable."""
    v = (version or "").strip().lstrip("vV")
    head = v.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def is_supported(printer: "Printer") -> bool:
    major = protocol_major(printer.protocol_version)
    return major is not None and major >= MIN_PROTOCOL_MAJOR


def compatibility_warning(printer: "Printer") -> str | None:
    if is_supported(printer):
        return None
    return INCOMPATIBLE_WARNING.format(name=printer.name or printer.ip or "printer",
                                       model=printer.machine_name or "unknown model",
                                       ver=printer.protocol_version or "unknown")


class IncompatiblePrinterError(RuntimeError):
    pass


def parse_discovery_reply(payload: bytes, source_ip: str = "") -> Printer | None:
    """Parse a UDP reply (spec §1). Ignores our own probe echo and non-JSON.

    Accepts both the nested `Data.Attributes` layout the app reads and a flat
    `Data{...}` layout (tolerance is a clean-room design choice).
    """
    if payload == DISCOVERY_PROBE:
        return None
    try:
        doc = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or "Id" not in doc:
        return None
    data = doc.get("Data") or {}
    attrs = data.get("Attributes") if isinstance(data.get("Attributes"), dict) else data
    res = None
    r = str(attrs.get("Resolution", "") or "")
    if "x" in r:
        w, _, h = r.partition("x")
        try:
            res = (int(w), int(h))
        except ValueError:
            res = None
    return Printer(
        id=str(doc.get("Id", "")),
        name=str(attrs.get("Name", "")),
        machine_name=str(attrs.get("MachineName", "")),
        protocol_version=str(attrs.get("ProtocolVersion", "")),
        firmware_version=str(attrs.get("FirmwareVersion", "")),
        resolution=res,
        ip=str(attrs.get("MainboardIP", "") or source_ip),
        mainboard_id=str(attrs.get("MainboardID", "")),
        sdcp_status=int(attrs.get("SDCPStatus", 0) or 0),
        local_sdcp_address=str(attrs.get("LocalSDCPAddress", "")),
        sdcp_address=str(attrs.get("SDCPAddress", "")),
        status=data.get("Status") if isinstance(data.get("Status"), dict) else {},
        source_ip=source_ip,
    )


def classify_topic(topic: str) -> str | None:
    """Route an incoming message by Topic prefix (spec §2a)."""
    for kind in ("response", "status", "attributes", "error", "notice"):
        if topic.startswith(f"sdcp/{kind}/"):
            return kind
    return None
