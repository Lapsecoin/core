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
# itself: the prebuilt .exe already has liboqs and libtorrent baked in at
# CI build time and never needs any of this. This only matters for
# `pip install lapsecoin` / running from source.
#
# Unlike scripts/install.sh, this hasn't been run against a real Windows
# machine as part of writing it -- there's no PowerShell interpreter
# available in the environment this was authored in. Test it before
# relying on it, and open an issue if something here doesn't work.

$ErrorActionPreference = "Continue"

$repoRaw  = if ($env:LAPSECOIN_REPO_RAW)  { $env:LAPSECOIN_REPO_RAW }  else { "https://raw.githubusercontent.com/Lapsecoin/core/main" }
$peersUrl = if ($env:LAPSECOIN_PEERS_URL) { $env:LAPSECOIN_PEERS_URL } else { "https://lapsenode.vicnas.me/api/peers/download" }

function Test-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# --- 1. build tools: liboqs-python needs these to build liboqs itself
#        automatically on first run; nothing else to set up beyond this. ---
if ((Test-Cmd "cmake") -and (Test-Cmd "git") -and (Test-Cmd "cl")) {
    Write-Host "cmake, git, and a C++ compiler already present."
} else {
    Write-Host "Installing build tools (cmake, git, Visual Studio Build Tools)..."
    if (-not (Test-Cmd "winget")) {
        Write-Error "winget not found (needs Windows 10 1809+ or Windows 11 with App Installer). Install cmake, git, and Visual Studio Build Tools (C++ workload) yourself, then re-run."
        exit 1
    }
    if (-not (Test-Cmd "cmake")) { winget install --id Kitware.CMake -e --silent }
    if (-not (Test-Cmd "git"))   { winget install --id Git.Git -e --silent }
    if (-not (Test-Cmd "cl")) {
        Write-Host "Installing Visual Studio Build Tools (C++ workload) -- this is a large download and may take a while."
        winget install --id Microsoft.VisualStudio.2022.BuildTools -e --silent --override "--quiet --add Microsoft.VisualStudio.Workload.VCTools"
    }
    Write-Host "You may need to open a new terminal for PATH updates to take effect."
}

# --- 2. install the app. Try the full install first; only fall back to
#        skipping libtorrent if THAT is specifically what failed -- any
#        other failure surfaces as-is rather than getting masked. ---
$installLog = [System.IO.Path]::GetTempFileName()
pip install lapsecoin 2>&1 | Tee-Object -FilePath $installLog
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Installed. Run: lapsecoin"
    Remove-Item $installLog -ErrorAction SilentlyContinue
    exit 0
}

$logContent = Get-Content $installLog -Raw
Remove-Item $installLog -ErrorAction SilentlyContinue

if ($logContent -notmatch "(?i)libtorrent") {
    Write-Host ""
    Write-Error "Install failed for a reason unrelated to libtorrent -- see the output above."
    exit 1
}

Write-Host ""
Write-Host "libtorrent failed to install (real wheel gaps on some platforms); continuing without it."
Write-Host "This only disables automatic DHT peer discovery -- the node still connects using a peers list."
Write-Host ""

$reqsTmp = [System.IO.Path]::GetTempFileName()
try {
    Invoke-WebRequest -Uri "$repoRaw/requirements.txt" -OutFile $reqsTmp -UseBasicParsing
} catch {
    Write-Error "Could not fetch the dependency list; install lapsecoin's other dependencies yourself, then: pip install lapsecoin --no-deps"
    exit 1
}
$reqsNoLt = [System.IO.Path]::GetTempFileName()
Get-Content $reqsTmp | Where-Object { $_ -notmatch '^libtorrent' } | Set-Content $reqsNoLt
pip install -r $reqsNoLt
pip install lapsecoin --no-deps
Remove-Item $reqsTmp, $reqsNoLt -ErrorAction SilentlyContinue

try {
    Invoke-WebRequest -Uri $peersUrl -OutFile "lapsecoin_peers.json" -UseBasicParsing
    Write-Host "Fetched a starter peers list into .\lapsecoin_peers.json."
} catch {
    Write-Warning "Could not fetch a starter peers list (network issue?); lapsecoin will still run, just without any peers until you add some."
}

Write-Host ""
Write-Host "Installed without libtorrent. Run: lapsecoin"
