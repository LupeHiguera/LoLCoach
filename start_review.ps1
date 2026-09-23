$reviewPython = Get-Command python -ErrorAction SilentlyContinue
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundledPython) {
    & $bundledPython (Join-Path $PSScriptRoot 'review_app.py') @args
} elseif ($reviewPython) {
    & $reviewPython.Source (Join-Path $PSScriptRoot 'review_app.py') @args
} else {
    Write-Error 'Install Python 3.10 or newer, then run python review_app.py.'
}
