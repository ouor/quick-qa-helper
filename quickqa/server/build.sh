#!/usr/bin/env bash
# Builds qqserver.jar (dex) from QQServer.java. Needs a JDK and $ANDROID_HOME with a platform + build-tools.
set -euo pipefail
cd "$(dirname "$0")"
PLATFORM=$(ls -d "$ANDROID_HOME"/platforms/android-* | sort -V | tail -1)
BT=$(ls -d "$ANDROID_HOME"/build-tools/* | sort -V | tail -1)
rm -rf build && mkdir -p build/classes
javac --release 11 -nowarn -cp "$PLATFORM/android.jar" -d build/classes QQServer.java
if [[ -f "$BT/d8.bat" ]]; then
  cmd //c "$(cygpath -w "$BT/d8.bat")" --min-api 26 --output qqserver.jar build/classes/*.class
else
  "$BT/d8" --min-api 26 --output qqserver.jar build/classes/*.class
fi
rm -rf build
echo "built $(pwd)/qqserver.jar"
