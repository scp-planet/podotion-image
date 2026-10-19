#!/bin/sh
set -eu

BIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
SKILL_DIR=$(dirname "$BIN_DIR")
MANIFEST="$SKILL_DIR/runtime.json"
[ -f "$MANIFEST" ] || { echo "Podotion Image runtime manifest is missing." >&2; exit 1; }

json_value() {
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$MANIFEST" | head -n 1
}

VERSION=$(json_value version)
TAG=$(json_value tag)
REPOSITORY=$(json_value repository)
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$OS" in
  linux) ;;
  darwin) ;;
  mingw*|msys*|cygwin*) OS=windows ;;
  *) echo "Unsupported operating system: $OS" >&2; exit 1 ;;
esac
MACHINE=$(uname -m)
case "$MACHINE" in
  x86_64|amd64) ARCH=amd64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) echo "Unsupported architecture: $MACHINE" >&2; exit 1 ;;
esac
KEY="$OS-$ARCH"
ASSET=$(sed -n "s/.*\"$KEY\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$MANIFEST" | head -n 1)
[ -n "$ASSET" ] || { echo "No Podotion Image runtime is published for $KEY." >&2; exit 1; }

if [ -n "${CODEX_HOME:-}" ]; then
  case "$CODEX_HOME" in
    "~") CODEX_HOME_DIR="$HOME" ;;
    "~/"*) CODEX_HOME_DIR="$HOME/${CODEX_HOME#\~/}" ;;
    /*) CODEX_HOME_DIR="$CODEX_HOME" ;;
    *) echo "CODEX_HOME must be an absolute native path." >&2; exit 1 ;;
  esac
else
  CODEX_HOME_DIR="$HOME/.codex"
fi

CACHE_DIR="$CODEX_HOME_DIR/podotion-image/runtime/$VERSION/$KEY"
BINARY="$CACHE_DIR/podotion-image"
[ "$OS" = "windows" ] && BINARY="$BINARY.exe"
mkdir -p "$CACHE_DIR"

if [ ! -x "$BINARY" ]; then
  LOCK="$CACHE_DIR/.download.lock"
  OWN_LOCK=0
  ATTEMPT=0
  while [ "$ATTEMPT" -lt 120 ]; do
    [ -x "$BINARY" ] && break
    if mkdir "$LOCK" 2>/dev/null; then
      OWN_LOCK=1
      break
    fi
    if [ -x "$BINARY" ]; then break; fi
    sleep 0.5
    ATTEMPT=$((ATTEMPT + 1))
  done
  if [ ! -x "$BINARY" ]; then
    if [ "$OWN_LOCK" -ne 1 ]; then
      echo "Timed out waiting for another Podotion Image runtime download." >&2
      exit 1
    fi
    TEMP_BINARY="$CACHE_DIR/.$ASSET.download.$$"
    TEMP_HASH="$TEMP_BINARY.sha256"
    cleanup() {
      rm -f "$TEMP_BINARY" "$TEMP_HASH"
      if [ "$OWN_LOCK" -eq 1 ]; then rmdir "$LOCK" 2>/dev/null || true; fi
    }
    trap cleanup EXIT INT TERM
    BASE_URL=$(json_value release_base_url)
    if [ -z "$BASE_URL" ]; then
      BASE_URL="https://github.com/$REPOSITORY/releases/download/$TAG"
    fi
    ASSET_URL="$BASE_URL/$ASSET"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL "$ASSET_URL" -o "$TEMP_BINARY"
      curl -fsSL "$ASSET_URL.sha256" -o "$TEMP_HASH"
    elif command -v wget >/dev/null 2>&1; then
      wget -q "$ASSET_URL" -O "$TEMP_BINARY"
      wget -q "$ASSET_URL.sha256" -O "$TEMP_HASH"
    else
      echo "Podotion Image runtime download requires curl or wget." >&2
      exit 1
    fi
    EXPECTED=$(tr -d '\r' < "$TEMP_HASH" | grep -Eo '[0-9a-fA-F]{64}' | head -n 1 | tr '[:upper:]' '[:lower:]')
    [ -n "$EXPECTED" ] || { echo "The published SHA-256 file is invalid." >&2; exit 1; }
    if command -v sha256sum >/dev/null 2>&1; then
      ACTUAL=$(sha256sum "$TEMP_BINARY" | awk '{print $1}')
    elif command -v shasum >/dev/null 2>&1; then
      ACTUAL=$(shasum -a 256 "$TEMP_BINARY" | awk '{print $1}')
    elif command -v openssl >/dev/null 2>&1; then
      ACTUAL=$(openssl dgst -sha256 "$TEMP_BINARY" | awk '{print $NF}')
    else
      echo "No SHA-256 utility is available." >&2
      exit 1
    fi
    ACTUAL=$(printf '%s' "$ACTUAL" | tr '[:upper:]' '[:lower:]')
    [ "$EXPECTED" = "$ACTUAL" ] || { echo "Downloaded binary checksum verification failed." >&2; exit 1; }
    chmod +x "$TEMP_BINARY"
    mv -f "$TEMP_BINARY" "$BINARY"
    rm -f "$TEMP_HASH"
    rmdir "$LOCK" 2>/dev/null || true
    trap - EXIT INT TERM
  fi
fi

exec "$BINARY" "$@"