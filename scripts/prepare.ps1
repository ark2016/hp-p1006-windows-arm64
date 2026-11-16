param([string]$SevenZip = 'C:\Program Files\7-Zip\7z.exe')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$package = Join-Path $projectRoot 'ljP1000_P1500-HB-pnp-win64-ru.exe'
$expected = '6DC51BBBD0FF3E79A97603C34BC865F73FBAF529E3F54FFCEAAFE477F1EA8A61'
if ((Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash -ne $expected) {
    throw 'Unexpected HP package hash. Do not install or extract an unknown replacement automatically.'
}
if (-not (Test-Path -LiteralPath $SevenZip)) { throw '7-Zip is required to extract the original HP package.' }
& $SevenZip x $package '-o.vendor\hp' -y
if ($LASTEXITCODE -ne 0) { throw 'HP archive extraction failed' }
& $SevenZip x '.vendor\hp\drv64.cab' '-o.vendor\hp\driver' -y
if ($LASTEXITCODE -ne 0) { throw 'HP CAB extraction failed' }
& (Join-Path $PSScriptRoot 'build.ps1')
if (-not $?) { throw 'Converter build failed' }
python -m p1006 firmware --output state\P1006.dl
if ($LASTEXITCODE -ne 0) { throw 'Firmware preparation failed' }
python -m p1006 diagnose
if ($LASTEXITCODE -ne 0) { throw 'Diagnostics failed' }
