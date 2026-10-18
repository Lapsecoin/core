# Installs the Store package on a Windows runner the way a user would and
# checks what the data folder does. Run by .github/workflows/release.yml (a
# required release check) and by hand through .github/workflows/msix.yml.
#
#   scripts/test_msix.ps1 -Source dist\lapsecoin-win -Version 1.1.4 -Msix lapsecoin.msix
#
# The package for the Store is unsigned, so a copy is signed with a throwaway
# certificate whose subject is the manifest publisher (Windows requires them
# to match) and trusted on the runner. The checks, all reported together:
#   - the node, started inside the package with a throwaway passphrase, makes
#     its wallet in %USERPROFILE%\LapseCoin and nowhere in the package store
#   - an upgrade to the next version leaves that wallet untouched
#   - an uninstall leaves that wallet in place
# Identity comes from the MSIX_* environment variables, as for build_msix.ps1.
# Ports are not checked: the node does not listen in a headless runner, packaged
# or not.

param(
    [Parameter(Mandatory)] [string] $Source,
    [Parameter(Mandatory)] [string] $Version,
    [Parameter(Mandatory)] [string] $Msix
)

$ErrorActionPreference = "Stop"
$failures = @()
$wallet = "lapsecoin_wallet.json"
$data = Join-Path $env:USERPROFILE "LapseCoin"

$work = Join-Path $PWD "msix-test"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item $work -ItemType Directory | Out-Null

# Packages signed with a certificate Windows does not trust cannot be installed.
$plain = [guid]::NewGuid().ToString()
$cert = New-SelfSignedCertificate -Type Custom -Subject $env:MSIX_PUBLISHER -KeyUsage DigitalSignature `
    -FriendlyName "LapseCoin package test" -CertStoreLocation "Cert:\CurrentUser\My" `
    -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")
Export-PfxCertificate -Cert $cert -FilePath "$work\test.pfx" -Password (ConvertTo-SecureString $plain -AsPlainText -Force) | Out-Null
Export-Certificate -Cert $cert -FilePath "$work\test.cer" | Out-Null
Import-Certificate -FilePath "$work\test.cer" -CertStoreLocation Cert:\LocalMachine\TrustedPeople | Out-Null
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" /v AllowAllTrustedApps /t REG_DWORD /d 1 /f | Out-Null

$signtool = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe" |
    Sort-Object { [version]$_.Directory.Parent.Name } -Descending | Select-Object -First 1
function Sign($file) {
    & $signtool.FullName sign /fd SHA256 /f "$work\test.pfx" /p $plain $file | Out-Null
    if ($LASTEXITCODE) { throw "signing $file failed" }
}

# The package a user would upgrade to: the same program, the next patch number.
$parts = $Version.Split(".")
$next = "$($parts[0]).$($parts[1]).$([int]$parts[2] + 1)"
Copy-Item $Msix "$work\v1.msix"
Sign "$work\v1.msix"
& "$PSScriptRoot\build_msix.ps1" -Source $Source -Version $next -Name $env:MSIX_IDENTITY_NAME `
    -Publisher $env:MSIX_PUBLISHER -PublisherDisplayName $env:MSIX_PUBLISHER_DISPLAY_NAME `
    -DisplayName $env:MSIX_DISPLAY_NAME -Out "$work\v2.msix"
Sign "$work\v2.msix"

function Stop-Node {
    Get-Process lapsecoin -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep 3
}
function WalletHash { (Get-FileHash (Join-Path $data $wallet) -Algorithm SHA256).Hash }

Remove-Item $data -Recurse -Force -ErrorAction SilentlyContinue

Add-AppxPackage -Path "$work\v1.msix"
$pkg = Get-AppxPackage -Name $env:MSIX_IDENTITY_NAME
Write-Host "installed $($pkg.PackageFullName)"

# cmd starts the node so the output can be kept; the node inherits the package.
$node = "/c cd /d `"$($pkg.InstallLocation)`" && set LAPSECOIN_PASSPHRASE=ci-test-passphrase-123&& lapsecoin.exe --no-gui > `"$work\node.log`" 2>&1"
Invoke-CommandInDesktopPackage -PackageFamilyName $pkg.PackageFamilyName -AppId LapseCoin -Command "cmd.exe" -Args $node
for ($i = 0; $i -lt 90 -and -not (Test-Path (Join-Path $data $wallet)); $i++) { Start-Sleep 2 }

if (-not (Test-Path (Join-Path $data $wallet))) {
    $failures += "the node did not make $wallet in $data"
} else {
    $before = WalletHash
    Write-Host "wallet made in $data"
}
$private = Join-Path $env:LOCALAPPDATA "Packages\$($pkg.PackageFamilyName)"
if (Get-ChildItem $private -Recurse -Filter $wallet -ErrorAction SilentlyContinue) {
    $failures += "a wallet was also written into the package store $private"
}
Stop-Node

if ($before) {
    try {
        Add-AppxPackage -Path "$work\v2.msix"
        $now = (Get-AppxPackage -Name $env:MSIX_IDENTITY_NAME).Version
        if ($now -ne "$next.0") { $failures += "after the upgrade the version is $now, expected $next.0" }
        if ((WalletHash) -ne $before) { $failures += "the upgrade changed the wallet" }
    } catch { $failures += "the upgrade failed: $($_.Exception.Message)" }

    Remove-AppxPackage -Package (Get-AppxPackage -Name $env:MSIX_IDENTITY_NAME).PackageFullName
    if (-not (Test-Path (Join-Path $data $wallet))) { $failures += "the uninstall deleted the wallet" }
    elseif ((WalletHash) -ne $before) { $failures += "the uninstall changed the wallet" }
} else {
    Remove-AppxPackage -Package $pkg.PackageFullName -ErrorAction SilentlyContinue
}
if (Get-AppxPackage -Name $env:MSIX_IDENTITY_NAME) { $failures += "the package is still installed after the uninstall" }

if ($failures) {
    Write-Host "node output (lines about the recovery phrase left out):"
    if (Test-Path "$work\node.log") { Get-Content "$work\node.log" | Where-Object { $_ -notmatch '(?i)phrase' } | Select-Object -Last 40 }
    $failures | ForEach-Object { Write-Host "FAILED: $_" }
    exit 1
}
Write-Host "install, first run, upgrade and uninstall all kept the wallet in $data"
Remove-Item $data -Recurse -Force -ErrorAction SilentlyContinue
