"""Export an explicit source allowlist, excluding personal and generated state."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


def export(destination):
    root=Path(__file__).resolve().parents[1]
    destination=Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('Use a new export directory')
    destination.mkdir(parents=True)
    files=[]
    for name in ('main.py','README.md','LICENSE','THIRD_PARTY_NOTICES.md','requirements.txt',
                 'version-info.txt','Keepside.spec','Build.ps1','Publish.ps1','Setup.ps1'):
        files.append(root/name)
    for folder in ('snapvault','tests','assets','browser_engine'):
        for path in (root/folder).rglob('*'):
            if not path.is_file(): continue
            relative=path.relative_to(root)
            if folder=='assets' and len(relative.parts)==2 and path.suffix.lower() not in {'.png','.ico'}: continue
            if any(part in {'__pycache__','.tmp','.git','.venv','data','bin','tools','.pytest_cache'} for part in relative.parts): continue
            if path.suffix.lower() not in {'.py','.json','.md','.txt','.au3','.ico','.png','.ps1'} and path.name not in {'LICENSE','NOTICE'}: continue
            if path.name=='prepare_assets.py': continue
            files.append(path)
    for name in ('collect_ocr_licenses.py','rthook_ocr_isolation.py','publish_release.py','merge_vaults.py','download_ocr_models.py','export_public_source.py'):
        files.append(root/'tools'/name)
    for path in files:
        relative=path.relative_to(root)
        target=destination/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        if path.suffix.lower() not in {'.png','.ico'}:
            value=path.read_text(encoding='utf-8-sig')
            private_path = r'(?i)[A-Z]:[\\/]+Users[\\/]+' + re.escape(Path.home().name) + r'(?:[\\/]|$)'
            if re.search(private_path,value):
                raise ValueError('Private reference in source file: '+relative.as_posix())
        shutil.copyfile(path,target)
    (destination/'.gitignore').write_text('.venv/\n.tmp/\nbuild/\ndist/\ndata/\n**/data/\n__pycache__/\n.pytest_cache/\n*.pyc\n*.onnx\n*.download\n*.zip\n*.exe\nprofile.json\nlocal-settings.json\n',encoding='utf-8')
    (destination/'AGENTS.md').write_text('# Keepside contributor notes\n\nKeep source and local user data separate. Never commit libraries, local name overrides, machine-specific paths, credentials or generated artifacts. Preserve existing libraries during upgrades. Public product naming is Keepside. Translate UI labels, never user content or persistent identifiers. Use isolated synthetic tests.\n',encoding='utf-8')
    docs=destination/'docs';docs.mkdir(exist_ok=True)
    (docs/'release-workflow.md').write_text('# Release workflow\n\nBuild with Build.ps1 -StageOnly. Verify the candidate using synthetic diagnostics in both languages. Close existing application copies normally, then use Publish.ps1 with the existing DataDirectory. Publishing backs up and verifies records, original image bytes and settings, and creates desktop shortcuts. Never publish data directories, local settings or rollback backups. SkipSelfTests is allowed only for the exact already-verified candidate. AllThemes is optional for changes to the theme system.\n',encoding='utf-8')
    manifest=[]
    for path in sorted(destination.rglob('*')):
        if path.is_file():
            data=path.read_bytes()
            manifest.append(dict(path=path.relative_to(destination).as_posix(),bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('destination');parser.add_argument('--manifest',required=True)
    args=parser.parse_args();manifest=export(args.destination)
    Path(args.manifest).write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(dict(files=len(manifest),bytes=sum(row['bytes'] for row in manifest))))
