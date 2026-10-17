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
TOOLS_READY=0   # 1 once ensure_build_tools has run

have() { command -v "$1" >/dev/null 2>&1; }

INSTALL_LOG="$(mktemp)"
trap 'rm -f "$INSTALL_LOG" "$INSTALL_LOG.last"' EXIT

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
  TOOLS_READY=1
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

# The HD wallet needs seeded FALCON key generation, which liboqs does not have;
# a small library built from the same sources provides it (native/falcon_seeded).
# The prebuilt download carries it. Without the prebuilt, or if it is missing
# from it, it is compiled from the sources the app was installed with, which
# needs a C compiler (installed here if it has to be). Runs after the app is
# installed, because the sources come with it.
finish_falcon_seeded() {
  local py check
  py="$(python_cmd)"
  [ -n "$py" ] || return 0
  check() {
    OQS_INSTALL_PATH="$OQS_DIR" "$py" - <<'PY'
import os, sys
import lapsecoin_data
sys.path.insert(0, os.path.join(list(lapsecoin_data.__path__)[0], "src"))
try:
    import falcon_seeded
except ModuleNotFoundError:
    sys.exit(2)     # the installed release predates the recovery-phrase wallet
sys.exit(0 if falcon_seeded.available() else 1)
PY
  }
  check
  case $? in
    0) echo "FALCON seeded key generation is ready."; return 0 ;;
    2) echo "This lapsecoin release predates the recovery-phrase wallet; nothing to build."; return 0 ;;
  esac
  if [ "$REQUIRE_PREBUILT" = "1" ]; then
    echo "The seeded FALCON library is not in the prebuilt download." >&2
    exit 1
  fi
  [ "$TOOLS_READY" = "1" ] || ensure_build_tools
  if check; then
    echo "FALCON seeded key generation is ready (built here)."
  else
    echo "Could not build the seeded FALCON library. The node still runs on an old key, but a new" >&2
    echo "recovery-phrase wallet needs it: install a C compiler and re-run this script." >&2
  fi
}

# The packages pip refused to replace because the system installed them
# without the record pip needs to uninstall them (Debian's python3-blinker on
# Colab and many cloud images), one name per word, or nothing if that is not
# what went wrong. pip words it differently by version, so every wording is
# matched:
#   current: error: uninstall-no-record-file
#            × Cannot uninstall blinker 1.7.0
#            The package's contents are unknown: no RECORD file was found for blinker.
#   older:   ERROR: Cannot uninstall 'blinker'. ... RECORD file not found.
#            ERROR: Cannot uninstall 'blinker'. It is a distutils installed project
unremovable_packages() {
  grep -qiE "uninstall-no-record-file|no RECORD file|RECORD file (was )?not found|distutils installed project" "$1" || return 0
  sed -nE "s/.*Cannot uninstall ['\"]?([A-Za-z0-9_.-]+)['\"]?.*/\1/p" "$1" | sort -u | tr '\n' ' '
}

# True when pip's errors name libtorrent, not merely when it lists libtorrent
# among the packages it is collecting and downloading, which it does on every
# run. Only then is dropping libtorrent the fix.
libtorrent_failed() {
  grep -iE '^[[:space:]]*(ERROR|error|×|Failed)' "$INSTALL_LOG.last" 2>/dev/null | grep -qi libtorrent
}

# One pip error aborts the whole install and leaves every other dependency
# uninstalled too, so a package pip cannot replace is installed fresh over the
# system one and the install retried. The output is appended to INSTALL_LOG
# for the checks below.
pip_install() {
  local log pkgs rc
  log="$(mktemp)"
  pip install "$@" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  cat "$log" >> "$INSTALL_LOG"
  cp "$log" "$INSTALL_LOG.last"
  if [ "$rc" -ne 0 ]; then
    pkgs="$(unremovable_packages "$log")"
    if [ -n "$pkgs" ]; then
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

# The build tools are skipped when the prebuilt liboqs is used, because nothing
# else normally needs them. If a package does have to be compiled (no wheel for
# this Python or platform), pip fails with a compiler error: install the tools
# then and try once more, rather than leave the user with a failed install.
needs_compiler() {
  grep -qiE "subprocess-exited-with-error|failed building wheel|failed to build|command '(gcc|cc|c\+\+|g\+\+|cmake)'|cmake: command not found|unable to execute|fatal error: .*\.h|microsoft visual c\+\+" \
    "$INSTALL_LOG.last" 2>/dev/null
}

pip_install_with_tools() {
  pip_install "$@" && return 0
  if [ "$TOOLS_READY" != "1" ] && needs_compiler; then
    echo
    echo "A package had to be compiled and the build tools were skipped; installing them and trying once more."
    ensure_build_tools
    pip_install "$@"
    return $?
  fi
  return 1
}

# Without libtorrent: its wheel is missing on some platforms. Everything else
# is installed from the dependency list with libtorrent taken out, then the
# app on top of it. Returns 1, and says so, if either install fails: the app
# without its dependencies is not an install.
install_without_libtorrent() {
  local reqs rc=0
  reqs="$(mktemp)"
  if ! curl -fsSL "$REPO_RAW/requirements.txt" -o "$reqs"; then
    echo "Could not fetch the dependency list; install lapsecoin's other dependencies yourself, then: pip install lapsecoin --no-deps" >&2
    rm -f "$reqs"
    return 1
  fi
  grep -v '^libtorrent' "$reqs" > "$reqs.nolt"
  pip_install_with_tools -r "$reqs.nolt" && pip_install_with_tools lapsecoin --no-deps || rc=1
  rm -f "$reqs" "$reqs.nolt"
  return $rc
}

# --- 2. install the app. Try the full install first; only fall back to
#        skipping libtorrent if THAT is specifically what failed -- any
#        other failure surfaces as-is rather than getting masked. ---
if pip_install_with_tools lapsecoin; then
  finish_liboqs
  finish_falcon_seeded
  echo
  echo "Installed. Run: lapsecoin"
  exit 0
fi

if ! libtorrent_failed; then
  echo
  echo "Install failed for a reason unrelated to libtorrent -- see the output above." >&2
  exit 1
fi

echo
echo "libtorrent failed to install (real wheel gaps on some platforms); continuing without it."
echo "This only disables automatic DHT peer discovery -- peers are then found on the local network, through other peers, or with --peer."
echo

if ! install_without_libtorrent; then
  echo
  echo "Install failed -- see the output above." >&2
  exit 1
fi

finish_liboqs
finish_falcon_seeded
echo
echo "Installed without libtorrent. Run: lapsecoin"
