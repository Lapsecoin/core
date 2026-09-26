#!/usr/bin/env bash
# One-command setup for running LapseCoin from source: makes sure the
# build tools liboqs needs are on PATH, then installs the app via pip.
# libtorrent (DHT peer discovery) has real wheel gaps on some platforms;
# if it's specifically what fails, this retries without it and grabs a
# starter peers list instead, rather than failing the whole install.
#
# Every step here is idempotent -- re-running this is always safe, and it
# never touches an install that already works.
#
# Not part of the release binary, and nothing in the app invokes this
# itself: the prebuilt AppImage/exe already have liboqs and libtorrent
# baked in at CI build time and never need any of this. This only
# matters for `pip install lapsecoin` / running from source.
set -uo pipefail

REPO_RAW="${LAPSECOIN_REPO_RAW:-https://raw.githubusercontent.com/Lapsecoin/core/main}"
PEERS_URL="${LAPSECOIN_PEERS_URL:-https://lapsenode.vicnas.me/api/peers/download}"

have() { command -v "$1" >/dev/null 2>&1; }

# --- 1. build tools: liboqs-python needs these to build liboqs itself
#        automatically on first run; nothing else to set up beyond this. ---
if have cmake && have git && (have cc || have gcc || have clang); then
  echo "cmake, git, and a C compiler already present."
else
  echo "Installing build tools (cmake, git, a C compiler)..."
  if [ "$(uname)" = "Darwin" ]; then
    if ! have brew; then
      echo "Homebrew not found. Install it from https://brew.sh, then re-run this script." >&2
      exit 1
    fi
    brew install cmake git
    (have cc || have clang) || xcode-select --install
  elif have apt-get; then
    sudo apt-get update -qq
    sudo apt-get install -y cmake git build-essential
  elif have dnf; then
    sudo dnf install -y cmake git gcc gcc-c++
  elif have pacman; then
    sudo pacman -Sy --noconfirm cmake git base-devel
  else
    echo "Unrecognized package manager. Install cmake, git, and a C compiler yourself, then re-run." >&2
    exit 1
  fi
fi

# --- 2. install the app. Try the full install first; only fall back to
#        skipping libtorrent if THAT is specifically what failed -- any
#        other failure surfaces as-is rather than getting masked. ---
INSTALL_LOG="$(mktemp)"
trap 'rm -f "$INSTALL_LOG"' EXIT

if pip install lapsecoin 2>&1 | tee "$INSTALL_LOG"; then
  echo
  echo "Installed. Run: lapsecoin"
  exit 0
fi

if ! grep -qi libtorrent "$INSTALL_LOG"; then
  echo
  echo "Install failed for a reason unrelated to libtorrent -- see the output above." >&2
  exit 1
fi

echo
echo "libtorrent failed to install (real wheel gaps on some platforms); continuing without it."
echo "This only disables automatic DHT peer discovery -- the node still connects using a peers list."
echo

REQS_TMP="$(mktemp)"
if ! curl -fsSL "$REPO_RAW/requirements.txt" -o "$REQS_TMP"; then
  echo "Could not fetch the dependency list; install lapsecoin's other dependencies yourself, then: pip install lapsecoin --no-deps" >&2
  exit 1
fi
grep -v '^libtorrent' "$REQS_TMP" | pip install -r /dev/stdin
pip install lapsecoin --no-deps
rm -f "$REQS_TMP"

if curl -fsSL "$PEERS_URL" -o lapsecoin_peers.json; then
  echo "Fetched a starter peers list into ./lapsecoin_peers.json."
else
  echo "Could not fetch a starter peers list (network issue?); lapsecoin will still run, just without any peers until you add some." >&2
fi

echo
echo "Installed without libtorrent. Run: lapsecoin"
