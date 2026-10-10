# Persistent W32Time configuration. Default invocation only prints the plan.
# Install/Restore require administrator privileges; no scheduled Set-Date loop,
# firewall change, domain policy override, package install or credential access.
[CmdletBinding(DefaultParameterSetName = 'Plan')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Install')][switch]$Install,
    [Parameter(Mandatory, ParameterSetName = 'Restore')][switch]$Restore
)

. (Join-Path $PSScriptRoot 'sync-trading-clock.ps1')

function Get-TradeClockRepairPlan {
    return [pscustomobject]@{
        scope = 'tradingagent-w32time-v1'
        service = 'W32Time'
        startup = 'Automatic'
        primary_peer = 'time.cloudflare.com,0x8'
        fallback_peer = 'time.windows.com,0xA'
        min_poll_log2 = 6
        max_poll_log2 = 6
        poll_seconds = 64
        first_install_requires_administrator = $true
        recurring_manual_commands = $false
        modifies_firewall = $false
        documentation = 'https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings'
    }
}

function Invoke-TradeTimeTool {
    param([string[]]$Arguments)
    $binary = Join-Path ([Environment]::GetFolderPath('System')) 'w32tm.exe'
    $lines = @(& $binary @Arguments)
    if ($LASTEXITCODE -ne 0) { throw 'w32time_command_failed' }
    return $lines
}

function Get-TradeTimeConfiguration {
    $base = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time'
    $parameters = Get-ItemProperty -LiteralPath "$base\Parameters" -ErrorAction Stop
    $config = Get-ItemProperty -LiteralPath "$base\Config" -ErrorAction Stop
    $client = Get-ItemProperty -LiteralPath "$base\TimeProviders\NtpClient" -ErrorAction Stop
    $service = Get-Service -Name W32Time -ErrorAction Stop
    return [pscustomobject]@{
        scope = 'tradingagent-w32time-v1'
        saved_at_utc = [DateTime]::UtcNow.ToString('o')
        type = [string]$parameters.Type
        peer = [string]$parameters.NtpServer
        min_poll = [int]$config.MinPollInterval
        max_poll = [int]$config.MaxPollInterval
        client_enabled = [int]$client.Enabled
        startup = [string]$service.StartType
        status = [string]$service.Status
    }
}

function Test-TradeTimeBackup {
    param($Value)
    if ($Value.scope -ne 'tradingagent-w32time-v1' -or
        $Value.type -notin @('NTP', 'NT5DS', 'AllSync', 'NoSync') -or
        $Value.peer.Length -gt 2048 -or
        $Value.min_poll -notin 0..15 -or $Value.max_poll -notin 0..15 -or
        $Value.min_poll -gt $Value.max_poll -or $Value.client_enabled -notin @(0, 1) -or
        $Value.startup -notin @('Automatic', 'Manual', 'Disabled') -or
        $Value.status -notin @('Running', 'Stopped')) {
        throw 'invalid_time_configuration_backup'
    }
}

