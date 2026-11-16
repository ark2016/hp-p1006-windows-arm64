param(
    [Parameter(Mandatory=$true)][string]$InputFile,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [ValidateRange(1,1000)][int]$MaxPages = 100,
    [ValidateSet(300,600)][int]$RasterDpi = 600
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapEncoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
$script:AsTaskResult = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
$script:AsTaskAction = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
} | Select-Object -First 1
function Wait-Result($Operation, [Type]$ResultType) {
    $task = $script:AsTaskResult.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $task.Wait(120000)) { throw 'PDF operation timed out' }
    return $task.Result
}
function Wait-Action($Operation) {
    $task = $script:AsTaskAction.Invoke($null, @($Operation))
    if (-not $task.Wait(120000)) { throw 'PDF rendering timed out' }
}
function Get-PngSize([IO.Stream]$Stream) {
    $Stream.Position = 0
    $header = New-Object byte[] 24
    $read = 0
    while ($read -lt $header.Length) {
        $count = $Stream.Read($header, $read, $header.Length - $read)
        if ($count -eq 0) { throw 'PDF renderer produced a truncated PNG' }
        $read += $count
    }
    if ([BitConverter]::ToString($header, 0, 8) -ne '89-50-4E-47-0D-0A-1A-0A' -or
        [Text.Encoding]::ASCII.GetString($header, 12, 4) -ne 'IHDR') {
        throw 'PDF renderer did not produce a PNG'
    }
    $width = [uint32]($header[16] * 16777216 + $header[17] * 65536 + $header[18] * 256 + $header[19])
    $height = [uint32]($header[20] * 16777216 + $header[21] * 65536 + $header[22] * 256 + $header[23])
    if ($width -eq 0 -or $height -eq 0) { throw 'PDF renderer produced an empty PNG' }
    return [PSCustomObject]@{ Width = $width; Height = $height }
}
function Render-Png($Page, $Options, [IO.Stream]$Stream) {
    $randomStream = $null
    try {
        $randomStream = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($Stream)
        Wait-Action ($Page.RenderToStreamAsync($randomStream, $Options))
        return Get-PngSize $Stream
    } finally {
        if ($null -ne $randomStream) { $randomStream.Dispose() }
    }
}
$sourcePath = (Resolve-Path -LiteralPath $InputFile).Path
$targetPath = [IO.Path]::GetFullPath($OutputDirectory)
[IO.Directory]::CreateDirectory($targetPath) | Out-Null
$file = Wait-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($sourcePath)) ([Windows.Storage.StorageFile])
$document = Wait-Result ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
if ($document.PageCount -gt $MaxPages) { throw "PDF exceeds $MaxPages pages" }
$pixelScaleX = $null
$pixelScaleY = $null
for ($index = 0; $index -lt $document.PageCount; $index++) {
    $page = $document.GetPage($index)
    try {
        # DestinationWidth/Height are DIPs, not physical pixels:
        # https://learn.microsoft.com/uwp/api/windows.data.pdf.pdfpagerenderoptions.destinationwidth
        # Measure this process's actual scale with a tiny disposable render;
        # no desktop DPI or process/thread DPI awareness setting is changed.
        if ($null -eq $pixelScaleX) {
            $probeOptions = New-Object Windows.Data.Pdf.PdfPageRenderOptions
            $probeOptions.DestinationWidth = 64
            $probeOptions.DestinationHeight = 64
            $probeOptions.BitmapEncoderId = [Windows.Graphics.Imaging.BitmapEncoder]::PngEncoderId
            $probeOptions.IsIgnoringHighContrast = $true
            $probeStream = New-Object IO.MemoryStream
            try { $probeSize = Render-Png $page $probeOptions $probeStream }
            finally { $probeStream.Dispose() }
            $pixelScaleX = $probeSize.Width / 64.0
            $pixelScaleY = $probeSize.Height / 64.0
            if ($pixelScaleX -lt 0.5 -or $pixelScaleX -gt 8 -or
                $pixelScaleY -lt 0.5 -or $pixelScaleY -gt 8) {
                throw "Unexpected PDF display scale: ${pixelScaleX}x${pixelScaleY}"
            }
        }
        # Rasterize at the selected physical square DPI, then use 1200x600.
        # Bound the output to A4 even on high-DPI displays and oversized PDFs.
        $a4Width = 4960.0 * $RasterDpi / 600.0
        $a4Height = 7016.0 * $RasterDpi / 600.0
        $scale = [Math]::Min($RasterDpi / 96.0, [Math]::Min($a4Width / $page.Size.Width, $a4Height / $page.Size.Height))
        $pixelWidth = [Math]::Max(1, [Math]::Round($page.Size.Width * $scale))
        $pixelHeight = [Math]::Max(1, [Math]::Round($page.Size.Height * $scale))
        $options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
        $options.DestinationWidth = [uint32][Math]::Max(1, [Math]::Floor($pixelWidth / $pixelScaleX))
        $options.DestinationHeight = [uint32][Math]::Max(1, [Math]::Floor($pixelHeight / $pixelScaleY))
        $options.BitmapEncoderId = [Windows.Graphics.Imaging.BitmapEncoder]::PngEncoderId
        $options.IsIgnoringHighContrast = $true
        $outputPath = Join-Path $targetPath ('page-{0:D4}.png' -f ($index + 1))
        $bounded = $false
        for ($attempt = 0; $attempt -lt 3; $attempt++) {
            # Only replace a file that this invocation created on attempt 0.
            $mode = if ($attempt -eq 0) { [IO.FileMode]::CreateNew } else { [IO.FileMode]::Create }
            $stream = [IO.File]::Open($outputPath, $mode)
            try { $actual = Render-Png $page $options $stream }
            finally { $stream.Dispose() }
            if ($actual.Width -le $pixelWidth -and $actual.Height -le $pixelHeight) {
                $bounded = $true
                break
            }
            # Handles fractional custom display scales and a DPI change
            # between calibration and rendering, without passing a huge PNG on.
            $correction = [Math]::Max($actual.Width / $pixelWidth, $actual.Height / $pixelHeight)
            $options.DestinationWidth = [uint32][Math]::Max(1, [Math]::Floor($options.DestinationWidth / $correction))
            $options.DestinationHeight = [uint32][Math]::Max(1, [Math]::Floor($options.DestinationHeight / $correction))
        }
        if (-not $bounded) { throw "PDF render exceeds its pixel limit: $($actual.Width)x$($actual.Height)" }
        Write-Output $outputPath
    } finally { $page.Dispose() }
}
