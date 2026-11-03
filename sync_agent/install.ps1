# Cài DST Sync agent cho người dùng hiện tại (không cần quyền Administrator).
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms

$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Utf8   = New-Object Text.UTF8Encoding($false)
$Config = [IO.File]::ReadAllText((Join-Path $Here 'config.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
$Target = Join-Path $env:LOCALAPPDATA "DSTSync\$($Config.cluster)"
$Vbs    = Join-Path ([Environment]::GetFolderPath('Startup')) "DSTSync_$($Config.cluster).vbs"

Write-Host "Cài đồng bộ cho server: $($Config.name)  ($($Config.server))"
Write-Host ''

# --- Chọn thư mục ---
$klei = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Klei\DoNotStarveTogether'
$dialog = New-Object Windows.Forms.FolderBrowserDialog
$dialog.Description = "Chọn thư mục để đồng bộ với server `"$($Config.name)`".`n" +
    "Muốn chơi bằng Host Game: chọn (hoặc tạo) Cluster_N trong Documents\Klei\DoNotStarveTogether\<dãy số>\."
$dialog.ShowNewFolderButton = $true
if (Test-Path -LiteralPath $klei) { $dialog.SelectedPath = $klei }

$old = Join-Path $Target 'config.json'
if (Test-Path -LiteralPath $old) {
    $prev = ([IO.File]::ReadAllText($old, [Text.Encoding]::UTF8) | ConvertFrom-Json).folder
    if ($prev) { $dialog.SelectedPath = $prev }
}

# Cửa sổ ẩn luôn nổi trên cùng để hộp thoại không bị che sau console.
$owner = New-Object Windows.Forms.Form -Property @{ TopMost = $true }
if ($dialog.ShowDialog($owner) -ne [Windows.Forms.DialogResult]::OK) { Write-Host 'Đã huỷ.'; exit 1 }
$Folder = $dialog.SelectedPath

if ((Split-Path -Leaf $Folder) -match '^\d+$' -or $Folder.TrimEnd('\') -ieq $klei) {
    [Windows.Forms.MessageBox]::Show($owner, "Đây là thư mục chứa nhiều thế giới, không phải một thế giới.`nHãy chọn hoặc tạo một thư mục con (ví dụ Cluster_9).", 'DST Sync') | Out-Null
    exit 1
}

if (Get-ChildItem -LiteralPath $Folder -Force | Select-Object -First 1) {
    $answer = [Windows.Forms.MessageBox]::Show($owner,
        "Thư mục đã có dữ liệu:`n$Folder`n`nLần đồng bộ đầu tiên sẽ lấy thế giới từ server và GHI ĐÈ thư mục này " +
        "(nội dung hiện tại được sao lưu vào $Target\backups).`n`nTiếp tục?",
        'DST Sync', [Windows.Forms.MessageBoxButtons]::YesNo, [Windows.Forms.MessageBoxIcon]::Warning)
    if ($answer -ne [Windows.Forms.DialogResult]::Yes) { Write-Host 'Đã huỷ.'; exit 1 }
}

# --- Dừng agent cũ (nếu cài lại) ---
$pidFile = Join-Path $Target 'agent.pid'
if (Test-Path -LiteralPath $pidFile) {
    $p = Get-Process -Id ([int](Get-Content -LiteralPath $pidFile)) -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -like 'powershell*') { Stop-Process -Id $p.Id -Force }
}

# --- Chép file ---
New-Item -ItemType Directory -Force -Path $Target | Out-Null
Copy-Item -LiteralPath (Join-Path $Here 'dstsync.ps1'), (Join-Path $Here 'uninstall.ps1') -Destination $Target -Force
$Config | Add-Member -NotePropertyName folder -NotePropertyValue $Folder -Force
[IO.File]::WriteAllText((Join-Path $Target 'config.json'), (ConvertTo-Json $Config), $Utf8)
[IO.File]::WriteAllText((Join-Path $Target 'GO_CAI_DAT.bat'),
    "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0uninstall.ps1`"`r`npause`r`n", [Text.Encoding]::ASCII)

# --- Tự chạy khi đăng nhập Windows (ẩn cửa sổ) ---
$script = Join-Path $Target 'dstsync.ps1'
$cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$script"""
[IO.File]::WriteAllText($Vbs, "CreateObject(""WScript.Shell"").Run ""$($cmd.Replace('"', '""'))"", 0, False`r`n", [Text.Encoding]::Unicode)

Start-Process wscript.exe -ArgumentList "`"$Vbs`""

Write-Host 'Đã cài xong!'
Write-Host "  Thư mục đồng bộ : $Folder"
Write-Host "  Agent cài ở     : $Target  (log: dstsync.log, sao lưu: backups\)"
Write-Host '  Agent tự chạy khi đăng nhập Windows và đang chạy nền từ bây giờ.'
Write-Host '  Lần đồng bộ đầu tiên sẽ bắt đầu trong vài giây. Xem trạng thái ở web: tab Thế giới.'
Write-Host "  Gỡ cài đặt: chạy GO_CAI_DAT.bat trong $Target"
