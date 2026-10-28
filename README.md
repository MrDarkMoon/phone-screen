# Phone Screen

Use your real Android phone inside VR on the **Steam Frame**. The headset tracks the phone you're holding, and its screen is drawn in VR exactly on top of it. Your apps, your touches and your phone's sound all work, with no PC needed: everything runs on the headset.

Phone Screen is an independent hobby project. It is not made by, or affiliated with, Valve or Google. It is experimental and works, but it isn't finished. Ideas and pull requests are very welcome. That's why it's here.

## How it works

The phone app shows a **tracking pattern** (two ArUco markers) on the real screen. Your apps run on a hidden **virtual display** (through [scrcpy](https://github.com/Genymobile/scrcpy)'s server), and that hidden screen is what you see in VR. Touches on the real screen go to it.

Several things work together to place the phone:

| Source | What it gives |
|---|---|
| Headset tracking cameras ([Frametop](https://github.com/DeeJanuz/frametop)'s `ft-camd`) | Marker corners. Every corner from every camera goes into one fit, each camera with the head pose at its own moment. |
| Phone motion sensors (game rotation vector, linear acceleration, about 200 Hz) | How the phone is turned, and its motion between camera sightings (Kalman filter). |
| Lit screen outline | Position when the markers are too small to read, with the turn taken from the sensors. |
| Phone selfie camera + face detector (YuNet) | The direction to your nose: tracking from the other side (still learning, see below). |

The phone and the headset match clocks over a ping/pong exchange, so every reading is placed at its true moment. The headset pose is sampled at 200 Hz and interpolated to each camera picture's time.

### It teaches itself

Most of the tuning is learned while you use it, and saved in `~/.config/phone-screen/learned.json`:

- **Each camera's delay.** When the phone's own sensors say it is lying still, the cameras must see it in one place however your head moves. If it seems to move with your head, the delay that keeps it still is learned.
- **How far ahead to draw.** The drawn screen is compared with where the cameras later saw the phone, until it no longer trails behind.
- **Where your face is.** The selfie camera's rays to your nose, taken from many places, meet at the nose. That also settles how big your face looks at each distance, and whether the camera picture is mirrored.
- **Smaller things:** heading, sighting noise, pattern brightness, and how much the screen's glow widens its outline in each camera.
- **Training mode (optional).** A see-through copy of the pattern hangs still in VR. Line the real phone up with it and tap the screen. Each tap is a sample, and their average becomes a fixed correction.

## What you need

- A Steam Frame running SteamOS with SteamVR.
- [Frametop](https://github.com/DeeJanuz/frametop)'s camera broker `ft-camd` (in `hands/`). **Also apply `frametop-camd-exact-buffers.patch`** (see below).
- A `dev` distrobox with `adb` (`android-tools`) and Java (JDK 17 or newer), to build the phone app.
- An Android phone with wireless debugging paired to the headset. Developed on a Pixel 9 Pro XL (Android 16). The screen size and selfie-camera position in `pattern.py` and `facetrack.py` are that phone's, so change them for yours.
- Optional: `clip-tts` on the PATH, for spoken status messages.

## Setup

```sh
./setup.sh                  # Python venv, scrcpy server, face model
android/sdk-setup.sh        # Android build tools (aapt2/zipalign through FEX on the ARM Frame)
distrobox enter dev -- android/build.sh    # builds android/build/PhoneScreen.apk
./install.sh                # background service: starts tracking when the phone app does
```

Install `PhoneScreen.apk` on the phone. Pair wireless debugging once (`adb pair`). With more than one device connected, set `PHONE_SERIAL`.

## Use

Open **Phone Screen** on the phone and press **Start tracking**. The service starts the camera feed and the tracker, and your phone's screen appears in VR on the phone. To stop, hold three fingers on the screen for 2 seconds. About 15 seconds later everything is switched off and the headset is back to normal.

To run it by hand instead: `venv/bin/python -u phone_screen.py` (add `PHONE_REC=1` to record `~/.local/state/phone-screen/track.csv` for analysis).

## The camera timing fix for ft-camd

`ft-camd` borrows the headset's camera buffers from XRService. After XRService restarts streaming, the buffer order is shuffled, and `ft-camd` had to guess which buffer held each new frame. For the two side (SLAM) cameras it often picked an older one, so frames were 0 to 250 ms older than their timestamps, at random. Anything tracked then jumped whenever the head moved.

The patch asks V4L2 (`VIDIOC_QUERYBUF`) which buffer each index holds, for every frame, so nothing is guessed. Static-phone tests went from 20 to 40 mm of wander during head movement to 4 to 5 mm. After rebuilding, `ft-camd` needs its capabilities again (`hands/run.sh caps`, or `sudo setcap cap_dac_read_search,cap_sys_ptrace,cap_perfmon=ep hands/build/ft-camd`).

## Files

| File | Job |
|---|---|
| `daemon.py`, `install.sh` | background service |
| `phone_screen.py` | main loop: overlay, fusion, everything together |
| `markers.py`, `solve.py`, `pattern.py` | marker and outline finding, joint fits |
| `fusion.py`, `sensors.py`, `headhist.py` | Kalman filter, phone sensors, head-pose history |
| `learn.py`, `train.py`, `facetrack.py` | self-learning, training mode, selfie-camera face tracking |
| `phonelink.py`, `android/` | the phone app and its link (adb reverse, port 8778) |
| `video.py`, `sharedtex.py`, `home.py` | scrcpy virtual display, sound, DMA-BUF overlay texture, VR home screen |

## Known problems and ideas

- Face tracking is learning, but its rays still scatter by about 3 cm. Better lens values, its own timing, and more face points should help.
- The marker pattern needs the screen fairly bright, and markers get hard to read beyond about 70 cm.
- The VR home screen is a static copy of the real one (`homecap.py`), because Android only runs the real launcher on the main display.
- The face detector costs a fair amount of CPU.
- Ideas: a printed marker on the headset for the selfie camera to track (`print/make_head_marker.py`), or the headset's IR illuminators.

## Credits

[scrcpy](https://github.com/Genymobile/scrcpy) (Apache 2.0) for the phone's screen, input and sound. [Frametop](https://github.com/DeeJanuz/frametop) (MIT) for the camera broker and calibration. [OpenCV](https://opencv.org) and its [YuNet](https://github.com/opencv/opencv_zoo) face model. [PyAV](https://github.com/PyAV-Org/PyAV) and [pyopenvr](https://github.com/cmbruns/pyopenvr).

## License

MIT
