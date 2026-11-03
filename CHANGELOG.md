# Changelog

## 0.2.0 - 2026-11-03

- iPhone (EpocCam over Wi-Fi) works as a camera at `/dev/video9`, tested in
  Chrome and Google Meet. The device is YUYV 640x480 at 25 fps.
- Live mini preview in the panel, and a big preview window on demand.
- Mirror, flip, brightness, contrast and saturation for the iPhone feed.
- Looks (Noir, Vivid, Warm, Cool, Sepia) and custom `.cube` LUTs.
- Three preset slots for the iPhone and USB webcams.
- Snapshot to `~/Pictures/CameraHub/`.
- Fixed the video stalling after the first frame (missing timestamps).
- The panel is tall enough to show every control without scrolling.

## 0.1.0

- Detection of Elgato USB cameras and V4L2 controls through `v4l2-ctl`.
- iPhone discovery and login over Wi-Fi.
