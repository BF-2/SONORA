[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Force,
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$DataRoot
)

$ErrorActionPreference = 'Stop'
$sonoraRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sonoraPython = Join-Path $sonoraRoot '.venv\Scripts\python.exe'
$sonoraPythons = @($sonoraPython, (Join-Path $sonoraRoot '.tools\runtimes\core\python.exe'))
if (Test-Path -LiteralPath $sonoraPythons[1]) { $sonoraPython = $sonoraPythons[1] }
if (-not $DataRoot) { $DataRoot = Join-Path $sonoraRoot 'runtime' }
$sonoraDb = Join-Path ([IO.Path]::GetFullPath($DataRoot)) 'sonora.sqlite'
$sonoraEndpoint = "http://127.0.0.1:$Port/api/v1"

function Test-SonoraCommand($Item) {
    if (-not $Item -or $Item.CommandLine -notmatch '(?i)(?:^|\s)-m\s+services\.orchestrator\.app(?:\s|$)') { return $false }
    $commandPort = 8765
    if ($Item.CommandLine -match '(?i)--port(?:=|\s+)(\d+)') { $commandPort = [int]$Matches[1] }
    return $commandPort -eq $Port
}

function Get-SonoraTargets {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if (-not $owners.Count) { return @() }
    $processes = @{}
    Get-CimInstance Win32_Process | ForEach-Object { $processes[[int]$_.ProcessId] = $_ }
    $desktopPaths = @('sonora', 'sonora-harmony', 'sonora-specialist', 'sonora-recovery') | ForEach-Object {
        Join-Path $sonoraRoot "apps\desktop\target\release\$_.exe"
    }
    $targets = @{}
    foreach ($owner in $owners) {
        $listener = $processes[[int]$owner]
        if (-not (Test-SonoraCommand $listener)) { throw "端口 $Port 由其它程序占用，未停止任何程序。" }
        $cursor = $listener
        $verified = $false
        $target = $null
        $seen = @{}
        for ($depth = 0; $depth -lt 8 -and $null -ne $cursor; $depth++) {
            if ($seen.ContainsKey([int]$cursor.ProcessId)) { break }
            $seen[[int]$cursor.ProcessId] = $true
            if ($cursor.ExecutablePath -in $sonoraPythons -and (Test-SonoraCommand $cursor)) {
                $verified = $true
                $target = $cursor
            }
            if ($verified -and $cursor.ExecutablePath -in $desktopPaths) {
                $target = $cursor
                break
            }
            $parent = $processes[[int]$cursor.ParentProcessId]
            if ($parent -and $parent.CreationDate -gt $cursor.CreationDate) { break }
            $cursor = $parent
        }
        if (-not $verified) { throw '无法确认服务属于本项目，未停止任何程序。' }
        $targets[[int]$target.ProcessId] = $target
    }
    return @($targets.Values)
}

function Get-SonoraHeaders {
    # New launcher tokens use Windows DPAPI. Explicit legacy session files remain supported.
    $paths = @((Join-Path $DataRoot 'launcher-session.json')) + @('six-stage-main-session.json', 'ui-session.json') | ForEach-Object {
        if ([IO.Path]::IsPathRooted($_)) { $_ } else { Join-Path $sonoraRoot "runtime\$_" }
    }
    foreach ($path in $paths) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $plain = $null
        try {
            $session = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
            $uri = [Uri]$session.endpoint
            if ($uri.Host -notin @('127.0.0.1', 'localhost') -or $uri.Port -ne $Port -or $uri.Scheme -ne 'http') { continue }
            if ($session.token_dpapi) {
                Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
                $secure = ConvertTo-SecureString $session.token_dpapi
                $plain = ([Net.NetworkCredential]::new('', $secure)).Password
            } else { $plain = $session.token }
            if (-not $plain) { continue }
            $headers = @{ Authorization = "Bearer $plain" }
            $null = Invoke-RestMethod -Uri "$sonoraEndpoint/jobs" -Headers $headers -TimeoutSec 3
            return $headers
        } catch {
            # Do not print credentials or HTTP request details.
        } finally { $plain = $null }
    }
    return $null
}

try {
    $targets = @(Get-SonoraTargets)
    if (-not $targets.Count) {
        Write-Host "声序服务未运行（端口 $Port），无需停止。"
        exit 0
    }
    $headers = if ($Force) { $null } else { Get-SonoraHeaders }
    $activeCount = $null
    if ($Force) { $activeCount = 0 }
    elseif ($headers) {
        $jobs = Invoke-RestMethod -Uri "$sonoraEndpoint/jobs" -Headers $headers -TimeoutSec 5
        $activeCount = @($jobs.jobs | Where-Object { $_.state -in @('queued', 'preparing', 'running') }).Count
    } elseif (Test-Path -LiteralPath $sonoraDb) {
        # Desktop/older launches may not have a saved token. Inspect their normal local DB read-only.
        $query = @'
import json,sqlite3,sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True,timeout=5) as db:
    print(sum(json.loads(row[0]).get('state') in ('queued','preparing','running') for row in db.execute('SELECT body FROM jobs')))
'@
        $count = & $sonoraPython -c $query $sonoraDb
        if ($LASTEXITCODE -eq 0 -and "$count" -match '^\d+$') { $activeCount = [int]$count }
    }
    if (-not $Force -and ($null -eq $activeCount -or $activeCount -gt 0)) {
        if ($null -eq $activeCount) { Write-Host '无法确认任务状态，本次未停止服务。' }
        else { Write-Host "有 $activeCount 个音频任务尚未结束，本次未停止服务。" }
        Write-Host '需要中断任务时，运行 scripts\stop-sonora.ps1 -Force。已保存的工程和音频会保留。'
        exit 2
    }
    $description = "仅停止本项目声序进程及其子进程：$($targets.ProcessId -join ', ')"
    if (-not $PSCmdlet.ShouldProcess($description, '停止 SONORA')) { exit 0 }
    if ($headers -and -not $Force) {
        try {
            $null = Invoke-RestMethod -Method Post -Uri "$sonoraEndpoint/dev/shutdown" -Headers $headers -TimeoutSec 5
            $deadline = [DateTime]::UtcNow.AddSeconds(10)
            while ([DateTime]::UtcNow -lt $deadline) {
                if (-not @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count) { break }
                Start-Sleep -Milliseconds 200
            }
        } catch {
            if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 409) {
                Write-Host '关闭前检测到任务仍在运行，本次保留服务。'
                exit 2
            }
            # Old launches have no graceful shutdown endpoint. Fall back to verified processes.
        }
    }
    foreach ($target in $targets) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($target.ProcessId)"
        if (-not $current) { continue }
        if ($current.CreationDate -ne $target.CreationDate -or $current.ExecutablePath -ine $target.ExecutablePath) {
            throw '进程身份已变化，拒绝停止可能复用此 PID 的其它程序。'
        }
        $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
        & $taskkill /PID $target.ProcessId /T /F | Out-Null
        if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue)) {
            throw '停止声序进程失败。'
        }
    }
    if (@(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count) {
        throw "端口 $Port 仍被占用，请检查当前程序。"
    }
    Write-Host '声序已停止。已保存的工程、模型、缓存和导出文件均保留。'
    exit 0
} catch {
    Write-Host ("停止失败：" + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
