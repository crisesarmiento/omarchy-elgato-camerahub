# Camera Hub 2.3.0 (7295) — what the plugin is based on

Source artifact: `CameraHub_2.3.0.7295.pkg` (xar, macOS installer).
App: `Camera Hub.app`, `CFBundleIdentifier` `com.elgato.CameraHub`,
`CFBundleShortVersionString` `2.3.0`, `CFBundleVersion` `7295`.
Qt 6.9.3. Copyright in the bundle: Corsair Memory, Inc.

This note records what was read out of that package. It is not an Elgato
document, and this repo does not ship their binary, firmware, LUTs, or UI assets.

REA opened the universal Mach-O
(`sha256 89ae4bc9c30c506890484d493e0e5b86d631d0c6f46ef6197020c9d8538c40f9`,
selected architecture `x86_64`). No decompiler was available on this machine
(Ghidra, Hopper, IDA, and Kuna are not configured), so the control map below
comes from the symbol table, debug strings, and a disassembly of the x86_64
slice. Where a byte layout was not recovered, it is marked unknown. This
plugin does not send firmware, and it does not write vendor HID or extension-unit
reports.

## Devices

Vendor id `4057` (`0x0fd9`) from `ElgatoDeviceCapabilities.json` in the app.
Product ids below are the ones that file names. USB 2.0 fallback ids are a
second product id, not a different product.

| Product | PID | USB 2.0 PID | Codename in the binary | Talks over |
|---|---:|---:|---|---|
| Facecam | 120 | — | Penna | HID + UVC extension unit |
| Facecam Pro | 121 | — | Cyclops | HID + UVC extension unit |
| Facecam Neo | 129 | — | — | same family as the other Facecams |
| Facecam 4K | 146 | 145 | Capra | same family |
| Facecam MK.2 | 148 | 147 | Tipo | same family |
| Facecam MK.2 Rev.2 | 215 | 214 | Tipo2 | same family |
| Cam Link | 97 | 98 | — | Realtek UVC extension (libRTICE) |
| Cam Link 4K | 102 | 103 | — | Realtek UVC extension |
| Cam Link 4K MK.2 | 123 | 133 | — | Realtek UVC extension |
| Cam Link 4K Rev.3 | 161 | 162 | — | Realtek UVC extension, HID interface 7 |
| Game Capture 4K X | 156 | 157 | — | capture card, not a webcam |
| Game Capture 4K S | 175 | 174 | — | capture card, HID interface 7 |

Game Capture 4K X also has product id 155, labeled USB 3.2 in that file.
Corsair Voyager internal cameras (vendor `1266`) are listed there and ignored here.

## What Camera Hub actually controls

Observed property and group names in the app binary:

- Exposure: auto, shutter, ISO, metering, compensation, exposure priority.
- White balance: temperature, tint (Pro), auto.
- Focus: auto, metering, touch-to-focus (Pro), presets.
- Frame: zoom, pan, tilt, digital window (Pro).
- Processing: noise reduction, sharpness, anti-flicker, HDR on some models.
- Device: LED mode and blink (Facecam), LED brightness (Pro), USB transfer mode.
- Software only, applied in Camera Hub rather than stored as a webcam control:
  LUT looks (Film Noir, Vivid Boost, Hollywood, True Tone, Coffee Break),
  background replacement, blur, virtual camera.

Firmware update symbols exist (`DoFirmwareUpdate`, bootloader mode). This
plugin does not implement them.

## Facecam image controls are a UVC extension unit

`CFacecamSupport::SetExposureTime` logs
`XU: Write XU_SELECTOR::EXPOSURE_TIME` and says the unit is 50 microseconds.
`CFacecamProSupport::SetExposureTime` logs the same selector name and says
100 microseconds. Both go through a `WriteUVC` call, not a raw HID report.

Selectors recovered from those calls (the value in `ecx` next to the log string):

| Selector name in the binary | Value | Payload |
|---|---:|---|
| `EXPOSURE_METERING` | 8 | 1 byte |
| `NOISE_REDUCTION` | 18 | 1 byte |
| `SERVICE_MODE` | 19 | 1 byte |
| `EXPOSURE_TIME` | 20 | 4-byte int |

The same call sites pass `0x1060000` as another argument. That value is the
same for every one of those writes. It was not recovered as a USB unit id.
Do not treat it as one.

Standard UVC names also appear next to the webcam support class:
`autoExposure`, `exposure`, `autoFocus`, `zoom`, `panTilt`, `viewRegion`,
`backlightCompensation`, `gain`, `antiFlicker`, `hue`, `saturation`,
`sharpness`, `whiteBalance`, `autoWhiteBalance`. On Linux those are the
controls `uvcvideo` usually exposes. The plugin drives those, and only those,
through `v4l2-ctl`.

## Facecam HID is a second channel

`CFacecamSupport` reads and writes HID reports (`ReadHIDWithRetries`,
`WriteHIDAndWaitForResponse`). A failed read logs
`Check the HID spec for updates`. Report ids seen as the tag argument of
read calls:

| Call | Tag byte |
|---|---:|
| `ReadLEDStatus` | `0xA2` |
| `ReadUserProfileFromDevice` | `0xB2` |
| `GetTransferMode` | `0xB7` |
| `GetExposurePriority` | `0xB9` |
| `GetMaxProfilesSupported` | `0xBA` |
| `GetVideoModes` | `0xBC` |
| `GetDMABufferState` | `0xC3` |
| `ReadISPValue` | `0xDD` |

The report body layout is unknown. LED mode, profile save, and USB transfer
mode go through this channel on the original Facecam. They are shown in the
panel as not available until that layout is confirmed against a plugged-in camera.

## Capture cards

`libRTICE_SDK_x64.dylib` and `libRTK_IO_x64.dylib` are Realtek IO libraries.
`CUVCExtensionPort` and `QextUVCPort` are the transport. AT commands in that
SDK cover HDMI, EDID, HDR-to-SDR, and USB speed. That is the Cam Link / Game
Capture path. This plugin detects those product ids and does not send AT commands.

## iPhone over Wi-Fi

Camera Hub browses Bonjour type `_egstreamwrap._tcp`. That is the EpocCam /
Camera Hub phone app, not a USB device. Elgato's own note lists the same
string. A live phone on this network advertised `EpocCam Pro on iPhone` with
TXT keys `SWr`, `I`, `D`, `V`, and `P`, on TCP port 9372.

Connecting to that port gets a binary `Helo` frame (fourcc little-endian).
The rest of StreamWrap (`SWMessage`, property requests) was not decoded, so
the plugin lists the phone and does not open the stream.

`_rdlink._tcp` also shows up on Apple devices. That is Apple's own service,
not Camera Hub. The plugin ignores it.

## Local app API

The app registers the URL scheme `camerahub://` and a loopback HTTP address
`http://127.0.0.1:%1/`. That server is how the macOS app is driven from outside.
It does not exist on Linux, so the plugin talks to the camera node instead.
