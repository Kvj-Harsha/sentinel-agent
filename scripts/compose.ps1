# Canonical entry point for Sentinel's Compose stack with local-safe overrides.
# Resolve paths from this script so it also works outside the repository root.
# Forward Docker Compose arguments unchanged, for example: config --quiet, ps.
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeRoot = Join-Path $repoRoot 'extras'
$composeArguments = @('--project-directory', $composeRoot, '--project-name', 'sentinel-agent')
foreach ($fileName in @(
    'docker-compose.alerting.yml',
    'docker-compose.app.yml',
    'docker-compose.loki.yml',
    'docker-compose.alloy.yml',
    'docker-compose.local.yml'
)) {
    $composeArguments += @('--file', (Join-Path $composeRoot $fileName))
}

if ($args.Count -gt 0 -and $args[0] -eq 'preflight') {
    if ($args.Count -ne 1) { throw 'Usage: .\scripts\compose.ps1 preflight' }
    $pythonPath = Join-Path $repoRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw 'Create the repository .venv and install requirements.txt before running preflight.'
    }
    & docker compose @composeArguments config --format json | & $pythonPath (Join-Path $PSScriptRoot 'preflight.py')
} elseif ($args.Count -eq 0) {
    & docker compose @composeArguments --help
} else {
    & docker compose @composeArguments @args
}
exit $LASTEXITCODE
