# Throwaway lab: how a full-trust MSIX behaves with data, uninstall, upgrade and listening sockets.
$ErrorActionPreference = 'Continue'
function Section($t) { Write-Host "`n##### $t" }

Section "Machine"
cmd /c ver | Out-Host
dotnet --list-sdks | Out-Host

Section "Allow sideloading on this runner"
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" /v AllowAllTrustedApps /t REG_DWORD /d 1 /f | Out-Host
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" /v AllowDevelopmentWithoutDevLicense /t REG_DWORD /d 1 /f | Out-Host

$work = "C:\lab"
New-Item -ItemType Directory $work -Force | Out-Null
New-Item -ItemType Directory C:\labout -Force | Out-Null

Section "Certificate and tools"
$cert = New-SelfSignedCertificate -Type Custom -Subject "CN=LapseLab" -KeyUsage DigitalSignature -FriendlyName "LapseLab" -CertStoreLocation "Cert:\CurrentUser\My" -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")
$pw = ConvertTo-SecureString -String "lab" -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath "$work\lab.pfx" -Password $pw | Out-Null
Export-Certificate -Cert $cert -FilePath "$work\lab.cer" | Out-Null
Import-Certificate -FilePath "$work\lab.cer" -CertStoreLocation Cert:\LocalMachine\TrustedPeople | Out-Null
$kit = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\makeappx.exe" | Sort-Object FullName | Select-Object -Last 1
$makeappx = $kit.FullName
$signtool = Join-Path $kit.DirectoryName "signtool.exe"
Write-Host "makeappx: $makeappx"

Add-Type -AssemblyName System.Drawing
function New-Png($path, $size) {
    $bmp = New-Object System.Drawing.Bitmap $size, $size
    $g = [System.Drawing.Graphics]::FromImage($bmp); $g.Clear([System.Drawing.Color]::SteelBlue); $g.Dispose()
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
}


function Wait-Ports($label, $seconds) {
    $t0 = Get-Date
    while (((Get-Date) - $t0).TotalSeconds -lt $seconds) {
        $l = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 8333, 8335 }
        if ($l) { Write-Host ("{0}: listening after {1:N0}s on ports {2}" -f $label, ((Get-Date) - $t0).TotalSeconds, (($l.LocalPort | Sort-Object -Unique) -join ',')); return }
        Start-Sleep 5
    }
    Write-Host "$label: nothing listening on 8333/8335 after $seconds s"
}
function Show-Log($path) {
    Write-Host "--- $path (lines with log markers; recovery phrase lines removed) ---"
    if (Test-Path $path) {
        Get-Content $path | Where-Object { $_ -match '(?i)\[|error|traceback|warn|listen|start|serv|peer|sync|port' -and $_ -notmatch '(?i)phrase' } | Select-Object -First 40 | Out-Host
    } else { Write-Host "(no such file)" }
}

