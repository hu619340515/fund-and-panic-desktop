$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$root = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $root "desktop")
try {
  npm.cmd ci
  npm.cmd run build:engine
  npm.cmd run dist:win
} finally {
  Pop-Location
}
