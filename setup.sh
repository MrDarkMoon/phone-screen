#!/bin/sh
# One-time setup on the Steam Frame: Python packages, scrcpy's server, the face model.
# Needs: a 'dev' distrobox with adb (android-tools), and Frametop's ft-camd (see README).
set -e
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q -r requirements.txt
# pyopenvr looks for its library by this name; SteamOS ships it as libopenvr_api.so
lib=$(venv/bin/python -c 'import openvr, os; print(os.path.dirname(openvr.__file__))')
ln -sf /usr/lib/libopenvr_api.so "$lib/libopenvr_api_64.so"
[ -f scrcpy-server-v4.1 ] || curl -sSLf -o scrcpy-server-v4.1 \
    https://github.com/Genymobile/scrcpy/releases/download/v4.1/scrcpy-server-v4.1
mkdir -p models
[ -f models/face_detection_yunet_2023mar.onnx ] || curl -sSLf -o models/face_detection_yunet_2023mar.onnx \
    https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
echo "set up. Build the phone app with android/build.sh, then ./install.sh for the background service."
