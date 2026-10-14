#!/usr/bin/env bash
# One-command setup for running LapseCoin from source: gets liboqs (a
# prebuilt copy where one exists, otherwise built from source, which needs
# build tools), then installs the app via pip.
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

# The liboqs release the prebuilt copy comes from. Keep it equal to LIBOQS_REF
# in release.yml and LIBOQS_VERSION in liboqs.yml (a test checks all four).
LIBOQS_VERSION="0.16.0"
LIBOQS_BASE="${LAPSECOIN_LIBOQS_BASE:-https://github.com/Lapsecoin/core/releases/download/liboqs-$LIBOQS_VERSION}"
# 1 makes the script stop instead of building liboqs from source when the
# prebuilt cannot be used. CI sets it so a prebuilt that is quietly ignored
# fails the run instead of being rebuilt and looking fine.
REQUIRE_PREBUILT="${LAPSECOIN_REQUIRE_PREBUILT:-0}"
OQS_DIR="${OQS_INSTALL_PATH:-$HOME/_oqs}"
PREBUILT=0

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

# --- Build tools: only needed when liboqs has to be built from source. ---
ensure_build_tools() {
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
}

python_cmd() { command -v python3 || command -v python; }

# A prebuilt liboqs (published by .github/workflows/liboqs.yml) saves the
# several-minute build and the build tools. Used only if it downloads, matches
# its checksum and loads on this machine; any failure returns 1 and the caller
# falls back to building.
try_prebuilt_liboqs() {
  local asset="liboqs-$LIBOQS_VERSION-linux-x86_64.tar.gz" tmp py glibc
  if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
    echo "liboqs: no prebuilt copy for $(uname -s) $(uname -m)."
    return 1
  fi
  for t in curl tar sha256sum; do
    have "$t" || { echo "liboqs: $t is missing, so the prebuilt copy is skipped."; return 1; }
  done
  py="$(python_cmd)"
  [ -n "$py" ] || { echo "liboqs: python is missing, so the prebuilt copy is skipped."; return 1; }
  glibc="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
  if [ -z "$glibc" ] || [ "$(printf '%s\n2.34\n' "$glibc" | sort -V | head -1)" != "2.34" ]; then
    echo "liboqs: glibc ${glibc:-unknown} is older than the 2.34 the prebuilt copy needs."
    return 1
  fi
  if [ -e "$OQS_DIR/lib/liboqs.so" ]; then
    echo "liboqs: already in $OQS_DIR."
    return 0
  fi
  tmp="$(mktemp -d)"
  if ! curl -fsSL "$LIBOQS_BASE/$asset" -o "$tmp/$asset" \
     || ! curl -fsSL "$LIBOQS_BASE/SHA256SUMS" -o "$tmp/SHA256SUMS"; then
    echo "liboqs: could not download the prebuilt copy from $LIBOQS_BASE"
    rm -rf "$tmp"; return 1
  fi
  if ! (cd "$tmp" && grep " $asset\$" SHA256SUMS | sha256sum -c - >/dev/null 2>&1); then
    echo "liboqs: the download does not match its checksum, so it is not used."
    rm -rf "$tmp"; return 1
  fi
  mkdir -p "$OQS_DIR"
  if ! tar -xzf "$tmp/$asset" -C "$OQS_DIR"; then
    echo "liboqs: could not unpack the prebuilt copy."
    rm -rf "$tmp"; return 1
  fi
  rm -rf "$tmp"
  # It has to load here before the build tools are skipped on its account.
  if ! "$py" -c "import ctypes; ctypes.CDLL('$OQS_DIR/lib/liboqs.so')" 2>/dev/null; then
    echo "liboqs: the prebuilt copy does not load on this system."
    rm -f "$OQS_DIR"/lib/liboqs.so*
    return 1
  fi
  echo "liboqs: using the prebuilt $LIBOQS_VERSION in $OQS_DIR"
  return 0
}

if try_prebuilt_liboqs; then
  PREBUILT=1
elif [ "$REQUIRE_PREBUILT" = "1" ]; then
  echo "LAPSECOIN_REQUIRE_PREBUILT is set and the prebuilt liboqs could not be used, so it is not being built from source." >&2
  exit 1
else
  ensure_build_tools
fi

# Final check, after the app is installed. With the prebuilt copy it confirms
# that oqs really loaded THAT file: liboqs-python prefers any liboqs it finds
# elsewhere on the system, which would make a bad prebuilt look fine. Without
# it, importing oqs makes liboqs-python compile liboqs into ~/_oqs now, which
# is minutes of work, so the first launch does not pay for it.
finish_liboqs() {
  local py
  py="$(python_cmd)"
  if [ "$PREBUILT" = "1" ]; then
    if OQS_DIR="$OQS_DIR" "$py" - <<'PY'
import os, sys
import oqs
want = os.path.realpath(os.path.join(os.environ["OQS_DIR"], "lib"))
loaded = sorted({l.split()[-1] for l in open("/proc/self/maps") if "liboqs" in l})
print("liboqs: loaded from", ", ".join(loaded))
sys.exit(0 if any(os.path.realpath(p).startswith(want) for p in loaded) else 3)
PY
    then
      echo "liboqs is ready (prebuilt)."
      return 0
    fi
    if [ "$REQUIRE_PREBUILT" = "1" ]; then
      echo "The prebuilt liboqs is installed but the app did not load it." >&2
      exit 1
    fi
    echo "The prebuilt liboqs did not load; building it from source instead."
    rm -f "$OQS_DIR"/lib/liboqs.so*
    PREBUILT=0
    ensure_build_tools
  fi
  echo
  echo "Building liboqs now (a few minutes, once) so the first launch starts straight away..."
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
  finish_liboqs
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

finish_liboqs
echo
echo "Installed without libtorrent. Run: lapsecoin"
