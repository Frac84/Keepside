param([switch]$StageOnly, [switch]$ReuseBrowserEngine, [switch]$WithoutSmartTags, [string]$DataDirectory)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$appName = 'Keepside'
$stageRoot = Join-Path $PSScriptRoot (Join-Path 'build\package' $appName)
$stageExe = Join-Path $stageRoot ($appName + '.exe')
$archivePath = Join-Path $PSScriptRoot ($appName + '-Windows-x64.zip')
foreach ($requiredBrandFile in @('assets\shiying.ico', 'assets\shiying.png', 'version-info.txt', 'snapvault\branding.py')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $requiredBrandFile) -PathType Leaf)) {
        throw "Brand resource is missing: $requiredBrandFile"
    }
}
& .venv\Scripts\python.exe -c "from snapvault.ocr import check_availability; check_availability()"
if ($LASTEXITCODE -ne 0) { throw 'Bundled OCR model verification failed.' }
if (-not $WithoutSmartTags) {
    & .venv\Scripts\python.exe -c "from snapvault.topic_runtime import validate_assets; validate_assets()"
    if ($LASTEXITCODE -ne 0) { throw 'Bundled classification model verification failed. Supply matching assets or use -WithoutSmartTags.' }
}
& .venv\Scripts\python.exe tools\collect_ocr_licenses.py
if ($LASTEXITCODE -ne 0) { throw 'OCR dependency notice collection failed.' }
if ($ReuseBrowserEngine) {
    $existingEngine = Join-Path $PSScriptRoot 'browser_engine\bin\SnapVaultBrowserEngine.exe'
    if (-not (Test-Path -LiteralPath $existingEngine -PathType Leaf)) {
        throw 'The existing browser engine is missing; rebuild it after closing the application.'
    }
    Write-Host 'Reusing the existing browser engine; verifying its corresponding source below.'
} else {
    & .\browser_engine\build-native.ps1
}
& .venv\Scripts\python.exe browser_engine\audit_core.py
if ($LASTEXITCODE -ne 0) { throw 'Original browser core verification failed.' }
$originalBuildPath = $env:PATH
try {
    # Third-party tools on PATH can carry an incompatible ICU with the same DLL
    # name as Windows ICU. Qt needs the Windows implementation. Resolve native
    # dependencies in a clean build environment instead of bundling foreign ICU.
    $env:PATH = "$PSScriptRoot\.venv\Scripts;$env:SystemRoot\System32;$env:SystemRoot"
    & .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath build\package ($appName + '.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
} finally {
    $env:PATH = $originalBuildPath
}
# Some Python distributions inject additional DLL directories at interpreter
# startup even with a clean PATH. Qt 6.11 imports Windows' unversioned ICU API;
# Poppler's identically named ICU exports version-suffixed symbols and breaks it.
# Windows provides this dependency. Never distribute those foreign shadow copies.
foreach ($foreignIcu in @('icuuc.dll', 'icudt78.dll')) {
    $foreignPath = Join-Path $stageRoot "_internal\$foreignIcu"
    if (Test-Path -LiteralPath $foreignPath) { Remove-Item -LiteralPath $foreignPath -Force }
}
# Include the corresponding application sources and original attribution.
$sourceRoot = Join-Path $stageRoot 'source'
New-Item -ItemType Directory -Path $sourceRoot -Force | Out-Null
foreach ($sourceFile in @('main.py', 'requirements.txt', 'Build.ps1', 'Publish.ps1', 'Setup.ps1', ($appName + '.spec'), 'version-info.txt', 'README.md', 'AGENTS.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $sourceFile) -Destination $sourceRoot -Force
}
foreach ($optionalLauncher in @('Start-SnapVault.cmd', 'Start-GlassPreview.cmd')) {
    $launcherPath = Join-Path $PSScriptRoot $optionalLauncher
    if (Test-Path -LiteralPath $launcherPath -PathType Leaf) {
        Copy-Item -LiteralPath $launcherPath -Destination $sourceRoot -Force
    }
}
$legacySpec = Join-Path $PSScriptRoot 'SnapVault.spec'
if (Test-Path -LiteralPath $legacySpec -PathType Leaf) {
    Copy-Item -LiteralPath $legacySpec -Destination $sourceRoot -Force
}
foreach ($sourceDir in @('snapvault', 'tests')) {
    $targetDir = Join-Path $sourceRoot $sourceDir
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot $sourceDir) -Filter '*.py' -File |
        Copy-Item -Destination $targetDir -Force
}
Copy-Item -LiteralPath 'snapvault\locales' -Destination (Join-Path $sourceRoot 'snapvault') -Recurse -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'assets') -Destination $sourceRoot -Recurse -Force
$privatePromptCopy = Join-Path $sourceRoot 'assets\shiying-icon-prompt.txt'
if (Test-Path -LiteralPath $privatePromptCopy) { Remove-Item -LiteralPath $privatePromptCopy -Force }
$sourceTools = Join-Path $sourceRoot 'tools'
New-Item -ItemType Directory -Path $sourceTools -Force | Out-Null
Copy-Item -LiteralPath 'tools\collect_ocr_licenses.py' -Destination $sourceTools -Force
Copy-Item -LiteralPath 'tools\rthook_ocr_isolation.py' -Destination $sourceTools -Force
Copy-Item -LiteralPath 'tools\publish_release.py' -Destination $sourceTools -Force
Copy-Item -LiteralPath 'tools\merge_vaults.py' -Destination $sourceTools -Force
Copy-Item -LiteralPath 'tools\download_ocr_models.py' -Destination $sourceTools -Force
$engineSourceRoot = Join-Path $sourceRoot 'browser_engine'
New-Item -ItemType Directory -Path $engineSourceRoot -Force | Out-Null
Get-ChildItem -LiteralPath browser_engine -File | Copy-Item -Destination $engineSourceRoot -Force
Copy-Item -LiteralPath browser_engine\upstream -Destination $engineSourceRoot -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $engineSourceRoot 'bin') -Force | Out-Null
Copy-Item -LiteralPath browser_engine\bin\SnapVaultBrowserEngine.exe -Destination (Join-Path $engineSourceRoot 'bin') -Force
foreach ($releaseDoc in @('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $releaseDoc) -Destination $stageRoot -Force
}
# Keep the optional visual-direction board available in the portable build.
foreach ($docsRoot in @((Join-Path $stageRoot 'docs'), (Join-Path $sourceRoot 'docs'))) {
    New-Item -ItemType Directory -Path (Join-Path $docsRoot 'design') -Force | Out-Null
    foreach ($designDoc in @('UI设计方向.html', 'design\shelf-v2-plan.md', 'design\application-glass-plan.md', 'design\frost-theme-plan.md', 'design\file-basket-plan.md', 'design\daily-actions-plan.md', 'design\hotkey-repair-plan.md', 'design\window-usability-plan.md', 'design\screenshot-workflow-plan.md', 'design\screenshot-editing-plan.md',
                            'application-glass-preview-light.png', 'application-glass-preview-dark.png',
                            'application-glass-native-verification.json', 'release-workflow.md')) {
        $designSource = Join-Path (Join-Path $PSScriptRoot 'docs') $designDoc
        if (Test-Path -LiteralPath $designSource -PathType Leaf) {
            Copy-Item -LiteralPath $designSource -Destination (Join-Path $docsRoot $designDoc) -Force
        }
    }
}
# Archive first; publishing never adds private data, bindings or rollback backups
# to this stage. StageOnly is useful for verification, but is not a delivered update.
foreach ($privateName in @('data', 'profile.json', '.tmp')) {
    if (Test-Path -LiteralPath (Join-Path $stageRoot $privateName)) {
        throw "The stage contains private/test state ($privateName); archive creation stopped."
    }
}
Compress-Archive -LiteralPath $stageRoot -DestinationPath $archivePath -Force
Write-Host "Built $stageExe"
Write-Host "Archive: $archivePath"
Write-Host 'Portable archive includes source and excludes the user data directory.'
if ($StageOnly) {
    Write-Host 'StageOnly: installation, vault continuity and desktop shortcuts have not been published.'
} else {
    $releaseRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'dist\SnapVault'))
    if (-not $DataDirectory) { $DataDirectory = Join-Path $releaseRoot 'data' }
    & (Join-Path $PSScriptRoot 'Publish.ps1') -StageRoot $stageRoot -InstallRoot $releaseRoot -DataDirectory $DataDirectory -PythonExe (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
}
