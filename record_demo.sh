#!/bin/sh
# Records what you really see in the headset (SteamVR's own copy of one eye: passthrough,
# the phone's screen, everything), then makes a GIF of it for the README.
#
#   ./record_demo.sh [SECONDS] [NAME]      default 15 seconds, name "demo"
#
# Writes docs/NAME.mp4 (the recording) and docs/NAME.gif. The headset must be worn: its
# view is black while the display is off. Needs ffmpeg with libx264.
set -e
cd "$(dirname "$0")"
SECS=${1:-15}
NAME=${2:-demo}
DEV=/dev/video99            # SteamVR's v4l2cam: one eye, the full picture, 1920x1080
mkdir -p docs
MP4=docs/$NAME.mp4
GIF=docs/$NAME.gif
say() { clip-tts say "$1" >/dev/null 2>&1 || true; }
RAW=/dev/shm/phone-screen-demo.nut     # raw frames in memory: ~190 MB a second
trap 'rm -f "$RAW"' EXIT
[ "$SECS" -le 25 ] || { echo "25 seconds at most (the raw recording is held in memory)"; exit 1; }
say "Recording for $SECS seconds."
# Raw, at low priority: encoding while recording took 1.5 CPU cores and the tracker lost
# the phone. The first frames after opening are black, so half a second is skipped.
nice -n 19 ffmpeg -loglevel error -y -f v4l2 -framerate 30 -i "$DEV" -ss 0.5 -t "$SECS" -c:v rawvideo -f nut "$RAW"
say "Recording done."
nice -n 19 ffmpeg -loglevel error -y -i "$RAW" -vf scale=1280:-2 -c:v libx264 -preset medium -crf 22 -pix_fmt yuv420p \
    -movflags +faststart "$MP4"
rm -f "$RAW"
# GIF: 10 frames a second, 560 wide, the passthrough's grain smoothed (it makes GIFs huge),
# its own palette. About 0.5 MB a second: keep it under ~18 s for GitHub's 10 MB limit.
ffmpeg -loglevel error -y -i "$MP4" -vf \
    "fps=10,scale=560:-2:flags=lanczos,hqdn3d=4:3:6:4,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
    "$GIF"
ls -l "$MP4" "$GIF"
