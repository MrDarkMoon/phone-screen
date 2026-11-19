#!/bin/sh
# Makes a ready-built phone app for a GitHub Release, so people can install it without
# building it themselves.
#
#   ./release.sh 6            builds app version 6 into dist/: PhoneScreen-v6.apk and its sha256
#   ./release.sh 6 --publish  ...and publishes it as GitHub release v6 (asks first)
#
# Needs the 'dev' distrobox (Java, adb and the gh CLI logged in) and the Android build tools
# (android/sdk-setup.sh). The APK is signed with ~/.config/phone-screen/app-key.jks: keep that
# file safe, because every later release must use the same key to install over the old one.
set -e
cd "$(dirname "$0")"
V=$1
case "$V" in ''|*[!0-9]*) echo "usage: $0 VERSION_NUMBER [--publish]"; exit 1 ;; esac
export XDG_RUNTIME_DIR=/run/user/$(id -u)     # distrobox needs the real one (a desktop session may set its own)
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "note: there are uncommitted changes; the release would not match any commit"
fi
distrobox enter dev -- env VERSION="$V" VERSION_NAME="1.$V" "$PWD/android/build.sh"
mkdir -p dist
APK=dist/PhoneScreen-v$V.apk
cp android/build/PhoneScreen.apk "$APK"
(cd dist && sha256sum "PhoneScreen-v$V.apk" > "PhoneScreen-v$V.apk.sha256")
echo "made $APK"
cat dist/PhoneScreen-v$V.apk.sha256
NOTES="Phone Screen's phone app, version $V. Install it on the phone (allow installs from your file manager or browser), then see the README for the headset side."
if [ "$2" = --publish ]; then
    if [ -z "$(git branch -r --contains HEAD)" ]; then
        echo "push this commit to GitHub first (the release points at it)"; exit 1
    fi
    printf 'Publish GitHub release v%s with %s? [y/N] ' "$V" "$APK"
    read ok
    [ "$ok" = y ] || { echo "not published"; exit 0; }
    distrobox enter dev -- gh release create "v$V" "$PWD/$APK" "$PWD/$APK.sha256" \
        --title "Phone Screen v$V" --notes "$NOTES" --target "$(git rev-parse HEAD)"
else
    echo "to publish it:  ./release.sh $V --publish"
fi
