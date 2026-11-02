param(
    [string]$AutoItRoot = (Join-Path $PSScriptRoot 'tools\autoit\install')
)

$ErrorActionPreference = 'Stop'
$engineSource = Join-Path $PSScriptRoot 'SnapVaultBrowserEngine.au3'
$engineOutputDir = Join-Path $PSScriptRoot 'bin'
$engineOutput = Join-Path $engineOutputDir 'SnapVaultBrowserEngine.exe'
$engineCompiler = Join-Path $AutoItRoot 'Aut2Exe\Aut2exe_x64.exe'
$engineChecker = Join-Path $AutoItRoot 'Au3Check.exe'
$engineIcon = Join-Path $PSScriptRoot 'icon.ico'
if (-not (Test-Path -LiteralPath $engineCompiler)) {
    throw "AutoIt compiler not found: $engineCompiler. Supply -AutoItRoot for the portable install directory."
}
New-Item -ItemType Directory -Path $engineOutputDir -Force | Out-Null

# Native compiler tools need short paths for folders containing non-ASCII text.
if (-not ('SnapVaultAutoItPaths' -as [type])) {
    Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class SnapVaultAutoItPaths {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern uint GetShortPathName(string path, StringBuilder buffer, uint capacity);
    public static string Short(string path) {
        var buffer = new StringBuilder(32768);
        if (GetShortPathName(path, buffer, (uint)buffer.Capacity) == 0)
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), path);
        return buffer.ToString();
    }
}
'@
}
$engineSourceShort = [SnapVaultAutoItPaths]::Short($engineSource)
if (Test-Path -LiteralPath $engineChecker) {
    $engineCheckResult = Start-Process -FilePath ([SnapVaultAutoItPaths]::Short($engineChecker)) -ArgumentList @($engineSourceShort) -WindowStyle Hidden -PassThru -Wait
    if ($engineCheckResult.ExitCode -ne 0) { throw "Au3Check failed with code $($engineCheckResult.ExitCode)." }
}
$engineOutputShort = Join-Path ([SnapVaultAutoItPaths]::Short($engineOutputDir)) 'SnapVaultBrowserEngine.exe'
$engineCompileResult = Start-Process -FilePath ([SnapVaultAutoItPaths]::Short($engineCompiler)) -ArgumentList @('/in', $engineSourceShort, '/out', $engineOutputShort, '/icon', ([SnapVaultAutoItPaths]::Short($engineIcon)), '/x64', '/nopack') -WindowStyle Hidden -PassThru -Wait
if ($engineCompileResult.ExitCode -ne 0) { throw "Aut2Exe failed with code $($engineCompileResult.ExitCode)." }
if (-not (Test-Path -LiteralPath $engineOutput)) { throw 'Aut2Exe did not produce the browser engine.' }
Write-Host "Built: $engineOutput"

