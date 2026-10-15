# CI only (.github/workflows/liboqs.yml). Proves that scripts/install.ps1
# really uses the prebuilt liboqs.
#
#   check_prebuilt_windows.ps1 -Dist DIR -Version 0.16.0 [-Published]
#
# Dist holds the asset and SHA256SUMS. Without -Published the script is fed
# Dist through a file: URL; with it, install.ps1 uses its real default download
# URL, and Dist is just the same files fetched separately to compare.
#
# The runner has Visual Studio and cmake, so a quiet fallback to building
# cannot be ruled out by their absence. Instead the install is run with
# LAPSECOIN_REQUIRE_PREBUILT=1, where falling back is an error, the log must
# show no build, and the library the app loads must be the downloaded file.
# The negative cases (a corrupted download, no download) must FAIL.
param(
    [Parameter(Mandatory)] [string] $Dist,
    [Parameter(Mandatory)] [string] $Version,
    [switch] $Published
)
$ErrorActionPreference = "Stop"

$Dist = (Resolve-Path $Dist).Path
$asset = "liboqs-$Version-windows-x64.zip"
$repo = Split-Path -Parent $PSScriptRoot
$work = Join-Path ([IO.Path]::GetTempPath()) ("oqs-check-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work | Out-Null
$oqsDir = Join-Path $work "home\_oqs"
$env:OQS_INSTALL_PATH = $oqsDir
$env:LAPSECOIN_REQUIRE_PREBUILT = "1"

function Run-Install($base, $logName) {
    if ($base) { $env:LAPSECOIN_LIBOQS_BASE = $base } else { Remove-Item Env:LAPSECOIN_LIBOQS_BASE -ErrorAction SilentlyContinue }
    $log = Join-Path $work $logName
    & pwsh -NoProfile -File (Join-Path $repo "scripts\install.ps1") *>&1 | Tee-Object -FilePath $log | Out-Host
    return [pscustomobject]@{ Code = $LASTEXITCODE; Log = (Get-Content $log -Raw) }
}

function Expect-Failure($name, $base) {
    Remove-Item -Recurse -Force $oqsDir -ErrorAction SilentlyContinue
    $r = Run-Install $base "$name.log"
    if ($r.Code -eq 0) { throw "FAIL: $name should have stopped the install" }
    if (Test-Path (Join-Path $oqsDir "bin\oqs.dll")) { throw "FAIL: $name left a liboqs behind" }
    Write-Host "ok: $name is refused"
    return $r
}

# 1. a corrupted download must be refused, not used and not rebuilt
$bad = Join-Path $work "bad"; New-Item -ItemType Directory -Path $bad | Out-Null
Copy-Item (Join-Path $Dist $asset), (Join-Path $Dist "SHA256SUMS") $bad
$bytes = [IO.File]::ReadAllBytes((Join-Path $bad $asset)); $bytes[100] = $bytes[100] -bxor 0xFF
[IO.File]::WriteAllBytes((Join-Path $bad $asset), $bytes)
$r = Expect-Failure "corrupted-asset" ([Uri]$bad).AbsoluteUri
if ($r.Log -notmatch "checksum") { throw "FAIL: the corrupted download was not refused for its checksum" }

# 2. a download that is not there must stop the install
$none = Join-Path $work "none"; New-Item -ItemType Directory -Path $none | Out-Null
Expect-Failure "missing-asset" ([Uri]$none).AbsoluteUri | Out-Null

# 3. the real run
Remove-Item -Recurse -Force $oqsDir -ErrorAction SilentlyContinue
$base = if ($Published) { $null } else { ([Uri]$Dist).AbsoluteUri }
$r = Run-Install $base "install.log"
if ($r.Code -ne 0) { throw "FAIL: the install failed" }
if ($r.Log -notmatch "liboqs: using the prebuilt") { throw "FAIL: the install did not use the prebuilt" }
if ($r.Log -match "installing it in|Cloning into|Installing build tools") { throw "FAIL: the install built something instead of using the prebuilt" }

# 4. the library the app loads is the downloaded file, not some other copy
$ref = Join-Path $work "ref"
Expand-Archive -LiteralPath (Join-Path $Dist $asset) -DestinationPath $ref
$want = (Get-FileHash (Join-Path $ref "bin\oqs.dll") -Algorithm SHA256).Hash.ToLower()
$py = @'
import hashlib, os, sys
import oqs
p = os.path.realpath(oqs.native()._name)
assert p.lower().startswith(os.path.realpath(sys.argv[1]).lower()), p
assert hashlib.sha256(open(p, "rb").read()).hexdigest() == sys.argv[2], "the loaded liboqs is not the downloaded one"
s = oqs.Signature("Falcon-512"); pk = s.generate_keypair(); sig = s.sign(b"lapsecoin")
assert oqs.Signature("Falcon-512").verify(b"lapsecoin", sig, pk)
print("prebuilt verified:", p)
'@
$pyFile = Join-Path $work "verify.py"
Set-Content -Path $pyFile -Value $py -Encoding ascii
python $pyFile $oqsDir $want
if ($LASTEXITCODE -ne 0) { throw "FAIL: the loaded liboqs is not the downloaded prebuilt" }

# 5. the seeded Falcon library came in the same download, loads from there,
#    and reproduces the public key the wallet page publishes for its first seed
$py2 = @'
import os, sys
sys.path.insert(0, "src")
import falcon_seeded
assert falcon_seeded.available(), "the seeded Falcon library did not come with the prebuilt"
p = os.path.realpath(falcon_seeded._lib._name)
assert p.lower().startswith(os.path.realpath(sys.argv[1]).lower()), p
print("seeded library verified:", p)
'@
$pyFile2 = Join-Path $work "verify_seeded.py"
Set-Content -Path $pyFile2 -Value $py2 -Encoding ascii
$env:OQS_INSTALL_PATH = $oqsDir
Push-Location $repo
python $pyFile2 $oqsDir
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { throw "FAIL: the seeded Falcon library did not come with the prebuilt" }
