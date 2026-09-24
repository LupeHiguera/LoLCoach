<#
.SYNOPSIS
  Build the static read-only demo in demo/ and, with -Publish, upload it to S3 + CloudFront.

.DESCRIPTION
  1. python export_demo.py  -> demo/data/*.json (scrubbed snapshot; skip with -SkipExport)
  2. Copies the review UI (index.html, app.js, style.css, fonts/) into demo/ and marks it
     <html data-mode="demo">, so app.js reads data/*.json and disables every save.
  3. Only with -Publish: aws s3 sync to a private bucket and a CloudFront invalidation.

  Read demo/data/ before publishing. It is built from real matches.

.EXAMPLE
  .\deploy_demo.ps1                       # build only; preview with: python -m http.server -d demo 8766
.EXAMPLE
  .\deploy_demo.ps1 -Publish -Bucket my-lolcoach-demo -DistributionId E123ABC
#>
param(
  [int]$Count = 5,
  [switch]$SkipExport,
  [switch]$Publish,
  [string]$Bucket,
  [string]$DistributionId
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$web  = Join-Path $root 'review_web'
$demo = Join-Path $root 'demo'

if (-not $SkipExport) {
  python (Join-Path $root 'export_demo.py') --count $Count --out (Join-Path $demo 'data')
  if ($LASTEXITCODE -ne 0) { throw 'export_demo.py failed; nothing was built.' }
}
if (-not (Test-Path (Join-Path $demo 'data\matches.json'))) { throw 'demo\data\matches.json is missing. Run without -SkipExport.' }

# UI files only: never review_web\mock (dev fixtures) or anything else in the folder.
New-Item -ItemType Directory -Force (Join-Path $demo 'fonts') | Out-Null
Copy-Item (Join-Path $web 'app.js'), (Join-Path $web 'style.css') $demo -Force
Copy-Item (Join-Path $web 'fonts\*') (Join-Path $demo 'fonts') -Force

# No server sends headers here, so the CSP goes in a meta tag (frame-ancestors is header-only).
$csp = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:"
$html = Get-Content (Join-Path $web 'index.html') -Raw -Encoding UTF8
if ($html -notmatch '<html lang="en">') { throw 'index.html has no <html lang="en"> tag to mark as demo.' }
$html = $html.Replace('<html lang="en">', '<html lang="en" data-mode="demo">')
$html = $html.Replace('<meta charset="utf-8">', "<meta charset=`"utf-8`">`n<meta http-equiv=`"Content-Security-Policy`" content=`"$csp`">")
[IO.File]::WriteAllText((Join-Path $demo 'index.html'), $html, (New-Object Text.UTF8Encoding $false))

$files = Get-ChildItem $demo -Recurse -File
Write-Host "Built demo\ ($($files.Count) files). Preview: python -m http.server -d demo 8766, then open http://127.0.0.1:8766/"

if (-not $Publish) { return }
if (-not $Bucket -or -not $DistributionId) { throw '-Publish needs -Bucket and -DistributionId.' }
aws s3 sync $demo "s3://$Bucket" --delete --exclude '*.ps1'
if ($LASTEXITCODE -ne 0) { throw 'aws s3 sync failed.' }
aws cloudfront create-invalidation --distribution-id $DistributionId --paths '/*' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'CloudFront invalidation failed.' }
Write-Host "Published to s3://$Bucket and invalidated $DistributionId."
