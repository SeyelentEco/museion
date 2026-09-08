# Run Museion for iteration: debug build, Vite hot reload for the UI, and the
# servers left running between restarts so the chat model loads once.
#
#   scripts\dev.ps1            # starts (or adopts) the servers, opens the window
#
# Edit apps/desktop/src/*.ts or index.html: the window reloads in a second.
# Edit Rust: cargo rebuilds in debug (about 20 s after the first time) and
# relaunches the window; the servers survive and are adopted.
#
# The window is opened with WebView2's debugging port on 9222, so the UI can
# be driven from a script for testing.
$root = Split-Path $PSScriptRoot -Parent
if (Test-Path "$PSScriptRoot\vsenv.ps1") { . "$PSScriptRoot\vsenv.ps1" }
$env:MUSEION_KEEP_SERVICES = '1'
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = '--remote-debugging-port=9222'
Set-Location "$root\apps\desktop"
cmd /c "npm run tauri dev"
