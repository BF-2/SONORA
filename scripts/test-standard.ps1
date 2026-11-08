[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$sonoraRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sonoraPython = Join-Path $sonoraRoot '.venv\Scripts\python.exe'
$sonoraResources = Get-Content -LiteralPath (Join-Path $sonoraRoot 'engine-envs\resources.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$sonoraWorkerPython = $sonoraResources.applio.python
$sonoraSuites = @(
    'test_core', 'test_domain', 'test_review_regressions', 'test_harmony_delivery',
    'test_harmony_review', 'test_editor_improvements', 'test_editor_followup', 'test_voicing_versions', 'test_timeline_visibility',
    'test_workflow_monitoring', 'test_separation_versions', 'test_standard_workflow',
    'test_specialist_integration', 'test_model_catalog', 'test_model_catalog_api',
    'test_haysay_profile', 'test_rvc_parameters_unit', 'test_workers_integrity', 'test_workers_svs'
) | ForEach-Object { 'tests/' + $_ + '.py' }
Push-Location $sonoraRoot
try {
    & $sonoraPython -m pytest @sonoraSuites -q --tb=short
    if ($LASTEXITCODE -ne 0) { throw 'Core regression failed.' }
    foreach ($sonoraSuite in @('test_workers_unit.py', 'test_harmony_worker_unit.py')) {
        & $sonoraWorkerPython -m unittest discover -s tests -p $sonoraSuite -v
        if ($LASTEXITCODE -ne 0) { throw "Worker regression failed: $sonoraSuite" }
    }
    & npm.cmd --prefix apps/web run build
    if ($LASTEXITCODE -ne 0) { throw 'TypeScript/Vite build failed.' }
} finally {
    Pop-Location
}