Section "Install the real LapseCoin MSI, copy its files, then remove it"
$ver = "1.1.4"
curl.exe -fsSL -o "$work\lapsecoin.msi" "https://lapsecoin.github.io/core/downloads/$ver/lapsecoin.msi"
(Get-Item "$work\lapsecoin.msi").Length | Out-Host
$p = Start-Process msiexec -ArgumentList "/i `"$work\lapsecoin.msi`" /qn LAUNCH=0" -Wait -PassThru
Write-Host "msiexec exit code: $($p.ExitCode)"
$installed = "$env:LOCALAPPDATA\Programs\LapseCoin"
if (-not (Test-Path "$installed\lapsecoin.exe")) { throw "lapsecoin.exe not found in $installed" }
Get-ChildItem $installed | Select-Object -First 12 -ExpandProperty Name | Out-Host
$layout = "$work\layout_real"
Remove-Item $layout -Recurse -Force -ErrorAction SilentlyContinue
New-Item "$layout\Assets" -ItemType Directory -Force | Out-Null
Copy-Item "$installed\*" $layout -Recurse -Force

Section "CONTROL: the same build, NOT packaged"
Remove-Item C:\labout\* -Force -ErrorAction SilentlyContinue
$ctl = Start-Process cmd.exe -ArgumentList "/c cd /d `"$installed`" && set LAPSECOIN_PASSPHRASE=lab-passphrase-123&& lapsecoin.exe --no-gui > C:\labout\control.log 2>&1" -PassThru
Wait-Ports "control" 120
Show-Log C:\labout\control.log
Get-Process lapsecoin -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 3
Start-Process msiexec -ArgumentList "/x `"$work\lapsecoin.msi`" /qn" -Wait
Remove-Item "$env:LOCALAPPDATA\LapseCoin" -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ("layout size MB: {0:N0}" -f ((Get-ChildItem $layout -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))
Write-Host ("marker present: " + (Test-Path "$layout\installed-by-msi"))

New-Png "$layout\Assets\StoreLogo.png" 50
New-Png "$layout\Assets\Square44x44Logo.png" 44
New-Png "$layout\Assets\Square150x150Logo.png" 150
$manifest = @"
<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
  xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
  xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
  IgnorableNamespaces="uap rescap">
  <Identity Name="LapseLab.Real" Publisher="CN=LapseLab" Version="1.0.0.0" ProcessorArchitecture="x64" />
  <Properties>
    <DisplayName>LapseLab Real</DisplayName>
    <PublisherDisplayName>LapseLab</PublisherDisplayName>
    <Logo>Assets\StoreLogo.png</Logo>
  </Properties>
  <Dependencies><TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.22621.0" /></Dependencies>
  <Resources><Resource Language="en-us" /></Resources>
  <Applications>
    <Application Id="App" Executable="lapsecoin.exe" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="LapseLab Real" Description="real build" BackgroundColor="transparent" Square150x150Logo="Assets\Square150x150Logo.png" Square44x44Logo="Assets\Square44x44Logo.png" />
    </Application>
  </Applications>
  <Capabilities><rescap:Capability Name="runFullTrust" /></Capabilities>
</Package>
"@
Set-Content -Path "$layout\AppxManifest.xml" -Value $manifest -Encoding UTF8
$msix = "$work\real.msix"
& $makeappx pack /d $layout /p $msix /o 2>&1 | Select-Object -Last 3 | Out-Host
& $signtool sign /fd SHA256 /f "$work\lab.pfx" /p lab $msix 2>&1 | Out-Null
Add-AppxPackage -Path $msix -ErrorAction Stop
$pkg = Get-AppxPackage -Name "LapseLab.Real"
$pfn = $pkg.PackageFamilyName
Write-Host "installed package $($pkg.PackageFullName)"

Section "Run the real node inside the package (headless, throwaway wallet)"
Remove-Item C:\labout\* -Force -ErrorAction SilentlyContinue
$args_ = "/c cd /d `"$($pkg.InstallLocation)`" && set LAPSECOIN_PASSPHRASE=lab-passphrase-123&& lapsecoin.exe --no-gui > C:\labout\node.log 2>&1"
Invoke-CommandInDesktopPackage -PackageFamilyName $pfn -AppId App -Command "cmd.exe" -Args $args_
Wait-Ports "packaged" 120
$procs = Get-Process lapsecoin -ErrorAction SilentlyContinue
Write-Host ("lapsecoin processes: " + @($procs).Count)
if ($procs) {
    $ids = $procs.Id
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $ids -contains $_.OwningProcess } | Sort-Object LocalPort | ForEach-Object { Write-Host ("LISTEN {0}:{1}" -f $_.LocalAddress, $_.LocalPort) }
}
foreach ($port in 8333, 8335) {
    try { $r = Invoke-WebRequest "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 15; Write-Host "HTTP $port -> $($r.StatusCode)" }
    catch { Write-Host "HTTP $port -> FAILED $($_.Exception.Message)" }
}
Show-Log C:\labout\node.log

Section "Where did the wallet end up? (seen from OUTSIDE the package)"
$realData = "$env:LOCALAPPDATA\LapseCoin"
$privData = "$env:LOCALAPPDATA\Packages\$pfn\LocalCache\Local\LapseCoin"
$profData = "$env:USERPROFILE\LapseCoin"
foreach ($d in @($realData, $privData, $profData)) {
    if (Test-Path $d) { Write-Host "EXISTS  $d"; Get-ChildItem $d -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 12 | ForEach-Object { Write-Host ("   " + $_.FullName.Substring($d.Length) + "  " + $_.Length) } }
    else { Write-Host "absent  $d" }
}

Section "Stop the node, uninstall, and see what is left"
Get-Process lapsecoin -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 3
Get-AppxPackage -Name "LapseLab.Real" | Remove-AppxPackage
Start-Sleep 5
foreach ($d in @($realData, $privData, $profData)) { Write-Host (("{0,-8} {1}" -f $(if (Test-Path $d) { 'KEPT' } else { 'DELETED/ABSENT' }), $d)) }
if (Test-Path "$realData\lapsecoin_wallet.json") { Write-Host "WALLET FILE SURVIVED UNINSTALL" } else { Write-Host "WALLET FILE DID NOT SURVIVE UNINSTALL (or was never in the real folder)" }
Section "DONE"
