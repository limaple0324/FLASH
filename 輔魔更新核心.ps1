param([string]$InstallDirectory = "")
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
if ([string]::IsNullOrWhiteSpace($InstallDirectory)) { $InstallDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path }
$InstallDirectory = [IO.Path]::GetFullPath($InstallDirectory)
$IndexUrl = 'https://raw.githubusercontent.com/limaple0324/FLASH/release/fumagic/release-index.json'
$ExpectedProduct = '輔魔'
$tmpRoot = Join-Path $env:TEMP ('fumagic-update-' + [Guid]::NewGuid().ToString('N'))
$stageExe = Join-Path $tmpRoot '輔魔.exe'
$targetExe = Join-Path $InstallDirectory '輔魔.exe'
$backupExe = Join-Path $tmpRoot '輔魔.exe.backup'
New-Item -ItemType Directory -Force $tmpRoot | Out-Null
try {
  $index = Invoke-RestMethod -Uri $IndexUrl -Method Get -TimeoutSec 20 -Headers @{'Cache-Control'='no-cache'}
  if ([int]$index.schema -ne 1 -or [string]$index.product -ne $ExpectedProduct -or [string]$index.channel -ne 'release/fumagic') { throw '更新索引身分不正確。' }
  $sha = ([string]$index.sha256).ToLowerInvariant()
  if ($sha -notmatch '^[0-9a-f]{64}$') { throw '更新索引 SHA-256 無效。' }
  $size = [int64]$index.size
  if ($size -le 0) { throw '更新索引檔案大小無效。' }
  $assetUrl = [string]$index.asset_url
  if ($assetUrl -notmatch '^https://github\.com/limaple0324/FLASH/releases/download/fumagic-[A-Za-z0-9._-]+/.+$') { throw '更新下載位置不受信任。' }
  Invoke-WebRequest -Uri $assetUrl -OutFile $stageExe -UseBasicParsing -TimeoutSec 300
  if ((Get-Item -LiteralPath $stageExe).Length -ne $size) { throw '下載檔案大小不符。' }
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $stageExe).Hash.ToLowerInvariant()
  if ($actual -ne $sha) { throw '下載檔案 SHA-256 不符。' }
  $bytes = [IO.File]::ReadAllBytes($stageExe)
  if ($bytes.Length -lt 2 -or $bytes[0] -ne 0x4d -or $bytes[1] -ne 0x5a) { throw '下載檔案不是 Windows PE 執行檔。' }
  Get-Process -Name '輔魔' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
  if (Test-Path -LiteralPath $targetExe) { Copy-Item -LiteralPath $targetExe -Destination $backupExe -Force }
  try {
    Copy-Item -LiteralPath $stageExe -Destination $targetExe -Force
    $installed = (Get-FileHash -Algorithm SHA256 -LiteralPath $targetExe).Hash.ToLowerInvariant()
    if ($installed -ne $sha) { throw '安裝後 SHA-256 驗證失敗。' }
  } catch {
    if (Test-Path -LiteralPath $backupExe) { Copy-Item -LiteralPath $backupExe -Destination $targetExe -Force }
    throw
  }
  Write-Host '輔魔更新完成。'
  Write-Host "SHA-256: $sha"
  exit 0
} catch {
  Write-Host ('輔魔更新失敗：' + $_.Exception.Message) -ForegroundColor Red
  exit 1
} finally {
  Remove-Item -LiteralPath $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
}
