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
$riskModel = Join-Path $engine "config\risk-model-v4.yaml"
$requirementsLock = Join-Path $engine "requirements-windows-lock.txt"
$miniRacer = Join-Path $venv "Lib\site-packages\py_mini_racer"

function Get-EngineSourceHash {
  $files = @(
    Get-Item (Join-Path $engine "server.py")
    Get-ChildItem -LiteralPath $engine -File -Filter "requirements*.txt"
    Get-ChildItem (Join-Path $engine "scripts") -Recurse -File | Where-Object { $_.Extension -in ".py", ".yaml", ".html" }
    Get-ChildItem (Join-Path $engine "config") -Recurse -File -Filter "*.yaml"
  )
  # 与 Node 的默认字符串排序一致，避免系统区域设置改变下划线和点的顺序。
  $sourcePaths = [string[]]($files | ForEach-Object { [IO.Path]::GetRelativePath($engine, $_.FullName).Replace("\", "/") })
  [Array]::Sort($sourcePaths, [StringComparer]::Ordinal)
  $hash = [Security.Cryptography.IncrementalHash]::CreateHash([Security.Cryptography.HashAlgorithmName]::SHA256)
  foreach ($relativePath in $sourcePaths) {
    $hash.AppendData([Text.Encoding]::UTF8.GetBytes($relativePath))
    $hash.AppendData([byte[]]@(0))
    $hash.AppendData([IO.File]::ReadAllBytes((Join-Path $engine $relativePath)))
  }
  return [Convert]::ToHexString($hash.GetHashAndReset()).ToLowerInvariant()
}

$sourceHashBefore = Get-EngineSourceHash
if (-not (Test-Path -LiteralPath $requirementsLock)) { throw "缺少已验证的 Windows 依赖锁文件 requirements-windows-lock.txt" }
if (-not (Test-Path $python)) {
  python -m venv $venv
}
& $python -m pip install --disable-pip-version-check -r $requirementsLock
# mini-racer 与 py-mini-racer 都写入 py_mini_racer 命名空间；保留两者依赖元数据，最后重装新版运行库以确定 DLL 归属。
$miniRacerPins = @([IO.File]::ReadAllLines($requirementsLock, [Text.Encoding]::UTF8) | Where-Object { $_ -match '^mini-racer==[0-9][0-9A-Za-z.+-]*$' })
if ($miniRacerPins.Count -ne 1) { throw "Windows 锁文件必须恰好锁定一个 mini-racer 版本" }
$miniRacerVersion = $miniRacerPins[0].Substring('mini-racer=='.Length)
& $python -m pip install --disable-pip-version-check --no-deps --force-reinstall "mini-racer==$miniRacerVersion"
$miniRacerDll = Join-Path $miniRacer "mini_racer.dll"
$miniRacerIcu = Join-Path $miniRacer "icudtl.dat"
foreach ($resource in @($miniRacerDll, $miniRacerIcu)) {
  if (-not (Test-Path -LiteralPath $resource -PathType Leaf)) { throw "缺少 PyMiniRacer 运行资源：$resource" }
}
& $python -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --name panic-engine `
  --paths $scripts `
  --add-data "${static};a_share_panic_index/web/static" `
  --add-data "${defaults};a_share_panic_index" `
  --add-data "${settings};config" `
  --add-data "${riskModel};config" `
  --collect-all akshare `
  --collect-all py_mini_racer `
  --add-binary "${miniRacerDll};py_mini_racer" `
  --add-data "${miniRacerIcu};py_mini_racer" `
  --collect-all baostock `
  --collect-all exchange_calendars `
  --collect-all numpy `
  --collect-all pandas `
  --collect-all scipy `
  --collect-all sklearn `
  --collect-all joblib `
  --collect-all threadpoolctl `
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
  engineVersion = "4.0"
  clientVersion = "3.0.0"
  databaseVersion = 6
  apiVersion = "2"
} | ConvertTo-Json
$manifestPath = Join-Path $dist "panic-engine\build-manifest.json"
[IO.File]::WriteAllText($manifestPath, $manifest + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
