param(
    [string]$StageRoot,
    [string]$InstallRoot,
    [string]$DataDirectory,
    [string]$PythonExe,
    [switch]$SkipSelfTests,
    [switch]$AllThemes,
    [string]$ShortcutName = 'Keepside',
    [switch]$Launch
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
Set-Location -LiteralPath $projectRoot
$appName = 'Keepside'
$legacyName = (-join [char[]](0x62FE, 0x5F71)) + '.exe'
$legacyApp = (-join [char[]](0x6355, 0x98CE, 0x6349, 0x5F71)) + '.exe'
if (-not $StageRoot) { $StageRoot = Join-Path $projectRoot (Join-Path 'build\package' $appName) }
if (-not $InstallRoot) { $InstallRoot = Join-Path $projectRoot 'dist\SnapVault' }
if (-not $DataDirectory) { $DataDirectory = Join-Path $InstallRoot 'data' }
if (-not $PythonExe) { $PythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe' }
$StageRoot = [IO.Path]::GetFullPath($StageRoot)
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$DataDirectory = [IO.Path]::GetFullPath($DataDirectory)
$stageExe = Join-Path $StageRoot ($appName + '.exe')
$installExe = Join-Path $InstallRoot ($appName + '.exe')

function Assert-ChildPath([string]$Path, [string]$Parent) {
    $absolute = [IO.Path]::GetFullPath($Path)
    $parentAbsolute = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    if (-not $absolute.StartsWith($parentAbsolute + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe publish path: $absolute"
    }
    return $absolute
}
function Assert-PlainDirectory([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Publish directories must not be links: $Path"
        }
    }
}
if ($StageRoot -eq $InstallRoot -or -not (Test-Path -LiteralPath $stageExe -PathType Leaf)) {
    throw 'A separate, complete staged build is required.'
}
if ($StageRoot.StartsWith($InstallRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $InstallRoot.StartsWith($StageRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Stage and installation directories must not contain one another.'
}
foreach ($runtimeName in @('_internal','source','docs')) {
    $runtimePath = Join-Path $InstallRoot $runtimeName
    if ($DataDirectory -eq $runtimePath -or $DataDirectory.StartsWith($runtimePath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The user vault cannot be inside a runtime directory.'
    }
}
Assert-PlainDirectory $StageRoot
Assert-PlainDirectory $InstallRoot
if (-not (Test-Path -LiteralPath (Join-Path $DataDirectory 'vault.sqlite3') -PathType Leaf)) {
    throw 'The original vault is missing. Specify -DataDirectory; publishing never creates an empty replacement.'
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw 'A Python runtime is required for consistent data backup.' }
$version = (Get-Item -LiteralPath $stageExe).VersionInfo.FileVersion
if ($version -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') { throw 'The staged program has invalid version metadata.' }
function Assert-AppExited {
    $applicationNames = @(($appName + '.exe'), $legacyName, $legacyApp, 'SnapVaultBrowserEngine.exe')
    $running = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in $applicationNames -or
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase))
    })
    if ($running.Count) { throw 'Exit all copies of the application and its browser helper normally before publishing; no process was stopped.' }
    # A source-run Python process can also own this vault. QLockFile starts with
    # its owner PID; never assume another program directory means another vault.
    $vaultLock = Join-Path $DataDirectory 'instance.lock'
    if (Test-Path -LiteralPath $vaultLock -PathType Leaf) {
        try { $ownerLine = Get-Content -LiteralPath $vaultLock -TotalCount 1 -ErrorAction Stop }
        catch { throw 'The vault lock cannot be read. Close the application normally before publishing.' }
        $ownerProcessId = 0
        if (-not [int]::TryParse([string]$ownerLine, [ref]$ownerProcessId) -or $ownerProcessId -le 0) {
            throw 'The vault lock owner cannot be verified. Check the lock before publishing; no data was changed.'
        }
        $owner = Get-Process -Id $ownerProcessId -ErrorAction SilentlyContinue
        if ($owner) { throw 'The original vault is still open in another process. Exit it normally before publishing; no process was stopped.' }
    }
}
Assert-AppExited
$stateRoot = Join-Path $projectRoot ('.tmp\release-backups\' + $version + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
$stateRoot = Assert-ChildPath $stateRoot $projectRoot
New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null

if (-not $SkipSelfTests) {
    $selfTests = @(@('--screenshot-edit-self-test','screenshot-edit','screenshot-edit-result.json'), @('--screenshot-privacy-self-test','screenshot-privacy','screenshot-privacy-result.json'), @('--screenshot-self-test','screenshot','screenshot-result.json'), @('--window-self-test','window','window-result.json'), @('--hotkey-self-test','hotkeys','hotkey-result.json'), @('--daily-tools-self-test','daily-tools','daily-tools-result.json'), @('--basket-self-test','basket','basket-result.json'), @('--shelf-self-test','shelf','shelf-result.json'))
    $selfTests = ,@('--language-self-test','language','language-result.json') + $selfTests
    if ($AllThemes) { $selfTests += ,@('--theme-self-test','themes','collection-result.json') }
    foreach ($test in $selfTests) {
        $qaRoot = Join-Path $stateRoot ('qa-' + $test[1])
        $process = Start-Process -FilePath $stageExe -ArgumentList @($test[0], ('"' + $qaRoot + '"')) -WindowStyle Hidden -PassThru -Wait
        if ($process.ExitCode -ne 0) { throw "Staged self-test failed: $($test[1])" }
        $qaResult = Get-Content -LiteralPath (Join-Path $qaRoot $test[2]) -Raw | ConvertFrom-Json
        if (-not $qaResult.ok) { throw "Staged report did not confirm success: $($test[1])" }
    }
}
Assert-AppExited
& $PythonExe (Join-Path $projectRoot 'tools\publish_release.py') prepare --data-dir $DataDirectory --state-dir $stateRoot
if ($LASTEXITCODE -ne 0) { throw 'Original-vault backup failed; nothing was replaced.' }

$bindingFile = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'SnapVault\profile.json'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPaths = @((Join-Path $desktop ($ShortcutName + '.lnk')), (Join-Path $desktop ($ShortcutName + ' ' + $version + '.lnk')))
$shortcutExisted = @{}
$bindingExisted = Test-Path -LiteralPath $bindingFile -PathType Leaf
if ($bindingExisted) { Copy-Item -LiteralPath $bindingFile -Destination (Join-Path $stateRoot 'profile-before.json') }
foreach ($shortcutPath in $shortcutPaths) {
    $shortcutExisted[$shortcutPath] = Test-Path -LiteralPath $shortcutPath -PathType Leaf
    if ($shortcutExisted[$shortcutPath]) { Copy-Item -LiteralPath $shortcutPath -Destination (Join-Path $stateRoot ([IO.Path]::GetFileName($shortcutPath))) }
}
$runtimeBackup = Join-Path $stateRoot 'runtime-before'
New-Item -ItemType Directory -Path $runtimeBackup -Force | Out-Null
$movedDirectories = @()
$newDirectories = @()
$copiedFiles = @()
$originalFileNames = @()
$releaseFiles = @(($appName + '.exe'), $legacyName, $legacyApp, 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md')
try {
    Assert-AppExited
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    $runtimeDirectories = @('_internal','source')
    if (Test-Path -LiteralPath (Join-Path $StageRoot 'docs')) { $runtimeDirectories += 'docs' }
    foreach ($name in $runtimeDirectories) {
        $target = Assert-ChildPath (Join-Path $InstallRoot $name) $InstallRoot
        $backupTarget = Assert-ChildPath (Join-Path $runtimeBackup $name) $runtimeBackup
        Assert-PlainDirectory $target
        if (Test-Path -LiteralPath $target) {
            Move-Item -LiteralPath $target -Destination $backupTarget
            $movedDirectories += $name
        }
        # Only after the old directory has moved successfully may rollback
        # treat this path as a newly written (possibly partial) runtime.
        $newDirectories += $name
        Copy-Item -LiteralPath (Join-Path $StageRoot $name) -Destination $InstallRoot -Recurse -Force
    }
    foreach ($name in $releaseFiles) {
        $target = Assert-ChildPath (Join-Path $InstallRoot $name) $InstallRoot
        if (Test-Path -LiteralPath $target) {
            Copy-Item -LiteralPath $target -Destination (Join-Path $runtimeBackup $name)
            $originalFileNames += $name
        }
        $source = if ($name -in @($legacyName, $legacyApp)) { $stageExe } else { Join-Path $StageRoot $name }
        $copiedFiles += $name
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
    & $PythonExe (Join-Path $projectRoot 'tools\publish_release.py') verify --data-dir $DataDirectory --state-dir $stateRoot
    if ($LASTEXITCODE -ne 0) { throw 'Data verification failed after runtime replacement.' }
    # Native PowerShell writes this path so Store-Python AppData redirection
    # cannot make the frozen application's binding invisible.
    New-Item -ItemType Directory -Path (Split-Path -LiteralPath $bindingFile) -Force | Out-Null
    $bindingTemporary = $bindingFile + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $bindingContent = @{version=1;data_dir=$DataDirectory} | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($bindingTemporary, $bindingContent, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $bindingTemporary -Destination $bindingFile -Force
    $shell = New-Object -ComObject WScript.Shell
    foreach ($shortcutPath in $shortcutPaths) {
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $installExe
        $shortcut.Arguments = '--data-dir "' + $DataDirectory + '"'
        $shortcut.WorkingDirectory = $InstallRoot
        $shortcut.IconLocation = $installExe + ',0'
        $shortcut.Description = $appName + ' ' + $version
        $shortcut.Save()
        $verifiedShortcut = $shell.CreateShortcut($shortcutPath)
        if ($verifiedShortcut.TargetPath -ne $installExe -or $verifiedShortcut.Arguments -ne $shortcut.Arguments) { throw 'Desktop shortcut verification failed.' }
    }
    $result = [ordered]@{ok=$true;version=$version;program=$installExe;data_directory=$DataDirectory;backup_directory=$stateRoot;profile_binding=$bindingFile;desktop_shortcuts=$shortcutPaths;self_tests_skipped=[bool]$SkipSelfTests}
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $stateRoot 'publish-result.json') -Encoding utf8
    $result | ConvertTo-Json -Depth 4
} catch {
    $publishError = $_
    # Retain failed new files for inspection. All recursive moves are checked
    # against the explicit installation and project backup roots above.
    $failedRuntime = Join-Path $stateRoot 'failed-runtime'
    New-Item -ItemType Directory -Path $failedRuntime -Force | Out-Null
    foreach ($name in $newDirectories) {
        $target = Assert-ChildPath (Join-Path $InstallRoot $name) $InstallRoot
        $failedTarget = Assert-ChildPath (Join-Path $failedRuntime $name) $failedRuntime
        Assert-PlainDirectory $target
        if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $failedTarget }
    }
    foreach ($name in $movedDirectories) {
        $target = Assert-ChildPath (Join-Path $InstallRoot $name) $InstallRoot
        $backupTarget = Assert-ChildPath (Join-Path $runtimeBackup $name) $runtimeBackup
        Move-Item -LiteralPath $backupTarget -Destination $target
    }
    foreach ($name in $copiedFiles) {
        $target = Assert-ChildPath (Join-Path $InstallRoot $name) $InstallRoot
        if ($name -in $originalFileNames) { Copy-Item -LiteralPath (Join-Path $runtimeBackup $name) -Destination $target -Force }
        elseif (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination (Join-Path $failedRuntime $name) }
    }
    if ($bindingExisted) { Copy-Item -LiteralPath (Join-Path $stateRoot 'profile-before.json') -Destination $bindingFile -Force }
    elseif (Test-Path -LiteralPath $bindingFile) { Remove-Item -LiteralPath $bindingFile -Force }
    foreach ($shortcutPath in $shortcutPaths) {
        if ($shortcutExisted[$shortcutPath]) { Copy-Item -LiteralPath (Join-Path $stateRoot ([IO.Path]::GetFileName($shortcutPath))) -Destination $shortcutPath -Force }
        elseif (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath -Force }
    }
    throw "Publishing failed and previous runtime/shortcuts were restored. Backup: $stateRoot. $publishError"
}
if ($Launch) { Start-Process -FilePath $installExe -ArgumentList @('--data-dir', ('"' + $DataDirectory + '"')) -WorkingDirectory $InstallRoot -WindowStyle Normal }
