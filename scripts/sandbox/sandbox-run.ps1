# Runs inside Windows Sandbox, started by Start-Sandbox.ps1: installs the test-signed package,
# starts it the way the Start menu does, and watches it. Results go to C:\Lapse\out, which is
# the "out" folder on the PC the Sandbox was started from.

param([int] $Minutes = 15)

$ErrorActionPreference = "Continue"
$out = "C:\Lapse\out"
New-Item $out -ItemType Directory -Force | Out-Null
$status = Join-Path $out "status.log"
function Say($text) {
    $line = "{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $text
    Add-Content $status $line
    Write-Host $line
}

$build = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
Say "Windows build $($build.CurrentBuildNumber).$($build.UBR), $env:PROCESSOR_ARCHITECTURE"

# A test sends no crash report to GitHub: the program and the launcher both honour this file.
$data = Join-Path $env:USERPROFILE "LapseCoin"
New-Item $data -ItemType Directory -Force | Out-Null
New-Item (Join-Path $data "crash-reports-off") -ItemType File -Force | Out-Null

# If Windows ends the program for a fault, keep a small memory dump of it (stacks of every thread).
$dumps = Join-Path $out "dumps"
New-Item $dumps -ItemType Directory -Force | Out-Null
$key = "HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\lapsecoin.exe"
New-Item $key -Force | Out-Null
Set-ItemProperty $key DumpFolder $dumps
Set-ItemProperty $key DumpType 1 -Type DWord

Import-Certificate -FilePath "C:\Lapse\lapsecoin-test.cer" -CertStoreLocation Cert:\LocalMachine\TrustedPeople | Out-Null
try {
    Add-AppxPackage -Path "C:\Lapse\lapsecoin-test-signed.msix"
} catch {
    Say "the package would not install: $($_.Exception.Message)"
    return
}
$pkg = Get-AppxPackage | Where-Object { $_.InstallLocation -and (Test-Path (Join-Path $_.InstallLocation "lapsecoin.exe")) } | Select-Object -First 1
if (-not $pkg) { Say "the package installed but was not found"; return }
Say "installed $($pkg.PackageFullName)"

$began = Get-Date
Start-Process "shell:AppsFolder\$($pkg.PackageFamilyName)!LapseCoin"
Say "started it; you can use it like a tester would (create a wallet, click around)"

function Running { @(Get-Process lapsecoin, lapsecoin-launch -ErrorAction SilentlyContinue).Count -gt 0 }

function Collect {
    Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = $began } -ErrorAction SilentlyContinue |
        Where-Object { $_.ProviderName -match "Application Error|Windows Error Reporting|Application Hang|\.NET" } |
        Format-List TimeCreated, ProviderName, Id, Message | Out-String | Set-Content (Join-Path $out "events.txt")
    Get-WinEvent -FilterHashtable @{ LogName = "Microsoft-Windows-CodeIntegrity/Operational"; StartTime = $began } -ErrorAction SilentlyContinue |
        Format-List TimeCreated, Id, Message | Out-String | Set-Content (Join-Path $out "code-integrity.txt")
    foreach ($f in "lapsecoin-crash.log", "lapsecoin-start.log", "lapsecoin.log") {
        $p = Join-Path $data $f
        if (Test-Path $p) { Copy-Item $p (Join-Path $out $f) -Force }
    }
}

Start-Sleep 20
$until = $began.AddMinutes($Minutes)
$next = $began.AddMinutes(1)
$died = $false
while ((Get-Date) -lt $until) {
    if (-not (Running)) {
        Say ("the app was GONE after {0} seconds" -f [int]((Get-Date) - $began).TotalSeconds)
        $died = $true
        break
    }
    if ((Get-Date) -ge $next) {
        Say ("still running after {0} minutes" -f [int]((Get-Date) - $began).TotalMinutes)
        $next = $next.AddMinutes(1)
    }
    Start-Sleep 10
}
Start-Sleep 5
Collect
if ($died) {
    Say "it died: see events.txt, lapsecoin-crash.log and the dumps folder in out"
} else {
    Say "it ran the whole $Minutes minutes; the logs are in out. This window can stay open."
}
