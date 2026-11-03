# Gỡ DST Sync agent. Thư mục thế giới và các bản sao lưu được giữ nguyên.
$ErrorActionPreference = 'Stop'

$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = [IO.File]::ReadAllText((Join-Path $Here 'config.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
$Target = Join-Path $env:LOCALAPPDATA "DSTSync\$($Config.cluster)"
$Vbs    = Join-Path ([Environment]::GetFolderPath('Startup')) "DSTSync_$($Config.cluster).vbs"

$pidFile = Join-Path $Target 'agent.pid'
if (Test-Path -LiteralPath $pidFile) {
    $p = Get-Process -Id ([int](Get-Content -LiteralPath $pidFile)) -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -like 'powershell*') { Stop-Process -Id $p.Id -Force }
}

Remove-Item -LiteralPath $Vbs -Force -ErrorAction SilentlyContinue
foreach ($name in 'dstsync.ps1', 'config.json', 'agent.pid', 'GO_CAI_DAT.bat') {
    Remove-Item -LiteralPath (Join-Path $Target $name) -Force -ErrorAction SilentlyContinue
}

Write-Host "Đã gỡ agent đồng bộ cho server $($Config.name)."
Write-Host "Các bản sao lưu (nếu có) vẫn còn trong $Target\backups"
Remove-Item -LiteralPath (Join-Path $Target 'uninstall.ps1') -Force -ErrorAction SilentlyContinue
