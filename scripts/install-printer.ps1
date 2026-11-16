param(
    [ValidateRange(1024,65535)][int]$Port = 8631,
    [ValidateNotNullOrEmpty()][string]$Name = 'HP LaserJet P1006 (ARM bridge)'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$url = "http://127.0.0.1:$Port/ipp/print"
$driverName = 'Microsoft IPP Class Driver'
$stateDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) 'state'
$resultPath = Join-Path $stateDirectory 'windows-printer.json'
$result = [ordered]@{
    schema_version = 1
    time_utc = [DateTime]::UtcNow.ToString('o')
    status = 'checking'
    requested_name = $Name
    requested_url = $url
    created = $false
    driver_name = $null
    port_name = $null
    endpoint_observed = $null
    endpoint_source = $null
    endpoint_verified = $false
    queue_verified = $false
    known_registration = $null
}

function Save-Result {
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    $temporary = $resultPath + '.tmp'
    $json = $result | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $resultPath -Force
}

function Test-BridgeEndpoint([string]$Candidate) {
    try {
        $uri = [Uri]$Candidate
        return ($uri.IsAbsoluteUri -and $uri.Scheme -in @('http', 'ipp') -and
            $uri.Host -eq '127.0.0.1' -and $uri.Port -eq $Port -and
            $uri.AbsolutePath -ceq '/ipp/print' -and
            [string]::IsNullOrEmpty($uri.Query) -and
            [string]::IsNullOrEmpty($uri.Fragment) -and
            [string]::IsNullOrEmpty($uri.UserInfo))
    } catch {
        return $false
    }
}

function Read-Queue {
    # Do not suppress CIM access failures or interpret them as "not installed".
    # Enumeration plus literal comparison also avoids wildcard queue names.
    $matches = @(Get-Printer -ErrorAction Stop | Where-Object { $_.Name -ieq $Name })
    if ($matches.Count -gt 1) { throw "Multiple queues match '$Name'. No queue was changed." }
    if ($matches.Count -eq 1) { return $matches[0] }
    return $null
}

function Read-Endpoint($PrinterPort) {
    # PrintManagement's MSFT_AdaptivePrinterPort exposes IppURL. DeviceURL can
    # identify WSD metadata instead, so it is not assumed to be the IPP URL.
    $property = $PrinterPort.PSObject.Properties['IppURL']
    if ($null -ne $property -and -not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        return @{ uri = [string]$property.Value; source = 'Get-PrinterPort.IppURL' }
    }
    try {
        $device = [Windows.Devices.Printers.IppPrintDevice, Windows.Devices.Printers, ContentType=WindowsRuntime]::FromPrinterName($Name)
        if ($null -ne $device -and $null -ne $device.PrinterUri) {
            return @{ uri = [string]$device.PrinterUri.AbsoluteUri; source = 'IppPrintDevice.PrinterUri' }
        }
    } catch {
        # WinRT availability/permissions vary; do not invent an endpoint.
        $result['endpoint_read_error'] = $_.Exception.Message
    }
    return $null
}

