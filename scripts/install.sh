#!/usr/bin/env bash
# One-command setup for running LapseCoin from source: makes sure the
# build tools liboqs needs are on PATH, then installs the app via pip.
# libtorrent (DHT peer discovery) has real wheel gaps on some platforms;
# if it's specifically what fails, this retries without it rather than
# failing the whole install.
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

have() { command -v "$1" >/dev/null 2>&1; }

INSTALL_LOG="$(mktemp)"
trap 'rm -f "$INSTALL_LOG"' EXIT

# liboqs builds against OpenSSL by default on Linux (it is only off by default
# on Windows), and the CI workflow installs libssl-dev for that reason, so the
# headers count as a required build tool here. macOS is not covered by the
# workflow, so it is not checked for them.
have_openssl_headers() {
  [ -f /usr/include/openssl/ssl.h ] || [ -f /usr/local/include/openssl/ssl.h ] \
    || { have pkg-config && pkg-config --exists openssl; }
}

# --- 1. build tools: liboqs-python needs these to build liboqs itself. ---
if have cmake && have git && (have cc || have gcc || have clang) \
   && { [ "$(uname)" = "Darwin" ] || have_openssl_headers; }; then
  echo "cmake, git, a C compiler, and OpenSSL headers already present."
else
  echo "Installing build tools (cmake, git, a C compiler, OpenSSL headers)..."
  if [ "$(uname)" = "Darwin" ]; then
    if ! have brew; then
      echo "Homebrew not found. Install it from https://brew.sh, then re-run this script." >&2
      exit 1
    fi
    brew install cmake git
    (have cc || have clang) || xcode-select --install
  elif have apt-get; then
    sudo apt-get update -qq
    sudo apt-get install -y cmake git build-essential libssl-dev
  elif have dnf; then
    sudo dnf install -y cmake git gcc gcc-c++ openssl-devel
  elif have pacman; then
    sudo pacman -Sy --noconfirm cmake git base-devel openssl
  else
    echo "Unrecognized package manager. Install cmake, git, and a C compiler yourself, then re-run." >&2
    exit 1
  fi
fi

# liboqs-python compiles liboqs into ~/_oqs the first time it is imported,
# which is minutes of work with a countdown. Importing it here instead means
# that happens during the install, not on the first launch. If it fails the
# install still stands and the first launch just tries again.
prebuild_liboqs() {
  echo
  echo "Building liboqs now (a few minutes, once) so the first launch starts straight away..."
  local py
  py="$(command -v python3 || command -v python)"
  if [ -n "$py" ] && "$py" -c "import oqs" 2>&1; then
    echo "liboqs is ready."
  else
    echo "Could not build liboqs here; it will be built on the first launch instead." >&2
  fi
}

# pip cannot replace a package the system installed without a RECORD file
# (Debian's python3-blinker on Colab and many cloud images), and that one
# error aborts the whole install, leaving every other dependency uninstalled
# too. When the log shows it, install a fresh copy over the system one, then
# retry. The output is appended to INSTALL_LOG for the checks below.
pip_install() {
  local log pkgs rc
  log="$(mktemp)"
  pip install "$@" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  cat "$log" >> "$INSTALL_LOG"
  if [ "$rc" -ne 0 ]; then
    pkgs="$(grep -oE 'Cannot uninstall [A-Za-z0-9_.-]+' "$log" | awk '{print $3}' | sort -u | tr '\n' ' ')"
    if [ -n "$pkgs" ] && grep -q "RECORD file not found" "$log"; then
      echo
      echo "pip cannot replace ${pkgs}(installed by the system). Installing a fresh copy over it, then retrying."
      pip install --ignore-installed $pkgs 2>&1 | tee -a "$INSTALL_LOG"
      pip install "$@" 2>&1 | tee -a "$INSTALL_LOG"
      rc=${PIPESTATUS[0]}
    fi
  fi
  rm -f "$log"
  return "$rc"
}

# --- 2. install the app. Try the full install first; only fall back to
#        skipping libtorrent if THAT is specifically what failed -- any
#        other failure surfaces as-is rather than getting masked. ---
if pip_install lapsecoin; then
  prebuild_liboqs
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
echo "This only disables automatic DHT peer discovery -- peers are then found on the local network, through other peers, or with --peer."
echo

REQS_TMP="$(mktemp)"
if ! curl -fsSL "$REPO_RAW/requirements.txt" -o "$REQS_TMP"; then
  echo "Could not fetch the dependency list; install lapsecoin's other dependencies yourself, then: pip install lapsecoin --no-deps" >&2
  exit 1
fi
grep -v '^libtorrent' "$REQS_TMP" > "$REQS_TMP.nolt"
pip_install -r "$REQS_TMP.nolt"
pip_install lapsecoin --no-deps
rm -f "$REQS_TMP" "$REQS_TMP.nolt"

prebuild_liboqs
echo
echo "Installed without libtorrent. Run: lapsecoin"
