#!/bin/sh
# Builds Phone Screen's phone app: android/build/PhoneScreen.apk. Runs in the dev distrobox
# (Java is there); no gradle, just the SDK's tools. aapt2 and zipalign run through FEX.
set -e
cd "$(dirname "$0")"
SDK=$HOME/Android/sdk
B=$SDK/build-tools/36.0.0
J=$SDK/platforms/android-36/android.jar
KEY=$HOME/.config/phone-screen/app-key.jks     # the same key every build, so updates install over
rm -rf build && mkdir -p build/classes build/res
~/Android/bin/aapt2 compile --dir res -o build/res.zip
~/Android/bin/aapt2 link -o build/unsigned.apk --manifest AndroidManifest.xml -I $J build/res.zip \
    --min-sdk-version 33 --target-sdk-version 36 --version-code ${VERSION:-1} --version-name 1.0
javac -Xlint:-options --release 17 -cp $J -d build/classes $(find src -name '*.java')
$B/d8 --release --min-api 33 --lib $J --output build $(find build/classes -name '*.class')
(cd build && zip -qj unsigned.apk classes.dex)
~/Android/bin/zipalign -f 4 build/unsigned.apk build/aligned.apk
if [ ! -f $KEY ]; then
    mkdir -p "$(dirname $KEY)"
    keytool -genkeypair -keystore $KEY -storepass phonescreen -keypass phonescreen -alias app \
        -keyalg RSA -keysize 2048 -dname CN=PhoneScreen -validity 20000
fi
$B/apksigner sign --ks $KEY --ks-pass pass:phonescreen --out build/PhoneScreen.apk build/aligned.apk 2>/dev/null
$B/apksigner verify build/PhoneScreen.apk 2>/dev/null
ls -l build/PhoneScreen.apk
