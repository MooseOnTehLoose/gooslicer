# ELEGOO SatelLite 1.0.2.43 (win-x64): Stage 1 static inventory

All findings are **static**: nothing has been executed. Each one is tagged
[OBSERVED] (seen directly), [INFERRED] (deduced) or [UNKNOWN].

## Target identity
- Installer: `ELEGOO_Satellite_1.0.2.43_win-x64.exe`, SHA-256 `5c9c6d8e89c1bf5587c3af7a50e4ab2ab9e005faf9677915da19a984084bb8ce`. [OBSERVED]
- Installer type: Inno Setup 5.5.0.1 (unicode), product "ELEGOO SatelLite". Unpacked with innoextract 1.9 into 19,048 files (1.8 GB under `app/`). [OBSERVED]
- Main exe: `app/ELEGOO SatelLite.exe`, 1.2 MiB, SHA-256 `f398d682…d9638`. [OBSERVED]

## Architecture
- **The main exe is a thin launcher.** Its import table names only Qt5Core/Gui/Widgets/OpenGL plus `EleStart`, `EleFramework`, `EleGui`, `EleCustomUI`, `EleOutputGui`, `EleResources`, `EleRegister` and `EleGeoKernelSystem` (from `objdump -p`). [OBSERVED]
- **The app is built on Voxeldance Tango.** It is an OEM build of Voxeldance's platform: `AI/README.txt` says "Voxeldance Tango AI", `EleMqtt.dll` exports a `VoxMqtt` class, `EleNetManager` has `VoxNetManager::SDCPEntity`, and `EleLink.dll` calls voxeldance.net/.tech/.top/.cc licensing endpoints. [OBSERVED]
- **There are 65 proprietary `Ele*.dll` modules.** By name, they cover mesh work (EleTri\*), solid/BRep shapes (EleShape\*), slicing and image output (EleImageApp, EleLyHatch), output (EleOutput\*), networking (EleNetManager, EleMqtt, EleHttpConnect, EleLink), cloud (EleCloudDatabase), and AI (EleAIAgent, EleOrient). [OBSERVED names / INFERRED roles]
- **Third-party components:**
  - UI: Qt 5 (Widgets, QML/Quick, WebEngine, WebSockets, Sql), with QtZeroConf for mDNS.
  - Geometry and imaging: OpenCascade (TK\*.dll), OpenVDB 10.0.2, Embree3, assimp, OpenCV 4.5.3, Skia, TBB.
  - AI: onnxruntime, plus an embedded Python 3.7 under `AI/python`.
  - Networking: Paho MQTT C/C++ 1.6.0 and boost.asio mqtt_cpp; a bundled Mosquitto broker; OpenSSL.
  - Other: Agora RTC/RTM (video streaming), sqlite3, ffmpeg (sw\*, video_dec), yaml-cpp, zstd, zlib, snappy. [OBSERVED]
- **Copies of Windows system DLLs ship in `app/`** (`user32`, `shell32`, `kernel32`, `ole32`, `oleaut32`, `comdlg32`). Their purpose is [UNKNOWN]. On Linux, ignore them.
- **Bundled installer:** `UVtools_win-x64_v4.4.3.msi`. UVtools is open source, and the app offers "Open in UVTools" (`slotOpenInUVTools`, EleImageApp). [OBSERVED]

## Data formats on disk
| File | Format | Status |
|---|---|---|
| `resources/machines/printer/officialPlatform.ini` | Qt `QSettings` INI. Floats are encoded as `@Variant(\0\0\0\x87 <BE float32>)` (QMetaType 0x87 = Float). One `[N]` section per printer: build volume, resolution, mirror, `UVtoolsFormat=goo`, `SupportNetWork`, plus default Resin\\* and Printer\\* exposure and lift parameters | [OBSERVED] |
| `resources/resin/material/*.data` (1,999 files, plus a `cn/` copy) | Plain JSON (`platitem`, `cost_estimation`, `cloudPlatExtraParam`, …) | [OBSERVED] |
| `resources/machines/ELEGOO_Jupiter_2.yml` | Printer firmware config (Z axis, print, resin detection params) in YAML with Chinese comments | [OBSERVED] |
| `resources/script/common/*.satsup` | Support templates as Qt `QDataStream` (UTF-16BE QString keys such as `support/scriptmanager/…`). Likely a serialized `QVariantMap` | [OBSERVED header / INFERRED container] |
| `environment/*.ini` | QSettings UI/env flags. `key.ini` holds `cache = elegoosatellite` | [OBSERVED] |
| `environment/env_standard.dat` | Opaque binary | [UNKNOWN] |
| Sliced output `.goo` | Written natively by EleImageApp. Its strings show a temp file, then file mapping, then a checksum appended, then commit ("Cannot append GOO checksum", "Cannot create GOO file mapping"). Projects autosave as `AUTOSAVE-%2.goo.3mf`. GOO is an ELEGOO-published format and UVtools has an open implementation of it | [OBSERVED strings / INFERRED flow] |

## Network protocols (EleNetManager.dll unless noted)
### A. SDCP: LAN printer control
- **UDP discovery.** The probe string is `M99999`. The keys parsed from replies are `Data`, `Attributes`, `MachineName`, `ProtocolVersion`, `FirmwareVersion`, `Resolution`, `MainboardID`, `SDCPStatus`. The `Network::VoxUDPSocket` class lives in EleLink. [OBSERVED strings]
  - The UDP port is [UNKNOWN] statically. The public SDCP spec uses 3000; this still needs confirming in code.
