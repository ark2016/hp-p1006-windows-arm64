[CmdletBinding()]
param(
    [string]$Compiler = 'C:\Strawberry\c\bin\gcc.exe'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$compilerCommand = Get-Command -Name $Compiler -ErrorAction Stop
$compilerPath = $compilerCommand.Source

Push-Location -LiteralPath $projectRoot
try {
    New-Item -ItemType Directory -Path 'bin' -Force | Out-Null
    $target = (& $compilerPath -dumpmachine).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not identify the compiler target.' }
    $compilerVersion = (& $compilerPath --version | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0) { throw 'Could not identify the compiler version.' }

    $common = @(
        '-std=gnu99', '-O2', '-Wall', '-static', '-static-libgcc',
        '-Wl,--no-insert-timestamp',
        '-Ithird_party/foo2zjs', '-Ithird_party/jbig'
    )
    $jbig = @('third_party/jbig/jbig.c', 'third_party/jbig/jbig_ar.c')
    foreach ($program in @('foo2xqx', 'xqxdecode', 'arm2hpdl')) {
        $arguments = $common + @("third_party/foo2zjs/$program.c")
        if ($program -ne 'arm2hpdl') { $arguments += $jbig }
        $arguments += @('-o', "bin/$program.exe")
        & $compilerPath @arguments
        if ($LASTEXITCODE -ne 0) { throw "Building $program failed (exit $LASTEXITCODE)." }
        Write-Host "Built bin/$program.exe ($target)"
    }

    $sourceFiles = Get-ChildItem -LiteralPath 'third_party/foo2zjs', 'third_party/jbig' -File -Recurse |
        Sort-Object FullName |
        ForEach-Object {
            [ordered]@{
                path = $_.FullName.Substring($projectRoot.Length + 1).Replace('\', '/')
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
    $binaries = foreach ($program in @('foo2xqx', 'xqxdecode', 'arm2hpdl')) {
        $binaryPath = "bin/$program.exe"
        [ordered]@{
            path = $binaryPath
            sha256 = (Get-FileHash -LiteralPath $binaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    [ordered]@{
        compiler = $compilerVersion
        target = $target
        note = 'These are user-mode converters. The default compiler produces x64 applications, which Windows 11 ARM64 can emulate; they are not native ARM64 print-driver DLLs.'
        sources = @($sourceFiles)
        binaries = @($binaries)
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath 'bin/build-info.json' -Encoding UTF8
}
finally {
    Pop-Location
}
