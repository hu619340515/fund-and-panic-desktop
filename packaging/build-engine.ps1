$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$root = Split-Path -Parent $PSScriptRoot
$engine = Join-Path $root "engine"
$dist = Join-Path $engine "dist"
$venv = Join-Path $engine ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$scripts = Join-Path $engine "scripts"
$static = Join-Path $scripts "a_share_panic_index\web\static"
$defaults = Join-Path $scripts "a_share_panic_index\defaults.yaml"
$settings = Join-Path $engine "config\settings.yaml"

function Get-EngineSourceHash {
  $files = @(
    Get-Item (Join-Path $engine "server.py")
    Get-Item (Join-Path $engine "requirements.txt")
    Get-ChildItem (Join-Path $engine "scripts") -Recurse -File | Where-Object { $_.Extension -in ".py", ".yaml", ".html" }
    Get-ChildItem (Join-Path $engine "config") -Recurse -File -Filter "*.yaml"
  ) | Sort-Object { [IO.Path]::GetRelativePath($engine, $_.FullName).Replace("\", "/") }
  $hash = [Security.Cryptography.IncrementalHash]::CreateHash([Security.Cryptography.HashAlgorithmName]::SHA256)
  foreach ($file in $files) {
    $relativePath = [IO.Path]::GetRelativePath($engine, $file.FullName).Replace("\", "/")
    $hash.AppendData([Text.Encoding]::UTF8.GetBytes($relativePath))
    $hash.AppendData([byte[]]@(0))
    $hash.AppendData([IO.File]::ReadAllBytes($file.FullName))
  }
  return [Convert]::ToHexString($hash.GetHashAndReset()).ToLowerInvariant()
}

$sourceHashBefore = Get-EngineSourceHash
if (-not (Test-Path $python)) {
  python -m venv $venv
}
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $engine "requirements.txt")
& $python -m pip install pyinstaller
& $python -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --name panic-engine `
  --paths $scripts `
  --add-data "${static};a_share_panic_index/web/static" `
  --add-data "${defaults};a_share_panic_index" `
  --add-data "${settings};config" `
  --collect-all akshare `
  --collect-all baostock `
  --collect-all exchange_calendars `
  --collect-all matplotlib `
  --collect-all mootdx `
  --collect-all yaml `
  --collect-submodules uvicorn `
  (Join-Path $engine "server.py") `
  --distpath $dist `
  --workpath (Join-Path $engine "build") `
  --specpath (Join-Path $engine "build")

$sourceHashAfter = Get-EngineSourceHash
if ($sourceHashAfter -ne $sourceHashBefore) {
  throw "引擎源码在构建期间发生变化，拒绝生成来源不明的产物"
}
$manifest = [ordered]@{
  sourceSha256 = $sourceHashAfter
  engineVersion = "3.0-realtime"
  clientVersion = "2.0.0"
} | ConvertTo-Json
$manifestPath = Join-Path $dist "panic-engine\build-manifest.json"
[IO.File]::WriteAllText($manifestPath, $manifest + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
