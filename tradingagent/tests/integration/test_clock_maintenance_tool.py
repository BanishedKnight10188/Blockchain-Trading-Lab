"""PowerShell syntax and privilege/restore guards; never touch live time settings."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/maintain-trading-clock.ps1"


def invoke(script):
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode("utf-8-sig", errors="replace").strip()


def test_persistent_clock_plan_is_read_only_and_uses_verified_peers():
    result = json.loads(invoke(f". '{TOOL}'; Get-TradeClockRepairPlan | ConvertTo-Json"))
    assert result["poll_seconds"] == 64 and result["first_install_requires_administrator"]
    assert not result["recurring_manual_commands"] and not result["modifies_firewall"]
    assert result["primary_peer"] == "time.cloudflare.com,0x8"
    assert result["fallback_peer"] == "time.windows.com,0xA"


def test_install_fails_before_mutation_without_admin():
    result = invoke(f"""
. '{TOOL}'
function Test-TradeClockAdministrator {{ return $false }}
function Set-Service {{ throw 'must_not_mutate' }}
try {{ Invoke-TradeClockMaintenance -Install; exit 2 }}
catch {{ $_.Exception.Message }}
""")
    assert result == "administrator_required"


def test_invalid_backup_is_rejected_before_restore():
    result = invoke(f"""
. '{TOOL}'
try {{ Test-TradeTimeBackup -Value ([pscustomobject]@{{scope='wrong'}}); exit 2 }}
catch {{ $_.Exception.Message }}
""")
    assert result == "invalid_time_configuration_backup"
