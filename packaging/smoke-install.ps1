$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$installer = Join-Path $root 'desktop\dist\FundAndPanic-Setup-2.0.2-x64.exe'
$portable = Join-Path $root 'desktop\dist\FundAndPanic-Portable-2.0.2-x64.exe'
$installPath = Join-Path $root ('output\install-test\安装目录-' + [guid]::NewGuid().ToString('N'))
if (-not [IO.Path]::GetFullPath($installPath).StartsWith($root + [IO.Path]::DirectorySeparatorChar)) { throw '安装测试目录越界' }
$markerName = '卸载保留验收-' + [guid]::NewGuid().ToString('N') + '.txt'
$markers = @()
foreach ($name in @('fund-and-panic-desktop', '基金与A股风险看板')) {
  $directory = Join-Path $env:APPDATA $name
  [IO.Directory]::CreateDirectory($directory) | Out-Null
  $marker = Join-Path $directory $markerName
  [IO.File]::WriteAllText($marker, '本文件仅用于验证卸载保留用户数据。', [Text.UTF8Encoding]::new($false))
  $markers += $marker
}
$installed = Start-Process -FilePath $installer -ArgumentList @('/S', "/D=$installPath") -WindowStyle Hidden -Wait -PassThru
if ($installed.ExitCode -ne 0) { throw "安装失败：$($installed.ExitCode)" }
$executable = Join-Path $installPath 'FundAndPanic.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw '安装后未找到客户端' }
& node (Join-Path $PSScriptRoot 'smoke-desktop.cjs') $executable
$uninstaller = Get-ChildItem -LiteralPath $installPath -Filter '*Uninstall*.exe' | Select-Object -First 1
if (-not $uninstaller) { throw '没有找到卸载程序' }
$uninstalled = Start-Process -FilePath $uninstaller.FullName -ArgumentList '/S' -WindowStyle Hidden -Wait -PassThru
if ($uninstalled.ExitCode -ne 0) { throw "卸载失败：$($uninstalled.ExitCode)" }
$deadline = [DateTime]::UtcNow.AddSeconds(30)
while ((Test-Path -LiteralPath $executable) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
if (Test-Path -LiteralPath $executable) { throw '卸载后可执行文件仍存在' }
foreach ($marker in $markers) {
  if (-not (Test-Path -LiteralPath $marker)) { throw '卸载误删用户数据标记' }
  Remove-Item -LiteralPath $marker
}
& node (Join-Path $PSScriptRoot 'smoke-desktop.cjs') $portable
$report = [ordered]@{ '时间' = [DateTime]::UtcNow.ToString('o'); '安装目录' = $installPath; '安装启动' = '通过'; '无系统Python路径' = '通过'; '卸载保留用户数据' = '通过'; '便携版启动' = '通过' }
$reportPath = Join-Path $root 'output\install-test\result.json'
[IO.File]::WriteAllText($reportPath, ($report | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
Write-Output '安装版、便携版、中文路径、无系统Python路径和卸载保留数据验收通过。'
