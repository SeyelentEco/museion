# A standalone exe without the release profile's LTO, for trying a change
# outside `tauri dev`. Not an installer: use `npm run tauri build` for that.
#
#   scripts\dev-build.ps1          # debug exe at target\debug\museion-desktop.exe
#   scripts\dev-build.ps1 -Run     # ...and launch it with the servers kept
#
# Times on the development machine (i7-12700K): full release build with LTO
# about 3 minutes; this, after the first time, about 20 to 40 seconds.
param([switch]$Run)
$root = Split-Path $PSScriptRoot -Parent
if (Test-Path "$PSScriptRoot\vsenv.ps1") { . "$PSScriptRoot\vsenv.ps1" }
Set-Location "$root\apps\desktop"
$sw = [Diagnostics.Stopwatch]::StartNew()
cmd /c "npm run tauri build -- --debug --no-bundle"
"built in {0:0} s" -f $sw.Elapsed.TotalSeconds
if ($Run) {
  $env:MUSEION_KEEP_SERVICES = '1'
  $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = '--remote-debugging-port=9222'
  Start-Process "$root\target\debug\museion-desktop.exe"
}
