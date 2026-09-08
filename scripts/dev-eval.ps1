# Test an ask-loop change from the command line, against the servers the app
# (or scripts\dev.ps1) already has running: build the core in debug and run
# the benchmark, or a subset of it, with the library from the app's config.
#
#   scripts\dev-eval.ps1                      # all 29 cases
#   scripts\dev-eval.ps1 reason               # a capability, a horizon, or a case id
#   scripts\dev-eval.ps1 bleach-drum -Label "new prompt"
#   scripts\dev-eval.ps1 -Chat                # a multi-turn conversation instead
#
# Prompts and limits come from the app's tuning.yaml override when it exists
# (Settings > Advanced), so what is tested is what the app would do.
param(
  [string]$Only = '',
  [string]$Label = 'dev',
  [switch]$Chat
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (Test-Path "$PSScriptRoot\vsenv.ps1") { . "$PSScriptRoot\vsenv.ps1" }
$cfgPath = "$env:APPDATA\org.museion.desktop\config.json"
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$db = Join-Path $cfg.library_dir 'museion-index.db'
$tuning = "$env:APPDATA\org.museion.desktop\tuning.yaml"
$tuningArgs = @()
if (Test-Path $tuning) { $tuningArgs = @('--tuning', $tuning) }

Set-Location $root
cargo build -q -p museion-core --bin museion
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$exe = "$root\target\debug\museion.exe"
if ($Chat) {
  & $exe chat --db $db @tuningArgs
} else {
  $onlyArgs = @()
  if ($Only) { $onlyArgs = @('--only', $Only) }
  & $exe eval --db $db --label $Label @onlyArgs @tuningArgs
}
