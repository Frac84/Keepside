# Release workflow

Build with Build.ps1 -StageOnly. Verify the candidate using synthetic diagnostics in both languages. Close existing application copies normally, then use Publish.ps1 with the existing DataDirectory. Publishing backs up and verifies records, original image bytes and settings, and creates desktop shortcuts. Never publish data directories, local settings or rollback backups. SkipSelfTests is allowed only for the exact already-verified candidate. AllThemes is optional for changes to the theme system.
