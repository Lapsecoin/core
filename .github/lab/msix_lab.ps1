# Throwaway lab: how a full-trust MSIX behaves with data, uninstall, upgrade and listening sockets.
$ErrorActionPreference = 'Continue'
function H($t) { Write-Host "`n##### $t" }

H "Machine"
cmd /c ver | Out-Host
dotnet --list-sdks | Out-Host

H "Allow sideloading on this runner"
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" /v AllowAllTrustedApps /t REG_DWORD /d 1 /f | Out-Host
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" /v AllowDevelopmentWithoutDevLicense /t REG_DWORD /d 1 /f | Out-Host

$work = "C:\lab"
New-Item -ItemType Directory $work -Force | Out-Null
New-Item -ItemType Directory C:\labout -Force | Out-Null

H "Build the probe program"
dotnet publish "$PSScriptRoot\probe\probe.csproj" -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o "$work\probe" | Out-Host
if (-not (Test-Path "$work\probe\probe.exe")) { throw "probe.exe was not built" }

H "Certificate and tools"
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

function New-Package($name, $version, $variant) {
    $dir = "$work\layout_$name"
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
    New-Item "$dir\Assets" -ItemType Directory -Force | Out-Null
    Copy-Item "$work\probe\probe.exe" "$dir\probe.exe"
    New-Png "$dir\Assets\StoreLogo.png" 50
    New-Png "$dir\Assets\Square44x44Logo.png" 44
    New-Png "$dir\Assets\Square150x150Logo.png" 150
    $props = ""
    $caps = '<rescap:Capability Name="runFullTrust" />'
    if ($variant -eq 'B') {
        $props = '<desktop6:FileSystemWriteVirtualization>disabled</desktop6:FileSystemWriteVirtualization><desktop6:RegistryWriteVirtualization>disabled</desktop6:RegistryWriteVirtualization>'
        $caps += '<rescap:Capability Name="unvirtualizedResources" />'
    }
    if ($variant -eq 'C') {
        $props = '<virtualization:FileSystemWriteVirtualization><virtualization:ExcludedDirectories><virtualization:ExcludedDirectory>$(KnownFolder:LocalAppData)\LapseLabExcl</virtualization:ExcludedDirectory></virtualization:ExcludedDirectories></virtualization:FileSystemWriteVirtualization>'
        $caps += '<rescap:Capability Name="unvirtualizedResources" />'
    }
    $manifest = @"
<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
  xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
  xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
  xmlns:desktop6="http://schemas.microsoft.com/appx/manifest/desktop/windows10/6"
  xmlns:virtualization="http://schemas.microsoft.com/appx/manifest/virtualization/windows10"
  IgnorableNamespaces="uap rescap desktop6 virtualization">
  <Identity Name="LapseLab.$name" Publisher="CN=LapseLab" Version="$version" ProcessorArchitecture="x64" />
  <Properties>
    <DisplayName>LapseLab $name</DisplayName>
    <PublisherDisplayName>LapseLab</PublisherDisplayName>
    <Logo>Assets\StoreLogo.png</Logo>
    $props
  </Properties>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.22621.0" />
  </Dependencies>
  <Resources><Resource Language="en-us" /></Resources>
  <Applications>
    <Application Id="Probe" Executable="probe.exe" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="LapseLab $name" Description="probe" BackgroundColor="transparent" Square150x150Logo="Assets\Square150x150Logo.png" Square44x44Logo="Assets\Square44x44Logo.png" />
    </Application>
  </Applications>
  <Capabilities>$caps</Capabilities>
</Package>
"@
    Set-Content -Path "$dir\AppxManifest.xml" -Value $manifest -Encoding UTF8
    $msix = "$work\$name-$version.msix"
    & $makeappx pack /d $dir /p $msix /o 2>&1 | Out-Host
    & $signtool sign /fd SHA256 /f "$work\lab.pfx" /p lab $msix 2>&1 | Out-Host
    return $msix
}

$targets = [ordered]@{
    'local_existing' = "$env:LOCALAPPDATA\LapseLab\existing.txt"
    'local_new'      = "$env:LOCALAPPDATA\LapseLab\new_by_pkg.txt"
    'roaming'        = "$env:APPDATA\LapseLabRoaming\roaming.txt"
    'profile'        = "$env:USERPROFILE\LapseLabProfile\profile.txt"
    'documents'      = "$([Environment]::GetFolderPath('MyDocuments'))\LapseLabDocs\doc.txt"
    'excluded'       = "$env:LOCALAPPDATA\LapseLabExcl\excl.txt"
    'programdata'    = "$env:ProgramData\LapseLabPD\pd.txt"
}

function Reset-Real {
    foreach ($k in $targets.Keys) {
        $d = Split-Path $targets[$k] -Parent
        Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue
    }
    Remove-Item C:\labout\* -Force -ErrorAction SilentlyContinue
}

