"""Verify that embedding changes do not alter the user's original AutoIt engine."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
ORIGINAL_SHA256 = "4d885bab5ef2ba3f81f04ed40d3cb319ea0c365dc8e42d2ee3246437f128f900"
FUNCTION = re.compile(r"^Func\s+(\w+)\([^\n]*\n.*?^EndFunc$", re.MULTILINE | re.DOTALL)
ALLOWED_GUARDS = {
    "singleInstance": "        If $embeddedMode Then Exit(23)\n",
    "onLeftButtonDown": "    If Not $cfgDoubleClick Then Return\n",
    "writeConfig": "    If $embeddedMode Then Return\n",
}
NEW_FUNCTIONS = {
    "readEmbeddedOptions", "applyEmbeddedOptions", "setEmbeddedProperty",
    "embeddedSetOptions", "embeddedExit", "embeddedCheckParent",
}


def audit(original: Path, modified: Path) -> dict:
    raw = original.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ORIGINAL_SHA256, "Original source baseline changed"
    before = raw.decode("utf-8-sig").replace("\r\n", "\n")
    after = modified.read_text(encoding="utf-8-sig")
    original_functions = {m[1]: m[0] for m in FUNCTION.finditer(before)}
    embedded_functions = {m[1]: m[0] for m in FUNCTION.finditer(after)}
    assert original_functions, "No original functions found"
    for name, original_body in original_functions.items():
        embedded_body = embedded_functions[name]
        guard = ALLOWED_GUARDS.get(name)
        if guard:
            assert embedded_body.count(guard) == 1, f"Unexpected {name} guard"
            embedded_body = embedded_body.replace(guard, "", 1)
        assert original_body == embedded_body, f"Original function changed: {name}"
    assert set(embedded_functions) - set(original_functions) == NEW_FUNCTIONS
    constants = re.compile(r"^Global Const \$(?:CHROME_\w+|RES_PRODUCT_NAME)\s*=.*$", re.MULTILINE)
    assert constants.findall(before) == constants.findall(after), "Window region or singleton name changed"
    return {
        "baseline_sha256": ORIGINAL_SHA256,
        "original_functions_verified": len(original_functions),
        "allowed_single_line_guards": sorted(ALLOWED_GUARDS),
        "new_embedding_functions": sorted(NEW_FUNCTIONS),
        "core_behavior": "unchanged",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, default=ROOT / "upstream" / "chrome-mouse-wheel-tab-scroller.au3")
    args = parser.parse_args()
    print(json.dumps(audit(args.original, ROOT / "SnapVaultBrowserEngine.au3"), indent=2))

