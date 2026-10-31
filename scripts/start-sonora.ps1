[CmdletBinding()]
param(
    [switch]$Browser,
    [switch]$NoOpenBrowser,
    [ValidateRange(0, 86400)][int]$RunForSeconds = 0
)

$ErrorActionPreference = 'Stop'
$sonoraRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sonoraPython = Join-Path $sonoraRoot '.venv\Scripts\python.exe'
$sonoraExe = Join-Path $sonoraRoot 'apps\desktop\target\release\sonora.exe'
$sonoraHarmonyExe = Join-Path $sonoraRoot 'apps\desktop\target\release\sonora-harmony.exe'
if (Test-Path -LiteralPath $sonoraHarmonyExe) { $sonoraExe = $sonoraHarmonyExe }
$sonoraSpecialistExe = Join-Path $sonoraRoot 'apps\desktop\target\release\sonora-specialist.exe'
if (Test-Path -LiteralPath $sonoraSpecialistExe) { $sonoraExe = $sonoraSpecialistExe }
$sonoraRecoveryExe = Join-Path $sonoraRoot 'apps\desktop\target\release\sonora-recovery.exe'
if (Test-Path -LiteralPath $sonoraRecoveryExe) { $sonoraExe = $sonoraRecoveryExe }
$sonoraPort = 8765

if (-not $Browser -and (Test-Path -LiteralPath $sonoraExe)) {
    $oldRoot = $env:SONORA_ROOT
    try {
        $env:SONORA_ROOT = $sonoraRoot
        # This is the interactive application the user requested; only helpers are hidden.
        Start-Process -FilePath $sonoraExe -WorkingDirectory $sonoraRoot -WindowStyle Normal -Wait
    } finally {
        $env:SONORA_ROOT = $oldRoot
    }
    exit
}

if (-not (Test-Path -LiteralPath $sonoraPython)) {
    throw 'Missing .venv\Scripts\python.exe. Prepare the SONORA core environment using README first.'
}
if (-not (Test-Path -LiteralPath (Join-Path $sonoraRoot 'apps\web\dist\index.html'))) {
    throw 'Missing web build. Run npm --prefix apps/web ci and npm --prefix apps/web run build first.'
}

$sonoraListener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $sonoraPort)
try {
    $sonoraListener.Start()
} catch {
    throw "Port $sonoraPort is occupied. Close the other SONORA session or inspect the owning program. No existing process was stopped."
} finally {
    $sonoraListener.Stop()
}

$sonoraLogs = Join-Path $sonoraRoot 'runtime\logs'
New-Item -ItemType Directory -Path $sonoraLogs -Force | Out-Null
$sonoraRandom = New-Object byte[] 32
$sonoraRng = [Security.Cryptography.RandomNumberGenerator]::Create()
$sonoraRng.GetBytes($sonoraRandom)
$sonoraRng.Dispose()
$sonoraToken = [BitConverter]::ToString($sonoraRandom).Replace('-', '').ToLowerInvariant()
$oldToken = $env:SONORA_SESSION_TOKEN
$oldRoot = $env:SONORA_ROOT
$oldUtf8 = $env:PYTHONUTF8
$sonoraProcess = $null

try {
    $env:SONORA_SESSION_TOKEN = $sonoraToken
    $env:SONORA_ROOT = $sonoraRoot
    $env:PYTHONUTF8 = '1'
    $sonoraProcess = Start-Process -FilePath $sonoraPython -ArgumentList @('-m', 'services.orchestrator.app', '--port', [string]$sonoraPort) -WorkingDirectory $sonoraRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $sonoraLogs 'browser-service.stdout.log') -RedirectStandardError (Join-Path $sonoraLogs 'browser-service.stderr.log')
    $env:SONORA_SESSION_TOKEN = $oldToken
    $env:SONORA_ROOT = $oldRoot
    $env:PYTHONUTF8 = $oldUtf8
    $sonoraDeadline = [DateTime]::UtcNow.AddSeconds(45)
    $sonoraReady = $false
    while ([DateTime]::UtcNow -lt $sonoraDeadline) {
        $sonoraProcess.Refresh()
        if ($sonoraProcess.HasExited) { throw 'Core service exited. See runtime/logs/browser-service.stderr.log.' }
        try {
            $sonoraHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$sonoraPort/api/v1/health" -TimeoutSec 2
            if ($sonoraHealth.protocol_version -ne 1) { throw 'Protocol mismatch: expected version 1.' }
            $null = Invoke-RestMethod -Uri "http://127.0.0.1:$sonoraPort/api/v1/projects" -Headers @{ Authorization = "Bearer $sonoraToken" } -TimeoutSec 2
            $sonoraReady = $true
            break
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if (-not $sonoraReady) { throw 'Core service authentication handshake timed out. See runtime/logs/browser-service.stderr.log.' }
    Write-Host "SONORA is ready at http://127.0.0.1:$sonoraPort. All audio processing stays local."
    if (-not $NoOpenBrowser) {
        # The fragment is never sent to the HTTP server. The UI consumes/removes it.
        Start-Process "http://127.0.0.1:$sonoraPort/#token=$sonoraToken"
    }
    if ($RunForSeconds -gt 0) {
        Start-Sleep -Seconds $RunForSeconds
    } else {
        $null = Read-Host 'Keep this window open. Press Enter to stop this SONORA browser session'
    }
} finally {
    $env:SONORA_SESSION_TOKEN = $oldToken
    $env:SONORA_ROOT = $oldRoot
    $env:PYTHONUTF8 = $oldUtf8
    if ($null -ne $sonoraProcess) {
        $sonoraProcess.Refresh()
        if (-not $sonoraProcess.HasExited) {
            # Only the process handle started by this script is eligible for cleanup.
            $sonoraTaskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
            $cleanup = Start-Process -FilePath $sonoraTaskkill -ArgumentList @('/PID', [string]$sonoraProcess.Id, '/T', '/F') -WindowStyle Hidden -Wait -PassThru
        }
        $sonoraProcess.Dispose()
    }
    $sonoraToken = $null
}
