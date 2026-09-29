$ErrorActionPreference = 'Stop'
$entry = Join-Path $PSScriptRoot 'run_converter.py'
$candidates = @(@{ Path = (Join-Path $PSScriptRoot '.venv/Scripts/python.exe'); Prefix = @() })

if ($env:VIRTUAL_ENV) {
    $candidates += @{ Path = (Join-Path $env:VIRTUAL_ENV 'Scripts/python.exe'); Prefix = @() }
}
$localRoot = Join-Path $env:LOCALAPPDATA 'Programs/Python'
if (Test-Path -LiteralPath $localRoot) {
    foreach ($folder in (Get-ChildItem -LiteralPath $localRoot -Directory | Sort-Object Name -Descending)) {
        $candidates += @{ Path = (Join-Path $folder.FullName 'python.exe'); Prefix = @() }
    }
}
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    $candidates += @{ Path = $python.Source; Prefix = @() }
}
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    $candidates += @{ Path = $launcher.Source; Prefix = @('-3.12') }
}

foreach ($candidate in $candidates) {
    $command = $candidate.Path
    $prefix = $candidate.Prefix
    if (-not (Test-Path -LiteralPath $command)) { continue }
    try {
        $null = & $command @prefix -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) {
            & $command @prefix $entry @args
            exit $LASTEXITCODE
        }
    } catch {
        continue
    }
}

Write-Error 'Python 3.12 or newer is required. Install Python, then run: pip install -r requirements.txt'
exit 1
