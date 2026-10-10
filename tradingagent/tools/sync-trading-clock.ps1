# Read-only by default. -Apply requires an administrator shell and performs
# one immediate correction of at most two seconds. No registry/service changes.
[CmdletBinding()]
param([switch]$Apply)

function ConvertFrom-TradeClockSamples {
    param([string[]]$Lines)
    $samples = @(
        foreach ($line in $Lines) {
            if ($line -match ',\s*([+-]\d+\.\d+)s\s*$') {
                [double]::Parse($Matches[1], [Globalization.CultureInfo]::InvariantCulture)
            }
        }
    )
    if ($samples.Count -ne 5) { throw 'five_valid_samples_required' }
    if (@($samples | Where-Object { [Math]::Abs($_) -gt 2 }).Count -gt 0) {
        throw 'clock_offset_exceeds_two_seconds'
    }
    $ordered = @($samples | Sort-Object)
    $spread = $ordered[4] - $ordered[0]
    if ($spread -gt 0.1) { throw 'unstable_ntp_samples' }
    return [pscustomobject]@{
        offset_seconds = $ordered[2]
        spread_seconds = $spread
        samples = $samples
        server = 'time.cloudflare.com'
    }
}

function Test-TradeClockAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $principal = [Security.Principal.WindowsPrincipal]::new($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    finally { $identity.Dispose() }
}

function Measure-TradeClockOffset {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $timeTool = Join-Path ([Environment]::GetFolderPath('System')) 'w32tm.exe'
    $lines = @(& $timeTool /stripchart /computer:time.cloudflare.com /dataonly /samples:5 /period:1)
    if ($LASTEXITCODE -ne 0) { throw 'ntp_measurement_failed' }
    if ($timer.Elapsed.TotalSeconds -gt 25) { throw 'ntp_measurement_too_slow' }
    return ConvertFrom-TradeClockSamples -Lines $lines
}

function Invoke-TradeClockSync {
    param([switch]$Apply)
    if ($Apply -and -not (Test-TradeClockAdministrator)) { throw 'administrator_required' }
    $before = Measure-TradeClockOffset
    $didApply = $false
    $afterOffset = $null
    if ($Apply) {
        if ([Math]::Abs($before.offset_seconds) -gt 0.005) {
            $target = (Get-Date).AddSeconds($before.offset_seconds)
            Set-Date -Date $target -ErrorAction Stop | Out-Null
            $didApply = $true
        }
        $after = Measure-TradeClockOffset
        $afterOffset = $after.offset_seconds
        if ([Math]::Abs($afterOffset) -gt 0.05) { throw 'clock_offset_not_converged' }
    }
    return [pscustomobject]@{
        measured_at_utc = [DateTime]::UtcNow.ToString('o')
        server = $before.server
        samples = $before.samples
        spread_seconds = $before.spread_seconds
        before_offset_seconds = $before.offset_seconds
        after_offset_seconds = $afterOffset
        applied = $didApply
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    try {
        Invoke-TradeClockSync -Apply:$Apply | ConvertTo-Json -Depth 3
    }
    catch {
        Write-Error $_.Exception.Message
        exit 1
    }
}
