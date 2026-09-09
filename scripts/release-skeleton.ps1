# Build the Windows skeleton zip locally: the app, the installer, every
# llama.cpp build, kiwix-tools, pdfium, the WebView2 offline installer,
# licences and README -- no content, no models. What the drive workflow
# publishes on a tag (see .github/workflows/drive.yml and docs/portable.md).
#
#   scripts\release-skeleton.ps1            # -> dist\museion-drive-<version>-windows.zip
param([string]$Out = 'dist')
$root = Split-Path $PSScriptRoot -Parent
if (Test-Path "$PSScriptRoot\vsenv.ps1") { . "$PSScriptRoot\vsenv.ps1" }
Set-Location "$root\apps\desktop"
cmd /c "npm run tauri build"
Set-Location $root
cargo build --release -p museion-core --bin museion
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$version = (Get-Content "$root\apps\desktop\src-tauri\tauri.conf.json" | ConvertFrom-Json).version
$installer = Get-ChildItem "$root\target\release\bundle\nsis\*-setup.exe" | Select-Object -First 1
$skeleton = "$root\$Out\skeleton"
if (Test-Path $skeleton) { Remove-Item $skeleton -Recurse -Force }
& "$root\target\release\museion.exe" prepare --skeleton --dest $skeleton --bundle go-bag `
  --accelerators cuda,vulkan,cpu --app "$root\target\release\museion-desktop.exe" --installer $installer.FullName
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$zip = "$root\$Out\museion-drive-v$version-windows.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "$skeleton\*" -DestinationPath $zip
Get-Item $zip | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB)}}
