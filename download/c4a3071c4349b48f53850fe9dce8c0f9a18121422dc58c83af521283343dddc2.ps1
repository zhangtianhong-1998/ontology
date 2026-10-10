param(
    [Parameter(Mandatory = $true)]
    [string]$Bundle,
    [switch]$WithDataHubLite
)

$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$bundlePath = (Resolve-Path $Bundle).Path
$wheelhouse = Join-Path $bundlePath "wheelhouse"
$requirements = Join-Path $bundlePath "requirements-route2-locked.txt"
$datahubRequirements = Join-Path $bundlePath "requirements-datahub-lite-locked.txt"
$route2Env = Join-Path $project ".venv"
$datahubEnv = Join-Path $project ".venv-datahub"
$route2Wheel = Get-ChildItem $wheelhouse -Filter "ontology_route2-0.3.0-*.whl" | Select-Object -First 1
if (-not (Test-Path $requirements) -or -not $route2Wheel) {
    throw "Bundle is missing requirements-route2-locked.txt or the route2 wheel"
}
if ($WithDataHubLite -and -not (Test-Path $datahubRequirements)) {
    throw "Bundle is missing requirements-datahub-lite-locked.txt"
}

if (Test-Path $route2Env) { throw "Existing .venv found; use a clean checkout or remove it explicitly" }
if ($WithDataHubLite -and (Test-Path $datahubEnv)) { throw "Existing .venv-datahub found; use a clean checkout or remove it explicitly" }
py -3.14 -m venv $route2Env
if ($LASTEXITCODE -ne 0) { throw "Python 3.14 venv creation failed" }
$route2Python = Join-Path $route2Env "Scripts\python.exe"
& $route2Python -m pip install --no-index --find-links $wheelhouse -r $requirements $route2Wheel.FullName
if ($LASTEXITCODE -ne 0) { throw "Route2 offline installation failed" }
& $route2Python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Route2 dependency check failed" }

if ($WithDataHubLite) {
    py -3.14 -m venv $datahubEnv
    if ($LASTEXITCODE -ne 0) { throw "DataHub Lite venv creation failed" }
    $datahubPython = Join-Path $datahubEnv "Scripts\python.exe"
    & $datahubPython -m pip install --no-index --find-links $wheelhouse -r $datahubRequirements "acryl-datahub[datahub-lite]==1.7.0.12"
    if ($LASTEXITCODE -ne 0) { throw "DataHub Lite offline installation failed" }
    & $datahubPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "DataHub Lite dependency check failed" }
}

Write-Output "Route2 installed in $route2Env"
if ($WithDataHubLite) { Write-Output "DataHub Lite installed separately in $datahubEnv" }
