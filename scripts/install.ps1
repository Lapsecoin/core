# One-command setup for running LapseCoin from source: makes sure the
# build tools liboqs needs are on PATH, then installs the app via pip.
# libtorrent (DHT peer discovery) has real wheel gaps on some platforms;
# if it's specifically what fails, this retries without it and grabs a
# starter peers list instead, rather than failing the whole install.
#
# Every step here is idempotent -- re-running this is always safe, and it
# never touches an install that already works.
#
# What gets installed, and why (checked against .github/workflows/release.yml
# and liboqs-python's own automatic liboqs build, which runs on first launch):
#   cmake, git, MSVC C++ build tools -- used by that liboqs build.
#   VC++ 2015-2022 runtime           -- the workflow installs vcredist140 for
#                                       the Windows build; installed here only
#                                       if the registry says it is missing.
# Deliberately NOT installed: OpenSSL and ninja. The workflow installs them,
# but liboqs turns OpenSSL off by default on Windows and liboqs-python builds
# with CMake's default generator, so neither is used when running from
# source. Cairo, PyInstaller and miniupnpc are only for building the .exe.
#
# Not part of the release binary, and nothing in the app invokes this
# itself: the prebuilt .exe already has liboqs and libtorrent baked in at
# CI build time and never needs any of this. This only matters for
# `pip install lapsecoin` / running from source.
#
# Everything below lives in a function and ends with `return`, never `exit`.
# The documented way to run this is `irm ... | iex`, which executes in the
# caller's own session, so an `exit` would close the user's PowerShell
# window before they could read the result.

$ErrorActionPreference = "Continue"

function Install-LapseCoin {
    $repoRaw  = if ($env:LAPSECOIN_REPO_RAW)  { $env:LAPSECOIN_REPO_RAW }  else { "https://raw.githubusercontent.com/Lapsecoin/core/main" }
    $peersUrl = if ($env:LAPSECOIN_PEERS_URL) { $env:LAPSECOIN_PEERS_URL } else { "https://lapsenode.vicnas.me/api/peers/download" }
    $wingetFlags = @("-e", "--silent", "--accept-package-agreements", "--accept-source-agreements")

    function Test-Cmd($name) {
        return [bool](Get-Command $name -ErrorAction SilentlyContinue)
    }

    # cl.exe is only on PATH inside a Visual Studio Developer prompt, so
    # `Get-Command cl` is false in a normal PowerShell even with Build Tools
    # installed. Ask the VS installer's own locator first (this also matches
    # Build Tools), and keep the old PATH check as a fallback.
    function Test-MsvcTools {
        if (Test-Cmd "cl") { return $true }
        $pf86 = ${env:ProgramFiles(x86)}
        if (-not $pf86) { return $false }
        $vswhere = Join-Path $pf86 "Microsoft Visual Studio\Installer\vswhere.exe"
        if (Test-Path $vswhere) {
            $found = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
            if ($found) { return $true }
        }
        return $false
    }

    # winget edits the machine/user PATH, which this already-running session
    # does not see. Append (never replace) entries it lacks, so anything
    # already on this session's PATH, such as an activated venv, stays first.
    function Update-SessionPath {
        $have = $env:Path -split ';'
        $fresh = ([Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                  [Environment]::GetEnvironmentVariable("Path", "User")) -split ';'
        foreach ($p in $fresh) {
            if ($p -and ($have -notcontains $p)) { $env:Path += ";$p"; $have += $p }
        }
    }

    # --- 1. build tools: liboqs-python needs these to build liboqs itself
    #        automatically on first run; nothing else to set up beyond this. ---
    $haveMsvc = Test-MsvcTools
    if ((Test-Cmd "cmake") -and (Test-Cmd "git") -and $haveMsvc) {
        Write-Host "cmake, git, and a C++ compiler already present."
    } else {
        Write-Host "Installing build tools (cmake, git, Visual Studio Build Tools)..."
        if (-not (Test-Cmd "winget")) {
            Write-Error "winget not found (needs Windows 10 1809+ or Windows 11 with App Installer). Install cmake, git, and Visual Studio Build Tools (C++ workload) yourself, then re-run."
            return
        }
        if (-not (Test-Cmd "cmake")) { winget install --id Kitware.CMake @wingetFlags }
        if (-not (Test-Cmd "git"))   { winget install --id Git.Git @wingetFlags }
        if (-not $haveMsvc) {
            Write-Host "Installing Visual Studio Build Tools (C++ workload) -- this is a large download and may take a while."
            winget install --id Microsoft.VisualStudio.2022.BuildTools @wingetFlags --override "--quiet --add Microsoft.VisualStudio.Workload.VCTools"
        }
        Update-SessionPath
        Write-Host "You may need to open a new terminal for PATH updates to take effect."
    }

    # --- 1b. VC++ 2015-2022 runtime, as the Windows release workflow does.
    #         Skipped when the registry already reports it installed. ---
    $vcrt = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
    if (-not ($vcrt -and $vcrt.Installed -eq 1)) {
        if (Test-Cmd "winget") {
            Write-Host "Installing the Visual C++ runtime..."
            winget install --id "Microsoft.VCRedist.2015+.x64" @wingetFlags
        } else {
            Write-Warning "Could not check or install the Visual C++ runtime (winget not found). If lapsecoin later reports a missing VCRUNTIME140.dll, install it from https://aka.ms/vs/17/release/vc_redist.x64.exe"
        }
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
        return
    }

    $logContent = Get-Content $installLog -Raw
    Remove-Item $installLog -ErrorAction SilentlyContinue

    if ($logContent -notmatch "(?i)libtorrent") {
        Write-Host ""
        Write-Error "Install failed for a reason unrelated to libtorrent -- see the output above."
        return
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
        return
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
}

Install-LapseCoin
