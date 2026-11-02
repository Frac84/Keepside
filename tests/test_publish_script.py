"""Exercise the actual publisher's rollback and exit guard in isolated fixtures.

Only selected AST blocks run. No release is installed and no desktop/profile is
read or written; all process results and the vault lock are synthetic.
"""
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = r'''
param([string]$Publisher, [string]$Fixture, [string]$Case)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Publisher, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Publisher syntax failed' }
foreach ($name in @('Assert-ChildPath', 'Assert-PlainDirectory', 'Assert-AppExited')) {
    $node = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $true)
    . ([scriptblock]::Create($node.Extent.Text))
}
$InstallRoot = Join-Path $Fixture 'install'
$DataDirectory = Join-Path $Fixture 'data'
$stateRoot = Join-Path $Fixture 'state'
$runtimeBackup = Join-Path $stateRoot 'runtime-before'
foreach ($directory in @($InstallRoot, $DataDirectory, $runtimeBackup)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$appName = 'SyntheticApp'; $legacyName = 'SyntheticLegacy.exe'
$script:processes = @(); $script:liveOwner = $false
function Get-CimInstance { param($ClassName) return $script:processes }
function Get-Process { param($Id, $ErrorAction) if ($script:liveOwner -and $Id -eq 101) { [pscustomobject]@{Id=101} } }
if ($Case -in @('external-process', 'live-lock', 'stale-lock')) {
    if ($Case -eq 'external-process') {
        $script:processes = @([pscustomobject]@{Name='SyntheticApp.exe';ExecutablePath='Z:\another-copy\SyntheticApp.exe'})
    } else {
        "101`nSyntheticApp`nSyntheticHost" | Set-Content -LiteralPath (Join-Path $DataDirectory 'instance.lock')
        $script:liveOwner = $Case -eq 'live-lock'
    }
    $blocked = $false
    try { Assert-AppExited } catch { $blocked = $true }
    if ($blocked -ne ($Case -ne 'stale-lock')) { throw 'Exit guard failed' }
    'ok'; exit 0
}
$oldDirectory = Join-Path $InstallRoot '_internal'
New-Item -ItemType Directory -Path $oldDirectory | Out-Null
[IO.File]::WriteAllText((Join-Path $oldDirectory 'old.txt'), 'old runtime')
$movedDirectories = @(); $newDirectories = @()
if ($Case -eq 'partial-copy') {
    Move-Item -LiteralPath $oldDirectory -Destination (Join-Path $runtimeBackup '_internal')
    $movedDirectories = @('_internal'); $newDirectories = @('_internal')
    New-Item -ItemType Directory -Path $oldDirectory | Out-Null
    [IO.File]::WriteAllText((Join-Path $oldDirectory 'new.txt'), 'partial new runtime')
}
$copiedFiles = @(); $originalFileNames = @()
$bindingExisted = $false; $bindingFile = Join-Path $Fixture 'unused-profile.json'
$shortcutPaths = @(); $shortcutExisted = @{}
$publishTry = $ast.Find({param($node) $node -is [Management.Automation.Language.TryStatementAst] -and $node.Body.Extent.Text.Contains('$runtimeDirectories =')}, $true)
$body = $publishTry.CatchClauses[0].Body.Extent.Text
$rollback = [scriptblock]::Create($body.Substring(1, $body.Length - 2))
try { & $rollback; throw 'Expected rollback to rethrow' } catch {
    if ($_.Exception.Message -notmatch '^Publishing failed and previous runtime/shortcuts were restored') { throw }
}
if ([IO.File]::ReadAllText((Join-Path $oldDirectory 'old.txt')) -ne 'old runtime') { throw 'Old runtime was not preserved' }
if (Test-Path -LiteralPath (Join-Path $oldDirectory 'new.txt')) { throw 'Partial new runtime remained installed' }
if ($Case -eq 'partial-copy' -and -not (Test-Path -LiteralPath (Join-Path $stateRoot 'failed-runtime\_internal\new.txt'))) {
    throw 'Partial runtime was not retained for inspection'
}
'ok'
'''


@pytest.mark.parametrize("scenario", [
    "failed-old-move", "partial-copy", "external-process", "live-lock", "stale-lock",
])
def test_actual_publish_script_preserves_runtime_and_requires_closed_vault(tmp_path, scenario):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is required for publisher integration checks")
    check = tmp_path / "check.ps1"
    check.write_text(SCRIPT, encoding="utf-8-sig")
    publisher = Path(__file__).parents[1] / "Publish.ps1"
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(check),
         "-Publisher", str(publisher), "-Fixture", str(tmp_path / "fixture"), "-Case", scenario],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
