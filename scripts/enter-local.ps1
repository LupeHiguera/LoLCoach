# Dot-source from the project root: . .\scripts\enter-local.ps1
$lolCoachRoot = Split-Path -Parent $PSScriptRoot
$lolCoachPython = Join-Path $lolCoachRoot '.venv\Scripts'
if (-not (Test-Path -LiteralPath (Join-Path $lolCoachPython 'python.exe'))) {
    throw 'Create .venv first with a working Python: python -m venv .venv'
}
$lolCoachBins = @($lolCoachPython)
$lolCoachFFmpeg = Join-Path $lolCoachRoot 'data\tools\ffmpeg\bin'
if (Test-Path -LiteralPath (Join-Path $lolCoachFFmpeg 'ffmpeg.exe')) {
    $lolCoachBins += $lolCoachFFmpeg
}
$lolCoachLms = Join-Path $env:USERPROFILE '.lmstudio\bin'
if (Test-Path -LiteralPath (Join-Path $lolCoachLms 'lms.exe')) {
    $lolCoachBins += $lolCoachLms
}
$env:PATH = ($lolCoachBins -join ';') + ';' + $env:PATH
$env:VIRTUAL_ENV = Join-Path $lolCoachRoot '.venv'
$env:PYTHONIOENCODING = 'utf-8'
Write-Host 'LoLCoach Python, FFmpeg and LM Studio paths are ready in this terminal.'
