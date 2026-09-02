#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build and install "Private Local AI.app".
#
# Produces a real, self-contained .app: the Python server is copied inside the
# bundle and its data lives in ~/Library/Application Support, so the installed
# app keeps working even if this source folder is moved or deleted.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }

APP_NAME="Private Local AI"
BUNDLE_ID="local.privateai.app"
VERSION="1.1.0"
DEST="${1:-/Applications}"
APP="$DEST/$APP_NAME.app"
BUILD="$ROOT/mac/build"
SUPPORT="$HOME/Library/Application Support/$APP_NAME"

step "1/6  Compiling the Swift app"
rm -rf "$BUILD"
mkdir -p "$BUILD"
swiftc -O \
  -target arm64-apple-macosx13.0 \
  -framework AppKit -framework WebKit -framework Carbon \
  -o "$BUILD/PrivateLocalAI" \
  mac/Sources/*.swift
ok "compiled $(du -h "$BUILD/PrivateLocalAI" | cut -f1) binary"

step "2/6  Building the icon"
if [ ! -f mac/AppIcon.icns ]; then
  ./.venv/bin/python mac/make_icon.py mac/AppIcon.iconset >/dev/null
  iconutil -c icns mac/AppIcon.iconset -o mac/AppIcon.icns
fi
ok "AppIcon.icns"

step "3/6  Assembling the bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/server"
cp "$BUILD/PrivateLocalAI" "$APP/Contents/MacOS/"
cp mac/AppIcon.icns "$APP/Contents/Resources/"

# the server itself
cp -R app "$APP/Contents/Resources/server/app"
# Stamp this build so a relaunch can tell a freshly installed server from a
# stale process still holding the port.
date +%Y%m%d-%H%M%S > "$APP/Contents/Resources/server/BUILD_ID"
cp requirements.txt "$APP/Contents/Resources/server/"
cp -R sample_docs "$APP/Contents/Resources/server/sample_docs"
find "$APP/Contents/Resources/server" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>PrivateLocalAI</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>Private Local AI transcribes your speech on this Mac using whisper.cpp. Audio is never sent anywhere.</string>
  <key>NSHumanReadableCopyright</key><string>Runs entirely on this Mac.</string>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist" >/dev/null
ok "bundle laid out at $APP"

step "4/6  Preparing the runtime environment"
mkdir -p "$SUPPORT"
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    v="$("$cand" -c 'import sys;print("%d%02d"%sys.version_info[:2])' 2>/dev/null || echo 0)"
    [ "$v" -ge 310 ] 2>/dev/null && { PY="$cand"; break; }
  fi
done
if [ -z "$PY" ]; then
  warn "No Python 3.10+ found — the app will try to set this up on first launch."
else
  if [ ! -x "$SUPPORT/venv/bin/python" ]; then
    "$PY" -m venv "$SUPPORT/venv"
    "$SUPPORT/venv/bin/python" -m pip install --quiet --upgrade pip
  fi
  "$SUPPORT/venv/bin/pip" install --quiet --only-binary=:all: -r requirements.txt
  ok "runtime ready at ~/Library/Application Support/$APP_NAME/venv"
fi

# Speech model: the installed app reads it from its own data folder, so give
# it a copy rather than leaving it pointing at this source tree.
WHISPER_DEST="$SUPPORT/local_data/models/whisper/ggml-base.en.bin"
WHISPER_SRC="$ROOT/local_data/models/whisper/ggml-base.en.bin"
mkdir -p "$(dirname "$WHISPER_DEST")"
if [ -s "$WHISPER_DEST" ]; then
  ok "speech model already installed"
elif [ -s "$WHISPER_SRC" ]; then
  cp "$WHISPER_SRC" "$WHISPER_DEST"
  ok "speech model copied ($(du -h "$WHISPER_DEST" | cut -f1))"
else
  warn "speech model not found — voice input will be unavailable."
  warn "run ./setup.sh first, then re-run this script."
fi

# Neural voice for the J.A.R.V.I.S. persona.
PIPER_DEST="$SUPPORT/local_data/models/piper"
mkdir -p "$PIPER_DEST"
if [ -s "$PIPER_DEST/en_GB-alan-medium.onnx" ]; then
  ok "neural voice already installed"
elif [ -s "$ROOT/local_data/models/piper/en_GB-alan-medium.onnx" ]; then
  cp "$ROOT/local_data/models/piper/"en_GB-alan-medium.onnx* "$PIPER_DEST/"
  ok "neural voice copied (en_GB-alan-medium)"
else
  warn "neural voice not found — falling back to macOS 'say'."
fi

# Optional voice cloning, installed into the app's own data folder. A venv
# cannot be relocated, so it is built in place rather than copied.
if [ -x "$SUPPORT/local_data/voiceclone-venv/bin/python" ]; then
  ok "voice cloning already installed"
elif [ "${PRIVATE_AI_WITH_VOICE_CLONING:-}" = "1" ]; then
  say "  installing voice cloning (~1.5 GB)…"
  PRIVATE_AI_DATA_DIR="$SUPPORT/local_data" ./scripts/install_voice_cloning.sh >/dev/null 2>&1 \
    && ok "voice cloning installed" || warn "voice cloning install failed"
else
  say "  ${DIM}voice cloning not installed — run with PRIVATE_AI_WITH_VOICE_CLONING=1 to add it${RESET}"
fi

step "5/6  Signing"
# Ad-hoc signature: enough for local use, and it gives the bundle a stable
# identity so macOS remembers the microphone permission you grant it.
codesign --force --deep --sign - \
  --identifier "$BUNDLE_ID" \
  --options runtime "$APP" 2>/dev/null \
  || codesign --force --deep --sign - --identifier "$BUNDLE_ID" "$APP"
codesign --verify --deep "$APP" && ok "ad-hoc signed"

step "6/6  Registering with Launch Services"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" 2>/dev/null || true
touch "$APP"
ok "registered"

printf '\n%s%sInstalled.%s\n\n' "$GREEN" "$BOLD" "$RESET"
printf '  %s\n' "$APP"
printf '  %sLaunch it from Spotlight, or:%s  open -a \"%s\"\n' "$DIM" "$RESET" "$APP_NAME"
printf '  %sSummon from anywhere:%s          ⌥Space\n' "$DIM" "$RESET"
printf '  %sData:%s                          %s\n\n' "$DIM" "$RESET" "$SUPPORT"
