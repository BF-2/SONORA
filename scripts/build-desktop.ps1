[CmdletBinding()]
param([switch]$SkipWebBuild, [ValidateSet('sonora','sonora-harmony','sonora-specialist','sonora-recovery')][string]$BinaryName = 'sonora-recovery')

$ErrorActionPreference = 'Stop'
$sonoraRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sonoraWeb = Join-Path $sonoraRoot 'apps\web'
$sonoraDesktop = Join-Path $sonoraRoot 'apps\desktop'
if (-not $SkipWebBuild) {
    if (-not (Test-Path -LiteralPath (Join-Path $sonoraWeb 'node_modules'))) {
        & npm.cmd --prefix $sonoraWeb ci
        if ($LASTEXITCODE -ne 0) { throw 'Failed to install the locked web dependencies.' }
    }
    & npm.cmd --prefix $sonoraWeb run build
    if ($LASTEXITCODE -ne 0) { throw 'Web build failed.' }
}
if (-not (Test-Path -LiteralPath (Join-Path $sonoraWeb 'dist\index.html'))) {
    throw 'A completed web build is required before compiling the desktop application.'
}
Push-Location $sonoraDesktop
try {
    & cargo build --release --locked --offline --bin $BinaryName
    if ($LASTEXITCODE -ne 0) { throw 'Desktop compilation failed. Verify MSVC and Windows SDK prerequisites.' }
    $sonoraTree = & cargo tree --locked --offline --target x86_64-pc-windows-msvc --prefix none --format '{p}|{l}'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to record actual desktop dependency licenses.' }
    $sonoraComponents = @{}
    foreach ($sonoraLine in $sonoraTree) {
        if ($sonoraLine -match '^(?<name>\S+) v(?<version>[^\s]+).*?\|(?<license>.*)$') {
            $sonoraKey = $Matches.name + '@' + $Matches.version
            $sonoraLicense = $Matches.license.Replace(' (*)', '').Trim()
            $sonoraComponents[$sonoraKey] = [ordered]@{
                name = $Matches.name
                version = $Matches.version
                license_expression = $sonoraLicense
                source = $(if ($Matches.name -eq 'sonora-desktop') { 'local' } else { 'crates.io' })
                purl = ('pkg:cargo/' + $Matches.name + '@' + $Matches.version)
            }
        }
    }
    $sonoraInventory = [ordered]@{
        schema_version = 1
        target = 'x86_64-pc-windows-msvc'
        lockfile_sha256 = (Get-FileHash Cargo.lock -Algorithm SHA256).Hash.ToLowerInvariant()
        components = @($sonoraComponents.Values | Sort-Object name, version)
    } | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText((Join-Path $sonoraDesktop 'dependencies.windows.json'), $sonoraInventory, [Text.UTF8Encoding]::new($false))
} finally {
    Pop-Location
}
$sonoraBinary = Join-Path $sonoraDesktop ('target\release\' + $BinaryName + '.exe')
$sonoraDigest = (Get-FileHash -LiteralPath $sonoraBinary -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "Built: $sonoraBinary"
Write-Host "SHA256: $sonoraDigest"
Write-Host 'The executable embeds the editor. It uses this project .venv and separate engine resources; it does not bundle model weights.'
