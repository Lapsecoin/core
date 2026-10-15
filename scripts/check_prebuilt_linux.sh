#!/usr/bin/env bash
# CI only (.github/workflows/liboqs.yml). Proves that scripts/install.sh really
# uses the prebuilt liboqs, in a bare Ubuntu 22.04 container with no compiler.
#
#   check_prebuilt_linux.sh DIST VERSION [published]
#
# DIST holds the asset and SHA256SUMS. Without "published" the script is fed
# DIST through a file:// URL; with it, install.sh uses its real default
# download URL, and DIST is just the same files fetched separately to compare.
#
# Two things keep this honest. First, no cmake, git or compiler may exist, so
# a quiet fallback to building from source cannot succeed. Second, the
# negative cases (a corrupted download, no download) must FAIL: a prebuilt
# that is silently ignored has to turn the run red, not green.
set -euo pipefail

DIST="$(cd "${1:?directory with the liboqs asset and SHA256SUMS}" && pwd)"
V="${2:?liboqs version}"
MODE="${3:-local}"
ASSET="liboqs-$V-linux-x86_64.tar.gz"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends curl ca-certificates python3 python3-pip >/dev/null

command -v pip >/dev/null 2>&1 || ln -s "$(command -v pip3)" /usr/local/bin/pip

for t in cmake git cc gcc g++ clang sudo; do
  if command -v "$t" >/dev/null 2>&1; then
    if [ "${CHECK_ALLOW_TOOLS:-0}" = "1" ]; then
      echo "warning: $t is installed; fine for a local dry run, never in CI"
    else
      echo "$t is installed, so this check would prove nothing" >&2
      exit 1
    fi
  fi
done

export HOME="${CHECK_HOME:-/root}"
export OQS_INSTALL_PATH="$HOME/_oqs"
export LAPSECOIN_REQUIRE_PREBUILT=1
cd "$REPO"
WORK="$(mktemp -d)"

expect_failure() {          # name, base URL
  rm -rf "$OQS_INSTALL_PATH"
  if LAPSECOIN_LIBOQS_BASE="$2" bash scripts/install.sh >"$WORK/$1.log" 2>&1; then
    echo "FAIL: $1 should have stopped the install" >&2
    cat "$WORK/$1.log" >&2
    exit 1
  fi
  if [ -e "$OQS_INSTALL_PATH/lib/liboqs.so" ]; then
    echo "FAIL: $1 left a liboqs behind" >&2
    exit 1
  fi
  echo "ok: $1 is refused"
}

# 1. a corrupted download must be refused, not used and not rebuilt
mkdir "$WORK/bad"; cp "$DIST/$ASSET" "$DIST/SHA256SUMS" "$WORK/bad/"
printf '\x00\x01\x02' | dd of="$WORK/bad/$ASSET" bs=1 seek=100 conv=notrunc 2>/dev/null
expect_failure corrupted-asset "file://$WORK/bad"
grep -q "checksum" "$WORK/corrupted-asset.log"

# 2. a download that is not there must stop the install
mkdir "$WORK/none"
expect_failure missing-asset "file://$WORK/none"

# 3. the real run
rm -rf "$OQS_INSTALL_PATH"
if [ "$MODE" = "published" ]; then
  unset LAPSECOIN_LIBOQS_BASE
else
  export LAPSECOIN_LIBOQS_BASE="file://$DIST"
fi
bash scripts/install.sh 2>&1 | tee "$WORK/install.log"
grep -q "liboqs: using the prebuilt" "$WORK/install.log"
if grep -qE "installing it in|Cloning into|Installing build tools" "$WORK/install.log"; then
  echo "FAIL: the install built something instead of using the prebuilt" >&2
  exit 1
fi

# 4. the library the app loads is the downloaded file, not some other copy
python3 - "$DIST/$ASSET" <<'PY'
import hashlib, os, sys, tarfile
import oqs

loaded = sorted({l.split()[-1] for l in open("/proc/self/maps") if "liboqs" in l})
home_lib = os.path.realpath(os.path.expanduser("~/_oqs/lib"))
assert len(loaded) == 1, loaded
assert os.path.realpath(loaded[0]).startswith(home_lib), loaded

with tarfile.open(sys.argv[1]) as t:
    member = [m for m in t.getmembers() if m.isfile()][0]
    want = hashlib.sha256(t.extractfile(member).read()).hexdigest()
got = hashlib.sha256(open(loaded[0], "rb").read()).hexdigest()
assert got == want, "the loaded liboqs is not the downloaded one"

s = oqs.Signature("Falcon-512")
pk = s.generate_keypair()
sig = s.sign(b"lapsecoin")
assert oqs.Signature("Falcon-512").verify(b"lapsecoin", sig, pk)
print("prebuilt verified:", loaded[0])
PY

# 5. the seeded Falcon library came in the same download, loads from there,
#    and reproduces the public key the wallet page publishes for its first seed
python3 - <<'PY'
import os, sys
sys.path.insert(0, "src")
import falcon_seeded

assert falcon_seeded.available(), "the seeded Falcon library did not come with the prebuilt"
loaded = sorted({l.split()[-1] for l in open("/proc/self/maps") if "libfalcon_seeded" in l})
home_lib = os.path.realpath(os.path.expanduser("~/_oqs/lib"))
assert loaded and all(os.path.realpath(p).startswith(home_lib) for p in loaded), loaded
print("seeded library verified:", loaded[0])
PY
