# DST Sync agent - đồng bộ 2 chiều một thư mục thế giới DST trên máy này với DST Web Panel.
# Chạy nền (được install.ps1 thêm vào Startup), cứ $PollSeconds hỏi server một lần xem có cần đồng bộ không.
# Chạy tay một lần:  powershell -ExecutionPolicy Bypass -File dstsync.ps1 -Once

param([switch]$Once)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem

$AgentVersion = 1
$PollSeconds  = 30
$BackupKeep   = 10
$GameProcesses = 'dontstarve_steam_x64', 'dontstarve_steam', 'dontstarve_dedicated_server_nullrenderer_x64', 'dontstarve_dedicated_server_nullrenderer'

$Here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config    = [IO.File]::ReadAllText((Join-Path $Here 'config.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
$Root      = $Config.folder.TrimEnd('\')
$BaseUrl   = $Config.server.TrimEnd('/') + '/sync-api/' + [Uri]::EscapeDataString($Config.cluster)
$BackupDir = Join-Path $Here 'backups'
$LogFile   = Join-Path $Here 'dstsync.log'
$Utf8      = New-Object Text.UTF8Encoding($false)

$script:SkipFiles = @('cluster_token.txt', 'webpanel.json', 'server.ini', 'server_log.txt', 'server_chat_log.txt', 'client_log.txt', 'client_chat_log.txt')
$script:SkipDirs  = @('backup')

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $Message
    try {
        if ((Test-Path -LiteralPath $LogFile) -and (Get-Item -LiteralPath $LogFile).Length -gt 1MB) {
            Move-Item -LiteralPath $LogFile -Destination "$LogFile.old" -Force
        }
        [IO.File]::AppendAllText($LogFile, $line + "`r`n", $Utf8)
    } catch {}
    if ($Once) { Write-Host $line }
}

# ---------------------------------------------------------------- HTTP

function Invoke-Api {
    param([string]$Path, $Body, [string]$InFile, [string]$OutFile)

    $req = [Net.HttpWebRequest]::Create($BaseUrl + $Path)
    $req.Method = 'POST'
    $req.Timeout = 900000
    $req.ReadWriteTimeout = 900000
    $req.Headers.Add('X-Sync-Token', $Config.token)

    try {
        if ($InFile) {
            $req.ContentType = 'application/zip'
            $req.AllowWriteStreamBuffering = $false
            $src = [IO.File]::OpenRead($InFile)
            try {
                $req.ContentLength = $src.Length
                $dst = $req.GetRequestStream()
                $src.CopyTo($dst)
                $dst.Close()
            } finally { $src.Close() }
        } else {
            $json = if ($null -eq $Body) { '{}' } else { ConvertTo-Json $Body -Depth 8 -Compress }
            $bytes = $Utf8.GetBytes($json)
            $req.ContentType = 'application/json; charset=utf-8'
            $req.ContentLength = $bytes.Length
            $dst = $req.GetRequestStream()
            $dst.Write($bytes, 0, $bytes.Length)
            $dst.Close()
        }

        $resp = $req.GetResponse()
    } catch {
        $ex = $_.Exception
        while ($ex -and -not ($ex -is [Net.WebException])) { $ex = $ex.InnerException }
        if ($ex -and $ex.Response) {
            $reader = New-Object IO.StreamReader($ex.Response.GetResponseStream(), $Utf8)
            $text = $reader.ReadToEnd()
            $reader.Close()
            $detail = $text
            try { $detail = ($text | ConvertFrom-Json).detail } catch {}
            throw "Server báo lỗi ($([int]$ex.Response.StatusCode)): $detail"
        }
        if ($ex) { throw "Không kết nối được server: $($ex.Message)" }
        throw
    }

    try {
        $stream = $resp.GetResponseStream()
        if ($OutFile) {
            $out = [IO.File]::Create($OutFile)
            try { $stream.CopyTo($out) } finally { $out.Close() }
            return $null
        }
        $reader = New-Object IO.StreamReader($stream, $Utf8)
        return ($reader.ReadToEnd() | ConvertFrom-Json)
    } finally { $resp.Close() }
}

# ---------------------------------------------------------------- File

function Test-Skip([string]$Rel) {
    $parts = $Rel.Split('/')
    if ($script:SkipFiles -contains $parts[-1]) { return $true }
    for ($i = 0; $i -lt $parts.Length - 1; $i++) {
        if ($script:SkipDirs -contains $parts[$i]) { return $true }
    }
    return $false
}

function Get-LocalPath([string]$Rel) {
    if ($Rel -match '(^|/)\.\.(/|$)' -or $Rel.Contains(':') -or $Rel.StartsWith('/')) { throw "Đường dẫn không hợp lệ: $Rel" }
    return Join-Path $Root ($Rel.Replace('/', '\'))
}

function Get-Manifest {
    $manifest = @{}
    if (-not (Test-Path -LiteralPath $Root)) { return $manifest }
    $prefix = (Get-Item -LiteralPath $Root).FullName.TrimEnd('\').Length + 1
    $epoch = New-Object DateTime(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)

    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force) {
        $rel = $file.FullName.Substring($prefix).Replace('\', '/')
        if (Test-Skip $rel) { continue }
        $manifest[$rel] = @{
            sha   = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLower()
            mtime = [int64]($file.LastWriteTimeUtc - $epoch).TotalSeconds
        }
    }
    return $manifest
}

function ConvertTo-Map($Obj) {
    $map = @{}
    if ($Obj) { foreach ($p in $Obj.PSObject.Properties) { $map[$p.Name] = @{ sha = $p.Value.sha; mtime = $p.Value.mtime } } }
    return $map
}

function Test-GameRunning {
    return [bool](Get-Process -Name $GameProcesses -ErrorAction SilentlyContinue)
}

function Remove-EmptyDirs {
    Get-ChildItem -LiteralPath $Root -Recurse -Directory -Force |
        Sort-Object { $_.FullName.Length } -Descending |
        Where-Object { -not (Get-ChildItem -LiteralPath $_.FullName -Force | Select-Object -First 1) } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
}

function Backup-Local([string]$Label) {
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $zip = Join-Path $BackupDir ('{0}_{1:yyyyMMdd-HHmmss}_{2}.zip' -f $Config.cluster, (Get-Date), $Label)
    [IO.Compression.ZipFile]::CreateFromDirectory($Root, $zip)
    Get-ChildItem -LiteralPath $BackupDir -Filter '*.zip' | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $BackupKeep | Remove-Item -Force
    Write-Log "Đã sao lưu thư mục local: $zip"
    return (Split-Path -Leaf $zip)
}

# ---------------------------------------------------------------- Đồng bộ

function Invoke-Pull($Server, $Local, [bool]$Backup) {
    $need  = @($Server.Keys | Where-Object { -not $Local.ContainsKey($_) -or $Local[$_].sha -ne $Server[$_].sha })
    $extra = @($Local.Keys | Where-Object { -not $Server.ContainsKey($_) })

    # server.ini (port) không thuộc tập đồng bộ, chỉ lấy khi máy local chưa có.
    foreach ($shard in 'Master', 'Caves') {
        if (($Server.Keys | Where-Object { $_.StartsWith("$shard/") }) -and -not (Test-Path -LiteralPath (Get-LocalPath "$shard/server.ini"))) {
            $need += "$shard/server.ini"
        }
    }

    if ($need.Count -eq 0 -and $extra.Count -eq 0) { return }

    if ($Backup -and $Local.Count -gt 0) { Backup-Local 'truoc-khi-lay-tu-server' | Out-Null }

    if ($need.Count -gt 0) {
        $tmp = Join-Path $env:TEMP ("dstsync_{0}_{1}.zip" -f $PID, [Guid]::NewGuid().ToString('N'))
        try {
            Invoke-Api '/download' @{ paths = $need } -OutFile $tmp | Out-Null
            $zip = [IO.Compression.ZipFile]::OpenRead($tmp)
            try {
                foreach ($entry in $zip.Entries) {
                    if ($entry.FullName.EndsWith('/')) { continue }
                    $dest = Get-LocalPath $entry.FullName
                    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
                    [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
                    # Giờ trong zip là giờ địa phương của server; đặt lại theo lúc đồng bộ cho nhất quán.
                    [IO.File]::SetLastWriteTimeUtc($dest, [DateTime]::UtcNow)
                }
            } finally { $zip.Dispose() }
        } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }

    foreach ($rel in $extra) { Remove-Item -LiteralPath (Get-LocalPath $rel) -Force }
    Remove-EmptyDirs
    Write-Log ("Lấy từ server: {0} file mới/đổi, xoá {1} file." -f $need.Count, $extra.Count)
}

function Invoke-Push($Server, $Local) {
    $changed = @($Local.Keys | Where-Object { -not $Server.ContainsKey($_) -or $Server[$_].sha -ne $Local[$_].sha })
    $tmp = Join-Path $env:TEMP ("dstsync_{0}_{1}.zip" -f $PID, [Guid]::NewGuid().ToString('N'))

    try {
        $fs = [IO.File]::Open($tmp, [IO.FileMode]::Create)
        $zip = New-Object IO.Compression.ZipArchive($fs, [IO.Compression.ZipArchiveMode]::Create)
        try {
            $writer = New-Object IO.StreamWriter($zip.CreateEntry('__dstsync_manifest.json').Open(), $Utf8)
            $writer.Write((ConvertTo-Json $Local -Depth 4 -Compress))
            $writer.Close()
            foreach ($rel in $changed) {
                [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, (Get-LocalPath $rel), $rel, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
            }
        } finally { $zip.Dispose(); $fs.Close() }

        $res = Invoke-Api '/push' -InFile $tmp
    } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }

    Write-Log ("Đẩy lên server: {0} file. Server đã sao lưu bản cũ: {1}" -f $changed.Count, $res.backup)
    return (ConvertTo-Map $res.server)
}

function Send-Done([bool]$Ok, [string]$Action, [string]$Message, $Manifest) {
    $body = @{ ok = $Ok; action = $Action; message = $Message }
    if ($null -ne $Manifest) { $body.manifest = $Manifest }
    $res = Invoke-Api '/done' $body
    return $res.ok
}

function Invoke-Sync {
    if (Test-GameRunning) {
        Write-Log 'Game DST đang mở trên máy này, hoãn đồng bộ.'
        Send-Done $false 'skip' 'Game DST đang mở trên máy local, sẽ thử lại sau.' | Out-Null
        return
    }

    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $local = Get-Manifest
    $plan = Invoke-Api '/plan' @{ manifest = $local }
    Write-Log "Kế hoạch: $($plan.action) - $($plan.reason)"

    if ($plan.action -eq 'wait') {
        Send-Done $false 'wait' $plan.reason | Out-Null
        return
    }

    $server = ConvertTo-Map $plan.server

    if ($plan.action -eq 'pull') {
        Invoke-Pull $server $local $true
    } elseif ($plan.action -eq 'push') {
        $server = Invoke-Push $server $local
        # Server có thể chỉnh lại vài file (cluster.ini giữ port của server...), lấy lại để hai bên giống hệt.
        Invoke-Pull $server (Get-Manifest) $false
    }

    $ok = Send-Done $true $plan.action $plan.reason (Get-Manifest)
    Write-Log ($(if ($ok) { 'Đồng bộ xong.' } else { 'Đồng bộ chưa khớp, server sẽ cho thử lại sau.' }))
}

# ---------------------------------------------------------------- Main

$mutex = New-Object Threading.Mutex($false, "Local\DSTSync_$($Config.cluster)")
if (-not $mutex.WaitOne(0)) {
    if ($Once) { Write-Host 'Agent khác đang chạy cho server này.' }
    exit 0
}

Write-Log "Agent khởi động. Server: $($Config.server)  Cluster: $($Config.cluster)  Thư mục: $Root"
Set-Content -LiteralPath (Join-Path $Here 'agent.pid') -Value $PID

while ($true) {
    try {
        $poll = Invoke-Api '/poll' @{ version = $AgentVersion; folder = $Root; host = $env:COMPUTERNAME; game_running = (Test-GameRunning) }
        if ($poll.rules) {
            $script:SkipFiles = @($poll.rules.skip_files)
            $script:SkipDirs  = @($poll.rules.skip_dirs)
        }

        if ($poll.due -or $Once) {
            Write-Log "Bắt đầu đồng bộ ($(if ($poll.reason -eq 'manual') { 'bấm trên web' } elseif ($poll.reason) { 'theo lịch' } else { 'chạy tay' }))."
            try {
                Invoke-Sync
            } catch {
                $msg = "$_"
                Write-Log "Lỗi: $msg"
                try { Send-Done $false 'error' $msg | Out-Null } catch {}
            }
        }
    } catch {
        Write-Log "Lỗi: $_"
    }

    if ($Once) { break }
    Start-Sleep -Seconds $PollSeconds
}