function Invoke-TradeClockMaintenance {
    param([switch]$Install, [switch]$Restore)
    if ($Install -and $Restore) { throw 'choose_install_or_restore' }
    $plan = Get-TradeClockRepairPlan
    if (-not ($Install -or $Restore)) { return $plan }
    if (-not (Test-TradeClockAdministrator)) { throw 'administrator_required' }
    if ((Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).PartOfDomain) {
        throw 'domain_time_policy_requires_separate_configuration'
    }
    $managed = 'HKLM:\SOFTWARE\Policies\Microsoft\W32Time'
    if (Test-Path -LiteralPath $managed) { throw 'managed_time_policy_cannot_be_overridden' }
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    $backupPath = Join-Path $root 'output/verification/w32time-original.local.json'
    $reportPath = Join-Path $root 'output/verification/w32time-maintenance.local.json'
    $base = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time'
    if ($Restore) {
        $saved = Get-Content -LiteralPath $backupPath -Raw -ErrorAction Stop | ConvertFrom-Json
        Test-TradeTimeBackup -Value $saved
        Set-ItemProperty -LiteralPath "$base\Parameters" -Name Type -Value $saved.type
        Set-ItemProperty -LiteralPath "$base\Parameters" -Name NtpServer -Value $saved.peer
        Set-ItemProperty -LiteralPath "$base\Config" -Name MinPollInterval -Value $saved.min_poll
        Set-ItemProperty -LiteralPath "$base\Config" -Name MaxPollInterval -Value $saved.max_poll
        Set-ItemProperty -LiteralPath "$base\TimeProviders\NtpClient" -Name Enabled -Value $saved.client_enabled
        # Restore only the five settings and service state owned by this tool.
        Set-Service -Name W32Time -StartupType $saved.startup -ErrorAction Stop
        if ($saved.status -eq 'Running') {
            Restart-Service -Name W32Time -ErrorAction Stop
        } else { Stop-Service -Name W32Time -ErrorAction Stop }
        return [pscustomobject]@{ restored = $true; backup = $backupPath }
    }
    $before = Measure-TradeClockOffset  # Verify the selected primary, bounded <=2s.
    if (-not (Test-Path -LiteralPath $backupPath)) {
        $saved = Get-TradeTimeConfiguration
        Test-TradeTimeBackup -Value $saved
        $encoded = [Text.UTF8Encoding]::new($false).GetBytes(($saved | ConvertTo-Json -Depth 3))
        $file = [IO.File]::Open($backupPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
        try { $file.Write($encoded, 0, $encoded.Length) } finally { $file.Dispose() }
    } else {
        Test-TradeTimeBackup -Value (Get-Content -LiteralPath $backupPath -Raw | ConvertFrom-Json)
    }
    $report = [ordered]@{ plan = $plan; backup = $backupPath;
        before_offset_seconds = $before.offset_seconds; configured = $false;
        source_verified = $false; failure = $null }
    try {
        Set-Service -Name W32Time -StartupType Automatic -ErrorAction Stop
        Start-Service -Name W32Time -ErrorAction Stop
        Set-ItemProperty -LiteralPath "$base\Config" -Name MinPollInterval -Value 6 -ErrorAction Stop
        Set-ItemProperty -LiteralPath "$base\Config" -Name MaxPollInterval -Value 6 -ErrorAction Stop
        Set-ItemProperty -LiteralPath "$base\TimeProviders\NtpClient" -Name Enabled -Value 1 -ErrorAction Stop
        $peers = $plan.primary_peer + ' ' + $plan.fallback_peer
        Invoke-TradeTimeTool -Arguments @('/config', "/manualpeerlist:$peers", '/syncfromflags:manual', '/update') | Out-Null
        Restart-Service -Name W32Time -ErrorAction Stop
        $report.configured = $true
        # One initial correction, followed by native continuous NTP discipline.
        $initial = Invoke-TradeClockSync -Apply
        $report.initial_offset_seconds = $initial.after_offset_seconds
        Invoke-TradeTimeTool -Arguments @('/resync', '/rediscover') | Out-Null
        $source = (Invoke-TradeTimeTool -Arguments @('/query', '/source')) -join ' '
        $report.source = $source.Trim()
        if ($source -notmatch 'time\.(cloudflare|windows)\.com') {
            throw 'service_has_not_selected_network_time'
        }
        $report.source_verified = $true
        $report.after = Get-TradeTimeConfiguration
        $report.after_offset_seconds = (Measure-TradeClockOffset).offset_seconds
        if ([Math]::Abs($report.after_offset_seconds) -gt 0.05) {
            throw 'clock_offset_not_converged'
        }
    }
    catch {
        $report.failure = $_.Exception.Message
        throw
    }
    finally {
        $report.measured_at_utc = [DateTime]::UtcNow.ToString('o')
        $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    }
    return [pscustomobject]$report
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    try { Invoke-TradeClockMaintenance -Install:$Install -Restore:$Restore | ConvertTo-Json -Depth 5 }
    catch { Write-Error $_.Exception.Message; exit 1 }
}