- **SDCP v3 over WebSocket.** The constant `3030` sits beside `http://%1:%2%3`. [OBSERVED]
  - This matches the public SDCP v3 spec (`ws://<ip>:3030/websocket`). [INFERRED]
- **SDCP v1 over MQTT, with the PC as broker.**
  - The app starts the bundled `resources/mosquitto/mosquitto.exe -c mosquitto.conf`. Process management uses Windows commands: `tasklist`, `taskkill /IM mosquitto.exe /F` and `cmd.exe`. [OBSERVED]
  - `myconfig.conf` contains `listener 4848` and `allow_anonymous true`. [OBSERVED]
  - Topics are `/sdcp/request/%1`, `/sdcp/response/%1`, `/sdcp/status/%1`, `/sdcp/attributes/%1`, `sdcp/error/` and `sdcp/notice/`, where `%1` is the MainboardID. [OBSERVED]
  - Inferred flow: the app replies to discovery with `LocalSDCPAddress`, and the printer (e.g. Saturn 3 Ultra) connects to the PC's broker. [INFERRED]
- **File transfer.**
  - Upload is a multipart POST to `http://%1:%2/uploadFile/upload`. Form fields: `TotalSize`, `Offset`, `Check`, `S-File-MD5`, and `File` (sent as `application/octet-stream`). [OBSERVED]
  - Status keys: `FileTransferInfo`, `DownloadOffset`, `CheckOffset`, `FileTotalSize`. [OBSERVED]

### B. HTTP REST printer API (newer models; Jupiter 2 is the likely target [INFERRED])
These are all `http://%1:%2/…` [OBSERVED]:
- `/api/files`, `/api/files/internal`, `/api/delete`, `/api/space`, `/api/history`
- `/api/upload/start` and `/api/upload/stop` (fields `filemd5`, `force`, `file_md5`, `file_size`, `chunk_size`, `file_name`)
- `/api/print/start`, `/api/print/pause`, `/api/print/stop`, `/api/print/quit`, `/api/print/finish`, `/api/print/force`, `/api/print/copy/stop`
- `/api/printer/name`, `/api/printer/profile`, `/api/printer/timelapse`, `/api/printer/timelapse/list`, `/api/printer/livecamera` (field `rtsp_url`)
- `/login`, `/logout`, `/status`, `/info`, `/image`
- `http://%1:80/video/%2`

The port value is [UNKNOWN].

### C. Elegoo cloud (WAN)
- REST: `/api/v1/device-management-server/...`, covering bind/unbind, list, name, pincode, online-status, report-data, event-data, local-file, `mqtt-link/mqtt-client` (which returns `mqttClientId`, `mqttUserName` and `mqttPassword`), oss, and agora-token. [OBSERVED]
- Cloud MQTT topics: `app/v1/%1/device/data`, `app/v1/%1/device/onoffline`, `app/v1/%1/event`, over `mqtt://`, `mqtts://`, or WebSocket at `/mqtt`. [OBSERVED]
- Base URLs: `https://satellite.elegoo.com` and `.com.cn`; images come from `iot-p.elegoo.com`. [OBSERVED]
- Remote video: Agora RTC (`wan-rtc://`). This has no Linux port path beyond Agora's own Linux SDK. [INFERRED]

### D. Licensing and telemetry (EleLink.dll)
- Voxeldance `vdpay` endpoints: signin, export count, deactivation, update modules. [OBSERVED]
- `http://ip-api.com/json/`, a geo-IP lookup. [OBSERVED]
- Feedback and attachment upload through `/api/tp/oss/*`. [OBSERVED]

## Windows API to Linux mapping (so far)
| Windows usage | Where | Linux equivalent |
|---|---|---|
| `CreateFileMapping`/`MapViewOfFile` for GOO output | EleImageApp strings | `open` + `ftruncate` + `mmap`, then `rename` for an atomic commit |
| `tasklist` / `taskkill` / `cmd.exe` to manage mosquitto | EleNetManager | `QProcess`/`posix_spawn` holding the child PID, or system `mosquitto` |
| IOCP (`CreateIoCompletionPort` …) | EleMqtt, via boost.asio | Handled by boost.asio's epoll backend; no code change needed |
| `%APPDATA%\ELEGOO SatelLite\` (Inno `userappdata`) | installer | `$XDG_CONFIG_HOME/elegoo-satellite/` and `$XDG_DATA_HOME/…` |
| Qt `QSettings` INI | profiles | Same API, so it is portable as-is |

## Things worth noting
- The bundled Mosquitto listens on 4848 with `allow_anonymous true`. While SatelLite runs, anyone on the LAN can publish printer commands. [OBSERVED config / INFERRED exposure]

## Open questions for stage 2 (decompilation)
1. The UDP discovery port, and the exact JSON envelope (`Cmd` codes, `RequestID`, `Topic`) of SDCP requests sent from EleNetManager.
2. The REST API port, and `/login` authentication.
3. The GOO writer in EleImageApp: header layout, layer RLE encoding, and checksum algorithm. This needs checking against UVtools' open implementation.
4. Which printer models use which route (SDCP-MQTT, SDCP-WS, or REST). The candidates are `slicedFileSuffixVersion`, `SupportNetWork` and model-name strings.
