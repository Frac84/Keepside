# SnapVault browser engine

This directory contains the original AutoIt browser-tab engine, adapted to run as a SnapVault-owned background process. The original project is [nurupo/chrome-mouse-wheel-tab-scroller](https://github.com/nurupo/chrome-mouse-wheel-tab-scroller), Copyright 2017–2022 Maxim Biro, GPL-3.0-only (see LICENSE). The starting source is the user's desktop PlusAutoIt modification, including its existing double-click-to-close and shutdown cleanup changes.

The original mouse handling, tab-strip rectangle constants, wheel direction, keyboard delivery (`Send` followed by `ControlSend`), double-click timing, autofocus modes and focus delay implementation are retained. Only embedded lifecycle, IPC and an optional double-click guard are added. `audit_core.py` verifies those core functions against the original desktop source.

## Build

Use the official [AutoIt portable package](https://www.autoitscript.com/site/autoit/downloads/). The default compiler location is `tools/autoit/install/Aut2Exe/Aut2exe_x64.exe`. Run `build-native.ps1`, or pass `-AutoItRoot` with the portable `install` directory. The output is `bin/SnapVaultBrowserEngine.exe`. The compiler is a build dependency only and is not required by end users.

## Embedded protocol

Launch with `--embedded-parent PID --embedded-options MASK`. The child has no independent tray, update checks or startup-registration actions. It exits when the parent no longer exists (checked every 500 ms). Its original singleton name is preserved, so a concurrently running original instance causes exit code 23 rather than duplicate processing. Failure to register Raw Input or embedded messages exits with code 24. Invalid embedded arguments exit with code 22.

Find the child-owned hidden GUI window with title `Chrome Mouse Wheel Tab Scroller Raw Input Handler Window`. Do not send IPC to the separate AutoIt runtime window. It uses `GUIRegisterMsg` for:

- `WM_APP + 42` (`0x802A`): `wParam = MASK`, `lParam = 0x53565442`; returns 1 after applying settings.
- `WM_APP + 43` (`0x802B`): `lParam = 0x53565442`; requests graceful exit.

Wait until `GetPropW(hwnd, "SnapVault.EngineReady")` equals 1 before sending options. After a synchronous options message completes, `GetPropW(hwnd, "SnapVault.EngineOptions")` equals the normalized applied mask plus 1; use this property for acknowledgement instead of depending on AutoIt's message callback return value.

The mask is:

| Bits | Value |
| --- | --- |
| 0 | Enabled |
| 1 | Reverse wheel direction |
| 2 | Double-click close enabled |
| 3 | Autofocus enabled |
| 4–5 | Original autofocus afterwards mode: 0 keep, 1 simple undo, 2 comprehensive undo |
| 8–15 | Original focus delay in milliseconds: 3, 4, 5, 8, 11, 15, 20, 25, 30, 35, 40, 50, 75, 100 |

The default mask is `0x192D`: enabled, normal direction, double-click close, autofocus, comprehensive undo, 25 ms. Invalid mode or delay fields retain their current valid values. Options are applied in memory. The engine reads the original config on launch but never writes it in embedded mode. Without `--embedded-parent`, its original standalone behavior remains available.

`smoke_protocol.py` verifies readiness, singleton collision (duplicate exits with code 23), options acknowledgement, normalization, graceful shutdown and automatic exit when an owned test parent exits. It always clears the enabled bit, so it never switches or closes browser tabs. The original standalone engine and SnapVault must be stopped first to release the original singleton.

