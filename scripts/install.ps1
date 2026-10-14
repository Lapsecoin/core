# One-command setup for running LapseCoin from source: gets liboqs (a
# prebuilt copy where one exists, otherwise built from source, which needs
# the Visual Studio C++ build tools), then installs the app via pip.
# libtorrent (DHT peer discovery) has real wheel gaps on some platforms;
# if it's specifically what fails, this retries without it rather than
# failing the whole install.
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
    $wingetFlags = @("-e", "--silent", "--accept-package-agreements", "--accept-source-agreements")

    # The liboqs release the prebuilt copy comes from. Keep it equal to
    # LIBOQS_REF in release.yml and LIBOQS_VERSION in liboqs.yml (a test checks
    # all four).
    $LiboqsVersion = "0.16.0"
    $liboqsBase = if ($env:LAPSECOIN_LIBOQS_BASE) { $env:LAPSECOIN_LIBOQS_BASE } else { "https://github.com/Lapsecoin/core/releases/download/liboqs-$LiboqsVersion" }
    # 1 makes the script stop instead of building liboqs from source when the
    # prebuilt cannot be used. CI sets it so a prebuilt that is quietly ignored
    # fails the run instead of being rebuilt and looking fine.
    $requirePrebuilt = ($env:LAPSECOIN_REQUIRE_PREBUILT -eq "1")
    $oqsDir = if ($env:OQS_INSTALL_PATH) { $env:OQS_INSTALL_PATH } else { Join-Path $HOME "_oqs" }
    $prebuilt = $false
    $script:toolsReady = $false   # true once Ensure-BuildTools has run

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

    # --- Build tools: only needed when liboqs has to be built from source. ---
    function Ensure-BuildTools {
        $script:buildToolsOk = $false
        $haveMsvc = Test-MsvcTools
        if ((Test-Cmd "cmake") -and (Test-Cmd "git") -and $haveMsvc) {
            Write-Host "cmake, git, and a C++ compiler already present."
        } else {
            Write-Host "Installing build tools (cmake, git, Visual Studio Build Tools)..."
            if (-not (Test-Cmd "winget")) {
                Write-Error "winget not found (needs Windows 10 1809+ or Windows 11 with App Installer). Install cmake, git, and Visual Studio Build Tools (C++ workload) yourself, then re-run."
                $script:buildToolsOk = $false
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
        $script:buildToolsOk = $true
        $script:toolsReady = $true
    }

    function Get-LiboqsAsset($name, $dest) {
        if ($liboqsBase -like "file:*") {
            Copy-Item -LiteralPath ([Uri]"$liboqsBase/$name").LocalPath -Destination $dest -ErrorAction Stop
        } else {
            Invoke-WebRequest -Uri "$liboqsBase/$name" -OutFile $dest -UseBasicParsing -ErrorAction Stop
        }
    }

    # A prebuilt oqs.dll (published by .github/workflows/liboqs.yml) saves the
    # build and the multi-gigabyte Visual Studio Build Tools. Used only if it
    # downloads, matches its checksum and loads on this machine; any failure
    # returns $false and the caller falls back to building.
    function Install-PrebuiltLiboqs {
        $asset = "liboqs-$LiboqsVersion-windows-x64.zip"
        if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
            Write-Host "liboqs: no prebuilt copy for $($env:PROCESSOR_ARCHITECTURE)."
            return $false
        }
        if (-not (Test-Cmd "python")) {
            Write-Host "liboqs: python is missing, so the prebuilt copy is skipped."
            return $false
        }
        $dll = Join-Path $oqsDir "bin\oqs.dll"
        if (Test-Path $dll) {
            Write-Host "liboqs: already in $oqsDir."
            return $true
        }
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ("liboqs-" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            try {
                Get-LiboqsAsset $asset (Join-Path $tmp $asset)
                Get-LiboqsAsset "SHA256SUMS" (Join-Path $tmp "SHA256SUMS")
            } catch {
                Write-Host "liboqs: could not download the prebuilt copy from $liboqsBase"
                return $false
            }
            $line = Get-Content (Join-Path $tmp "SHA256SUMS") | Where-Object { $_ -match ("\s" + [regex]::Escape($asset) + "$") } | Select-Object -First 1
            $want = if ($line) { ($line -split "\s+")[0].ToLower() } else { "" }
            $got = (Get-FileHash (Join-Path $tmp $asset) -Algorithm SHA256).Hash.ToLower()
            if (-not $want -or $want -ne $got) {
                Write-Host "liboqs: the download does not match its checksum, so it is not used."
                return $false
            }
            New-Item -ItemType Directory -Force -Path $oqsDir | Out-Null
            Expand-Archive -LiteralPath (Join-Path $tmp $asset) -DestinationPath $oqsDir -Force
        } finally {
            Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        }
        # It has to load here before the build tools are skipped on its account.
        python -c "import ctypes, sys; ctypes.CDLL(sys.argv[1])" $dll 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "liboqs: the prebuilt copy does not load on this system."
            Remove-Item -Force (Join-Path $oqsDir "bin\oqs.dll") -ErrorAction SilentlyContinue
            return $false
        }
        Write-Host "liboqs: using the prebuilt $LiboqsVersion in $oqsDir"
        return $true
    }

    $prebuilt = [bool](Install-PrebuiltLiboqs | Select-Object -Last 1)
    if (-not $prebuilt) {
        if ($requirePrebuilt) {
            Write-Error "LAPSECOIN_REQUIRE_PREBUILT is set and the prebuilt liboqs could not be used, so it is not being built from source."
            $script:installFailed = $true
            return
        }
        Ensure-BuildTools | Out-Null
        if (-not $script:buildToolsOk) { return }
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

    # Final check, after the app is installed. With the prebuilt copy it
    # confirms that oqs really loaded THAT file: liboqs-python prefers any
    # liboqs it finds elsewhere on the system, which would make a bad prebuilt
    # look fine. Without it, importing oqs makes liboqs-python compile liboqs
    # now, which is minutes of work, so the first launch does not pay for it.
    function Finish-Liboqs {
        if ($prebuilt) {
            $check = "import os, sys, oqs; p = os.path.realpath(oqs.native()._name); w = os.path.realpath(sys.argv[1]); print('liboqs: loaded from', p); sys.exit(0 if p.lower().startswith(w.lower()) else 3)"
            python -c $check $oqsDir
            if ($LASTEXITCODE -eq 0) {
                Write-Host "liboqs is ready (prebuilt)."
                return
            }
            if ($requirePrebuilt) {
                Write-Error "The prebuilt liboqs is installed but the app did not load it."
                $script:installFailed = $true
                return
            }
            Write-Host "The prebuilt liboqs did not load; building it from source instead."
            Remove-Item -Force (Join-Path $oqsDir "bin\oqs.dll") -ErrorAction SilentlyContinue
            Ensure-BuildTools | Out-Null
            if (-not $script:buildToolsOk) { return }
        }
        Write-Host ""
        Write-Host "Building liboqs now (a few minutes, once) so the first launch starts straight away..."
        python -c "import oqs"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "liboqs is ready."
        } else {
            Write-Warning "Could not build liboqs here; it will be built on the first launch instead."
        }
    }

    # --- 2. install the app. Try the full install first; only fall back to
    #        skipping libtorrent if THAT is specifically what failed -- any
    #        other failure surfaces as-is rather than getting masked. ---
    $installLog = [System.IO.Path]::GetTempFileName()
    pip install lapsecoin 2>&1 | Tee-Object -FilePath $installLog
    if ($LASTEXITCODE -eq 0) {
        Finish-Liboqs
        Write-Host ""
        Write-Host "Installed. Run: lapsecoin"
        Remove-Item $installLog -ErrorAction SilentlyContinue
        return
    }

    $logContent = Get-Content $installLog -Raw

    # The build tools are skipped when the prebuilt liboqs is used, because
    # nothing else normally needs them. If a package does have to be compiled
    # (no wheel for this Python), pip fails with a compiler error: install the
    # tools then and try once more, rather than leave a failed install.
    if (-not $script:toolsReady -and $logContent -match "(?i)subprocess-exited-with-error|failed building wheel|failed to build|microsoft visual c\+\+|unable to execute|fatal error:") {
        Write-Host ""
        Write-Host "A package had to be compiled and the build tools were skipped; installing them and trying once more."
        Ensure-BuildTools | Out-Null
        if (-not $script:buildToolsOk) { return }
        pip install lapsecoin 2>&1 | Tee-Object -FilePath $installLog
        if ($LASTEXITCODE -eq 0) {
            Finish-Liboqs
            Write-Host ""
            Write-Host "Installed. Run: lapsecoin"
            Remove-Item $installLog -ErrorAction SilentlyContinue
            return
        }
        $logContent = Get-Content $installLog -Raw
    }
    Remove-Item $installLog -ErrorAction SilentlyContinue

    if ($logContent -notmatch "(?i)libtorrent") {
        Write-Host ""
        Write-Error "Install failed for a reason unrelated to libtorrent -- see the output above."
        return
    }

    Write-Host ""
    Write-Host "libtorrent failed to install (real wheel gaps on some platforms); continuing without it."
    Write-Host "This only disables automatic DHT peer discovery -- peers are then found on the local network, through other peers, or with --peer."
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

    Finish-Liboqs
    Write-Host ""
    Write-Host "Installed without libtorrent. Run: lapsecoin"
}

Install-LapseCoin

# Only for CI: a run that required the prebuilt liboqs and could not use it
# must be a failed run, which `return` alone does not make it.
if ($script:installFailed -and $env:LAPSECOIN_REQUIRE_PREBUILT -eq "1") { exit 1 }
