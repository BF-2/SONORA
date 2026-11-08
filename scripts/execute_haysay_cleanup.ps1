$ErrorActionPreference = 'Stop'
$sonoraRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$auditRoot = [IO.Path]::GetFullPath('E:\voice\maintenance\sonora_haysay_20261104\retired-evidence')
$planPath = Join-Path $sonoraRoot 'engine-envs\haysay\cleanup-plan.json'
$plan = Get-Content -LiteralPath $planPath -Raw -Encoding UTF8 | ConvertFrom-Json
$failed = @()
$moved = 0
foreach ($item in $plan) {
    $source = [IO.Path]::GetFullPath($item.source)
    $dest = [IO.Path]::GetFullPath($item.destination)
    if (-not $source.StartsWith($sonoraRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Source outside SONORA: $source" }
    if (-not $dest.StartsWith($auditRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Destination outside audit: $dest" }
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Move-Item -LiteralPath $source -Destination $dest -ErrorAction Stop
        $moved++
    } catch {
        $failed += [PSCustomObject]@{ source=$source; destination=$dest; error=$_.Exception.Message }
    }
}
$failedJson = ConvertTo-Json -InputObject @($failed) -Depth 5
Set-Content -LiteralPath (Join-Path $sonoraRoot 'engine-envs\haysay\pending-cleanup.json') -Value $failedJson -Encoding UTF8
Write-Output "Moved $moved retired files out of SONORA; pending $($failed.Count)"
