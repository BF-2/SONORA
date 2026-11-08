[CmdletBinding()]
param(
    [ValidateSet('Menu', 'Base', 'M6', 'All', 'None')][string]$Models = 'Menu',
    [ValidateSet('Auto', 'Origin', 'Mirror')][string]$Source = 'Auto',
    [ValidateSet('Auto', 'CPU', 'CU128')][string]$TorchBackend = 'Auto',
    [switch]$NonInteractive,
    [switch]$Plan,
    [switch]$DownloadOnly,
    [switch]$ConfigureOnly
)

$ErrorActionPreference = 'Stop'
$sonoraRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sonoraManifest = Get-Content -LiteralPath (Join-Path $sonoraRoot 'engine-envs\portable-assets.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$sonoraTools = Join-Path $sonoraRoot '.tools'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'

if ($Models -eq 'Menu') {
    if ($NonInteractive -or $ConfigureOnly) { $Models = 'Base' }
    else {
        Write-Host ''
        Write-Host '声序 SONORA · 一键准备' -ForegroundColor Cyan
        Write-Host '  1  基础模型 + 运行环境'
        Write-Host '  2  M6 RVC 六角色 + 配套 index + RVC 基础资源'
        Write-Host '  3  全部模型 + 运行环境'
        Write-Host '  4  仅安装 / 修复运行环境'
        Write-Host '  0  退出'
        do { $sonoraChoice = Read-Host '请选择 [1/2/3/4/0]' } until ($sonoraChoice -match '^[0-4]$')
        if ($sonoraChoice -eq '0') { exit 0 }
        $Models = @{ '1' = 'Base'; '2' = 'M6'; '3' = 'All'; '4' = 'None' }[$sonoraChoice]
    }
}

if ($Plan) {
    $sonoraGroups = switch ($Models) { 'Base' { @('base','rvc-base') }; 'M6' { @('m6','rvc-base') }; 'All' { @('base','rvc-base','m6') }; default { @() } }
    $sonoraFiles = @($sonoraManifest.files | Where-Object { @($_.groups | Where-Object { $_ -in $sonoraGroups }).Count -gt 0 })
    $sonoraFiles | Select-Object id, path, @{Name='MB';Expression={[Math]::Round($_.bytes / 1MB, 2)}} | Format-Table -AutoSize
    Write-Host ('模型总量：{0:N2} GB；已有且校验通过的文件会跳过。' -f (($sonoraFiles | Measure-Object bytes -Sum).Sum / 1GB))
    exit 0
}

New-Item -ItemType Directory -Path $sonoraTools -Force | Out-Null
$sonoraLock = $null
$sonoraHttp = $null
try {
    try { $sonoraLock = [IO.File]::Open((Join-Path $sonoraTools 'bootstrap.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw '另一个声序准备任务正在运行。请等待该任务完成。' }
    Add-Type -AssemblyName System.Net.Http
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $sonoraHandler = [Net.Http.HttpClientHandler]::new()
    if ($env:HTTPS_PROXY) { $sonoraHandler.Proxy = [Net.WebProxy]::new($env:HTTPS_PROXY) }
    $sonoraHttp = [Net.Http.HttpClient]::new($sonoraHandler)
    $sonoraHttp.Timeout = [TimeSpan]::FromMinutes(20)
    $sonoraHttp.DefaultRequestHeaders.UserAgent.ParseAdd('SONORA-Setup/1')

    function Get-SonoraSourceOrder([string[]]$Urls) {
        if ($Source -eq 'Origin') { return $Urls }
        if ($Source -eq 'Mirror') { return @($Urls | Select-Object -Skip 1) + @($Urls[0]) }
        $sonoraScored = foreach ($sonoraUrl in $Urls) {
            $sonoraWatch = [Diagnostics.Stopwatch]::StartNew()
            $sonoraCancel = [Threading.CancellationTokenSource]::new(5000)
            $sonoraRequest = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Head, $sonoraUrl)
            $sonoraResponse = $null
            try {
                $sonoraResponse = $sonoraHttp.SendAsync($sonoraRequest, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $sonoraCancel.Token).GetAwaiter().GetResult()
                $sonoraScore = if ($sonoraResponse.IsSuccessStatusCode) { $sonoraWatch.Elapsed.TotalSeconds } else { [double]::PositiveInfinity }
            } catch { $sonoraScore = [double]::PositiveInfinity }
            finally { if ($sonoraResponse) { $sonoraResponse.Dispose() }; $sonoraRequest.Dispose(); $sonoraCancel.Dispose() }
            [pscustomobject]@{ Url = $sonoraUrl; Score = $sonoraScore }
        }
        return @($sonoraScored | Sort-Object Score | ForEach-Object Url)
    }

    $sonoraUv = Join-Path $sonoraTools 'bin\uv.exe'
    if ((Test-Path -LiteralPath $sonoraUv) -and
        (Get-FileHash -LiteralPath $sonoraUv -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sonoraManifest.uv.executable_sha256) {
        Move-Item -LiteralPath $sonoraUv -Destination ($sonoraUv + '.previous-' + [Guid]::NewGuid().ToString('N'))
    }
    if (-not (Test-Path -LiteralPath $sonoraUv)) {
        Write-Host '[准备下载工具] 自动检测源站与镜像站…'
        $sonoraCache = Join-Path $sonoraTools 'cache'
        New-Item -ItemType Directory -Path $sonoraCache -Force | Out-Null
        $sonoraArchive = Join-Path $sonoraCache 'uv.zip'
        $sonoraUvReady = (Test-Path -LiteralPath $sonoraArchive) -and ((Get-FileHash -LiteralPath $sonoraArchive -Algorithm SHA256).Hash.ToLowerInvariant() -eq $sonoraManifest.uv.sha256)
        if (-not $sonoraUvReady) {
            $sonoraUrls = @($sonoraManifest.uv.url) + @($sonoraManifest.mirrors.github | ForEach-Object { $_.TrimEnd('/') + '/' + $sonoraManifest.uv.url })
            foreach ($sonoraUrl in (Get-SonoraSourceOrder $sonoraUrls)) {
                $sonoraResponse = $null; $sonoraStream = $null; $sonoraOutput = $null
                try {
                    Write-Host ('[下载 uv] ' + ([Uri]$sonoraUrl).Host)
                    $sonoraResponse = $sonoraHttp.GetAsync($sonoraUrl, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
                    $null = $sonoraResponse.EnsureSuccessStatusCode()
                    $sonoraStream = $sonoraResponse.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
                    $sonoraPartial = $sonoraArchive + '.part'
                    $sonoraOutput = [IO.File]::Create($sonoraPartial)
                    $sonoraBuffer = New-Object byte[] (1024 * 1024)
                    $sonoraReceived = 0L
                    while ($true) {
                        $sonoraReadTimeout = [Threading.CancellationTokenSource]::new(20000)
                        try { $sonoraRead = $sonoraStream.ReadAsync($sonoraBuffer, 0, $sonoraBuffer.Length, $sonoraReadTimeout.Token).GetAwaiter().GetResult() }
                        finally { $sonoraReadTimeout.Dispose() }
                        if ($sonoraRead -le 0) { break }
                        $sonoraOutput.Write($sonoraBuffer, 0, $sonoraRead)
                        $sonoraReceived += $sonoraRead
                        if ($sonoraReceived -gt $sonoraManifest.uv.bytes) { throw '下载大小超出固定版本' }
                        Write-Progress -Activity '准备下载工具' -Status ('{0:N1} MB' -f ($sonoraReceived / 1MB))
                    }
                    $sonoraOutput.Dispose(); $sonoraOutput = $null
                    if ((Get-FileHash -LiteralPath $sonoraPartial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sonoraManifest.uv.sha256) { throw '下载校验未通过' }
                    Move-Item -LiteralPath $sonoraPartial -Destination $sonoraArchive -Force
                    $sonoraUvReady = $true
                    break
                } catch { Write-Host '[切换 uv 下载源]' }
                finally {
                    if ($sonoraOutput) { $sonoraOutput.Dispose() }; if ($sonoraStream) { $sonoraStream.Dispose() }; if ($sonoraResponse) { $sonoraResponse.Dispose() }
                    Write-Progress -Activity '准备下载工具' -Completed
                }
            }
        }
        if (-not $sonoraUvReady) { throw 'uv 下载未完成，请检查网络后重新运行。' }
        $sonoraUvStage = Join-Path $sonoraTools ('staging\uv-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $sonoraUvStage -Force | Out-Null
        $sonoraZip = [IO.Compression.ZipFile]::OpenRead($sonoraArchive)
        try {
            foreach ($sonoraEntry in $sonoraZip.Entries) {
                if ($sonoraEntry.Name -eq 'uv.exe') {
                    $sonoraExtracted = Join-Path $sonoraUvStage 'uv.exe'
                    [IO.Compression.ZipFileExtensions]::ExtractToFile($sonoraEntry, $sonoraExtracted, $false)
                    New-Item -ItemType Directory -Path (Split-Path -Parent $sonoraUv) -Force | Out-Null
                    Copy-Item -LiteralPath $sonoraExtracted -Destination $sonoraUv
                }
            }
        } finally { $sonoraZip.Dispose() }
        if (-not (Test-Path -LiteralPath $sonoraUv)) { throw 'uv 压缩包中缺少 Windows 程序。' }
        if ((Get-FileHash -LiteralPath $sonoraUv -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sonoraManifest.uv.executable_sha256) { throw 'uv 程序校验未通过。' }
    }

    $env:UV_CACHE_DIR = Join-Path $sonoraTools 'cache\uv'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $sonoraTools 'python-base'
    $env:UV_NO_CONFIG = '1'
    function Find-SonoraPython {
        if (Test-Path -LiteralPath $env:UV_PYTHON_INSTALL_DIR) {
            foreach ($sonoraDirectory in (Get-ChildItem -LiteralPath $env:UV_PYTHON_INSTALL_DIR -Directory | Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) })) {
                $sonoraCandidate = Join-Path $sonoraDirectory.FullName 'python.exe'
                if (-not (Test-Path -LiteralPath $sonoraCandidate)) { continue }
                $sonoraVersion = & $sonoraCandidate -I -c 'import platform; print(platform.python_version())' 2>$null
                if ($LASTEXITCODE -eq 0 -and $sonoraVersion -eq $sonoraManifest.python_version) { return $sonoraCandidate }
            }
        }
        return $null
    }
    $sonoraBasePython = Find-SonoraPython
    if (-not $sonoraBasePython) {
        Write-Host '[安装项目内 Python] 自动检测源站与镜像站…'
        $sonoraPythonOrigins = @('https://releases.astral.sh/github/python-build-standalone/releases/download', 'https://github.com/astral-sh/python-build-standalone/releases/download')
        $sonoraPythonOrigins += @($sonoraManifest.mirrors.github | ForEach-Object { $_.TrimEnd('/') + '/https://github.com/astral-sh/python-build-standalone/releases/download' })
        $sonoraPythonProbes = @($sonoraPythonOrigins | ForEach-Object { $_ + '/' + $sonoraManifest.python_probe })
        foreach ($sonoraProbe in (Get-SonoraSourceOrder $sonoraPythonProbes)) {
            $sonoraMirrorBase = $sonoraProbe.Substring(0, $sonoraProbe.Length - $sonoraManifest.python_probe.Length - 1)
            & $sonoraUv python install $sonoraManifest.python_version --install-dir $env:UV_PYTHON_INSTALL_DIR --no-bin --no-registry --mirror $sonoraMirrorBase
            if ($LASTEXITCODE -eq 0) { $sonoraBasePython = Find-SonoraPython; if ($sonoraBasePython) { break } }
            Write-Host '[切换 Python 下载源]'
        }
        if (-not $sonoraBasePython) { throw '项目内 Python 安装未完成，请重新运行以继续。' }
    }
    $sonoraArguments = @((Join-Path $PSScriptRoot 'portable_setup.py'), '--root', $sonoraRoot, '--uv', $sonoraUv, '--base-python', $sonoraBasePython,
                         '--models', $Models.ToLowerInvariant(), '--source', $Source.ToLowerInvariant(), '--torch-backend', $TorchBackend.ToLowerInvariant())
    if ($DownloadOnly) { $sonoraArguments += '--download-only' }
    if ($ConfigureOnly) { $sonoraArguments += '--configure-only' }
    & $sonoraBasePython @sonoraArguments
    if ($LASTEXITCODE -ne 0) { throw '准备过程尚未完成。保留的下载进度将在下次运行时继续。' }
} finally {
    if ($sonoraHttp) { $sonoraHttp.Dispose() }
    if ($sonoraLock) { $sonoraLock.Dispose() }
}