try {
    if (Test-Path -LiteralPath $resultPath) {
        try {
            $previous = Get-Content -LiteralPath $resultPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($null -ne $previous.known_registration) {
                $result.known_registration = $previous.known_registration
            }
        } catch {
            $result['previous_record_error'] = $_.Exception.Message
        }
    }

    # The caller owns the bridge lifetime. This script only creates/verifies a
    # queue; it never starts/elevates processes or sends a print job.
    $response = Invoke-WebRequest -Uri $url -UseBasicParsing -DisableKeepAlive -TimeoutSec 5
    if ($response.StatusCode -ne 200 -or $response.Content -notmatch 'HP LaserJet P1006 local IPP bridge') {
        throw "P1006 bridge is not responding at $url. Start Start-Bridge.cmd first."
    }
    if (-not (Get-Command Add-Printer -ErrorAction Stop).Parameters.ContainsKey('IppURL')) {
        throw 'This Windows PrintManagement module does not support Add-Printer -IppURL.'
    }

    $printer = Read-Queue
    if ($null -eq $printer) {
        Add-Printer -Name $Name -IppURL $url -ErrorAction Stop
        $result.created = $true
        $printer = Read-Queue
        if ($null -eq $printer) { throw 'Add-Printer returned, but the new queue is not present.' }
    }
    $result.driver_name = [string]$printer.DriverName
    $result.port_name = [string]$printer.PortName
    if ($printer.DriverName -ine $driverName) {
        throw "Queue '$Name' uses '$($printer.DriverName)', not '$driverName'. It was not overwritten. Choose another queue name or inspect it in Windows Settings."
    }
    if ([string]::IsNullOrWhiteSpace([string]$printer.PortName)) {
        throw "Queue '$Name' has no printer port. It was not changed."
    }
    $ports = @(Get-PrinterPort -ErrorAction Stop | Where-Object { $_.Name -ieq $printer.PortName })
    if ($ports.Count -ne 1) {
        throw "Cannot identify the queue's current port '$($printer.PortName)'. It was not changed."
    }
    $printerPort = $ports[0]
    $result['port_description'] = [string]$printerPort.Description
    $endpoint = Read-Endpoint $printerPort
    if ($null -ne $endpoint) {
        $result.endpoint_observed = $endpoint.uri
        $result.endpoint_source = $endpoint.source
        if (-not (Test-BridgeEndpoint $endpoint.uri)) {
            throw "Queue '$Name' points to '$($endpoint.uri)', not '$url'. It was not overwritten. Choose another queue name or inspect it in Windows Settings."
        }
        $result.endpoint_verified = $true
    } elseif ($result.created) {
        # The URL is known from this successful Add-Printer invocation, but
        # Windows did not expose a readback value. Keep that distinction.
        $result.endpoint_source = 'installation-request; readback unavailable'
    } else {
        $known = $result.known_registration
        if ($null -eq $known -or $known.name -ine $Name -or
            $known.driver_name -ine $driverName -or
            $known.port_name -ine $printer.PortName -or
            -not (Test-BridgeEndpoint ([string]$known.requested_url))) {
            throw "Queue '$Name' already exists, but its IPP endpoint cannot be verified and no matching registration record exists. It was not changed. Choose a different -Name or inspect its port in Windows Settings."
        }
        $result.endpoint_source = 'matching-saved-registration; live readback unavailable'
    }

    $result.queue_verified = $true
    $result.status = 'ready'
    $result.known_registration = [ordered]@{
        name = [string]$printer.Name
        driver_name = [string]$printer.DriverName
        port_name = [string]$printer.PortName
        requested_url = $url
        endpoint_verified = $result.endpoint_verified
        time_utc = [DateTime]::UtcNow.ToString('o')
    }
    Save-Result
    Write-Output "Queue ready: $($printer.Name)"
    Write-Output "Driver: $($printer.DriverName); port: $($printer.PortName)"
    if (-not $result.endpoint_verified) {
        Write-Output "Endpoint readback is unavailable; evidence: $($result.endpoint_source)."
    }
    Write-Output "Result: $resultPath"
    exit 0
} catch {
    $failure = $_
    $result.status = 'error'
    $result['error'] = $failure.Exception.Message
    $result['error_id'] = $failure.FullyQualifiedErrorId
    $result['error_category'] = [string]$failure.CategoryInfo.Category
    $result['hresult'] = '0x' + $failure.Exception.HResult.ToString('X8')
    $errorText = "$($result.error) $($result.error_id) $($result.hresult)"
    $denied = ($failure.CategoryInfo.Category -eq 'PermissionDenied' -or
        $errorText -match '80041003|80070005|AccessDenied|PermissionDenied|UnauthorizedAccess')
    if ($denied) {
        $result['next_step'] = 'Run Start-Bridge.cmd in the normal Windows user session. If Windows still denies printer management, keep the bridge running and run scripts/install-printer.ps1 from an administrator PowerShell. This script does not elevate automatically.'
    } else {
        $result['next_step'] = 'Inspect this error and state/bridge.log. Existing queues are never replaced automatically; use a different -Name if the name belongs to another printer.'
    }
    try { Save-Result } catch { [Console]::Error.WriteLine("Could not save result: " + $_.Exception.Message) }
    [Console]::Error.WriteLine("Printer setup failed: " + $failure.Exception.Message)
    [Console]::Error.WriteLine([string]$result.next_step)
    exit 1
}