function Show-Real($label) {
    H "REAL FILE SYSTEM, seen from OUTSIDE the package ($label)"
    foreach ($k in $targets.Keys) {
        $p = $targets[$k]
        if (Test-Path $p) {
            $c = (Get-Content $p -Raw).Trim() -replace "\r?\n", " | "
            Write-Host ("{0,-15} PRESENT  {1}" -f $k, $c)
        } else {
            Write-Host ("{0,-15} missing" -f $k)
        }
    }
}

function Show-Private($label, $pfn) {
    $priv = Join-Path $env:LOCALAPPDATA "Packages\$pfn"
    H "PACKAGE PRIVATE STORE ($label): $priv"
    if (Test-Path $priv) {
        Get-ChildItem $priv -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("  " + $_.FullName.Substring($priv.Length)) }
    } else { Write-Host "  (folder does not exist)" }
}

function Install-Pkg($msix) {
    try { Add-AppxPackage -Path $msix -ErrorAction Stop; Write-Host "installed $msix" }
    catch { Write-Host "INSTALL FAILED: $($_.Exception.Message)"; return $false }
    return $true
}

function Run-In-Package($name, $argline, $tag, $wait = $true) {
    $pkg = Get-AppxPackage -Name "LapseLab.$name"
    $exe = Join-Path $pkg.InstallLocation "probe.exe"
    Remove-Item "C:\labout\probe_$tag.log" -Force -ErrorAction SilentlyContinue
    try {
        Invoke-CommandInDesktopPackage -PackageFamilyName $pkg.PackageFamilyName -AppId Probe -Command $exe -Args $argline
    } catch { Write-Host "Invoke-CommandInDesktopPackage FAILED: $($_.Exception.Message)" }
    if ($wait) {
        for ($i = 0; $i -lt 40 -and -not (Test-Path "C:\labout\probe_$tag.log"); $i++) { Start-Sleep 1 }
        if (Test-Path "C:\labout\probe_$tag.log") { Write-Host "--- probe output ($tag) ---"; Get-Content "C:\labout\probe_$tag.log" | Out-Host }
        else { Write-Host "NO OUTPUT from probe ($tag): the package process did not run or could not write C:\labout" }
    }
}

function Experiment($variant, $title) {
    $name = "Probe$variant"
    H "EXPERIMENT $variant : $title"
    Reset-Real
    New-Item "$env:LOCALAPPDATA\LapseLab" -ItemType Directory -Force | Out-Null
    Set-Content "$env:LOCALAPPDATA\LapseLab\existing.txt" "created-before-install-by-outside-process"
    $m1 = New-Package $name "1.0.0.0" $variant
    if (-not (Install-Pkg $m1)) { return }
    $pkg = Get-AppxPackage -Name "LapseLab.$name"
    $pfn = $pkg.PackageFamilyName
    Run-In-Package $name "write W1" "W1"
    Show-Real "after first run"
    Show-Private "after first run" $pfn

    Write-Host "`n--- upgrade to 1.0.1.0 ---"
    $m2 = New-Package $name "1.0.1.0" $variant
    if (Install-Pkg $m2) {
        Run-In-Package $name "read R2" "R2"
        Show-Real "after upgrade"
        Show-Private "after upgrade" $pfn
    }

    Write-Host "`n--- uninstall ---"
    Get-AppxPackage -Name "LapseLab.$name" | Remove-AppxPackage
    Start-Sleep 3
    Show-Real "AFTER UNINSTALL"
    Show-Private "AFTER UNINSTALL" $pfn
}

Experiment 'A' 'default virtualization'
Experiment 'B' 'FileSystemWriteVirtualization=disabled + unvirtualizedResources'
Experiment 'C' 'Windows 11 ExcludedDirectories for LapseLabExcl + unvirtualizedResources'

H "EXPERIMENT N : can a full-trust package accept a connection from outside?"
Reset-Real
$mN = New-Package "ProbeN" "1.0.0.0" 'A'
if (Install-Pkg $mN) {
    Remove-Item C:\labout\probe_listen_ready.txt -Force -ErrorAction SilentlyContinue
    Run-In-Package "ProbeN" "listen L1" "L1" $false
    for ($i = 0; $i -lt 30 -and -not (Test-Path C:\labout\probe_listen_ready.txt); $i++) { Start-Sleep 1 }
    if (Test-Path C:\labout\probe_listen_ready.txt) {
        Get-Content C:\labout\probe_listen_ready.txt | Out-Host
        $ips = @('127.0.0.1') + @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' } | Select-Object -First 1 -ExpandProperty IPAddress)
        foreach ($h in $ips) {
            try { $c = New-Object Net.Sockets.TcpClient; $c.Connect($h, 18333); $r = New-Object IO.StreamReader($c.GetStream()); Write-Host ("connect {0}: {1}" -f $h, $r.ReadLine()); $c.Close() }
            catch { Write-Host ("connect {0}: FAILED {1}" -f $h, $_.Exception.Message) }
        }
    } else { Write-Host "listener never reported ready" }
    Get-AppxPackage -Name "LapseLab.ProbeN" | Remove-AppxPackage
}
H "DONE"
