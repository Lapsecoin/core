# Installs an MSI on a Windows runner, starts the node from it, uninstalls the
# MSI silently while the node is running, and checks the node was stopped and
# the program removed. An uninstall that leaves the node running leaves it
# holding the data folder, which then blocks a move of that folder and the next
# start (the single-instance lock). Run by .github/workflows/release.yml (a
# release check) and by hand through .github/workflows/msi-uninstall.yml.
#
#   scripts/test_msi_uninstall.ps1 -Msi lapsecoin.msi

param([Parameter(Mandatory)] [string] $Msi)

$ErrorActionPreference = "Stop"
$failures = @()
$msi = (Resolve-Path $Msi).Path
$dir = Join-Path $env:LOCALAPPDATA 'Programs\LapseCoin'

function Run($arguments, $seconds, $what) {
    $proc = Start-Process msiexec -ArgumentList $arguments -PassThru
    $null = $proc.Handle
    if (-not $proc.WaitForExit($seconds * 1000)) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $script:failures += "$what did not finish in $seconds seconds"
        return $null
    }
    return $proc
}

$i = Run @('/i', $msi, '/qn', 'LAUNCH=0') 180 'the install'
if (-not $i -or $i.ExitCode -ne 0) { throw "the install failed" }

$env:LAPSECOIN_PASSPHRASE = "ci-uninstall-test-123"
Start-Process "$dir\lapsecoin.exe" -ArgumentList '--no-gui' | Out-Null
Start-Sleep 20
$before = @(Get-Process lapsecoin -ErrorAction SilentlyContinue)
if (-not $before) { throw "the node did not stay running, so the test cannot tell anything" }
Write-Host "node running before the uninstall: pid $($before.Id -join ', ')"

$u = Run @('/x', $msi, '/qn') 180 'the uninstall'
Start-Sleep 5
$after = @(Get-Process lapsecoin -ErrorAction SilentlyContinue)
if ($after) {
    $failures += "the uninstall left LapseCoin running (pid $($after.Id -join ', '))"
    $after | Stop-Process -Force
}
if ($u -and $u.ExitCode -notin 0, 3010) { $failures += "the uninstall exited $($u.ExitCode)" }
if (Test-Path (Join-Path $dir 'lapsecoin.exe')) { $failures += "the uninstall left the program behind" }

if ($failures) { $failures | ForEach-Object { Write-Host "FAILED: $_" }; exit 1 }
Write-Host "the uninstall stopped the running node and removed the program"
