#!/bin/sh
# The Android build tools for android/build.sh, on the Steam Frame (ARM, aarch64).
# Java comes from the 'dev' distrobox (e.g. Fedora: dnf install java-25-openjdk-devel).
# Google's aapt2 and zipalign are x86-64 only, so they run through Steam's FEX emulator
# with a tiny x86 root made of SteamLinuxRuntime_4's glibc.
set -e
SDK=$HOME/Android/sdk
STEAM=$HOME/.steam/steam/steamapps/common
mkdir -p "$SDK" "$HOME/Android/bin"
if [ ! -x "$SDK/cmdline-tools/latest/bin/sdkmanager" ]; then
    tmp=$(mktemp -d)
    curl -sSLf -o "$tmp/c.zip" https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip
    unzip -q "$tmp/c.zip" -d "$tmp"
    mkdir -p "$SDK/cmdline-tools" && mv "$tmp/cmdline-tools" "$SDK/cmdline-tools/latest"
    rm -rf "$tmp"
fi
distrobox enter dev -- sh -c "yes | $SDK/cmdline-tools/latest/bin/sdkmanager --sdk_root=$SDK --licenses >/dev/null;
    $SDK/cmdline-tools/latest/bin/sdkmanager --sdk_root=$SDK 'platforms;android-36' 'build-tools;36.0.0'"
# a minimal x86-64 root for FEX
R=$(ls -d "$STEAM"/SteamLinuxRuntime_4/steamrt4_platform_*/files/lib/x86_64-linux-gnu | tail -1)
X=$HOME/Android/x86root
mkdir -p "$X/lib64" "$X/lib/x86_64-linux-gnu" "$X/etc"
for l in ld-linux-x86-64.so.2 libc.so.6 libdl.so.2 libpthread.so.0 libm.so.6 librt.so.1 libgcc_s.so.1; do
    cp -L "$R/$l" "$X/lib/x86_64-linux-gnu/"
done
cp "$X/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2" "$X/lib64/"
for t in aapt2 zipalign; do
    cat > "$HOME/Android/bin/$t" <<W
#!/bin/sh
B=\$HOME/Android/sdk/build-tools/36.0.0
FEX_ROOTFS=\$HOME/Android/x86root LD_LIBRARY_PATH=/lib/x86_64-linux-gnu:\$B/lib64 exec $STEAM/FEX-Emu/usr/bin/FEX \$B/$t "\$@"
W
    chmod +x "$HOME/Android/bin/$t"
done
"$HOME/Android/bin/aapt2" version
