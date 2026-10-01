# Project-local binaries from the Windows distributor linked by ffmpeg.org/download.html.
$ErrorActionPreference = 'Stop'
$lolCoachRoot = Split-Path -Parent $PSScriptRoot
$lolCoachTools = Join-Path $lolCoachRoot 'data\tools'
$lolCoachArchive = Join-Path $lolCoachTools 'ffmpeg-release-essentials.zip'
$lolCoachBase = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
New-Item -ItemType Directory -Force -Path $lolCoachTools | Out-Null
$ProgressPreference = 'SilentlyContinue'
Invoke-WebRequest -UseBasicParsing -Uri $lolCoachBase -OutFile $lolCoachArchive
$lolCoachExpected = (Invoke-WebRequest -UseBasicParsing -Uri "$lolCoachBase.sha256").Content.Trim().Split(' ')[0]
$lolCoachActual = (Get-FileHash -LiteralPath $lolCoachArchive -Algorithm SHA256).Hash
if ($lolCoachActual -ne $lolCoachExpected) { throw 'FFmpeg archive checksum mismatch.' }
$lolCoachExtract = Join-Path $lolCoachTools ('ffmpeg-extract-' + [guid]::NewGuid().ToString('N'))
Expand-Archive -LiteralPath $lolCoachArchive -DestinationPath $lolCoachExtract
$lolCoachSource = Get-ChildItem -LiteralPath $lolCoachExtract -Directory | Select-Object -First 1
$lolCoachBin = Join-Path $lolCoachTools 'ffmpeg\bin'
New-Item -ItemType Directory -Force -Path $lolCoachBin | Out-Null
foreach ($lolCoachExe in @('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe')) {
    Copy-Item -LiteralPath (Join-Path $lolCoachSource.FullName "bin\$lolCoachExe") -Destination $lolCoachBin
}
Copy-Item -LiteralPath (Join-Path $lolCoachSource.FullName 'LICENSE') -Destination (Join-Path $lolCoachTools 'ffmpeg')
Write-Host 'FFmpeg and FFprobe installed under data/tools/ffmpeg/bin; checksum verified.'
