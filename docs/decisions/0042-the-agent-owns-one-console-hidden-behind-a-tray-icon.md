# 0042. The host agent owns one console, hidden behind a tray icon

- Date: 2026-08-15
- Status: accepted
- Rule: the agent owns exactly one window, born hidden, shown only from the tray. `--hide` is what says the console is the agent's; without it the console belongs to whoever ran it and is never touched.
- Rule: the tray icon is the host agent mark and the mark carries the status; one shortcut both starts the agent and shows the running one, because from outside those are the same intention.
- Amends [0023](0023-the-host-agent-is-a-sensor-not-a-decision-maker.md), which described the agent as a headless service with no desktop presence at all.

## Context

The agent and its two llama-server processes put three console windows on the desktop at every logon, and none of them was ever read. The ask was: minimize to a tray icon, give it an icon, maybe a basic TUI, and fold the embed window into the same place.

Two separate problems hide inside that. Where the *output* goes, and where the *window* goes. They have different answers.

## Decision

**One window, hidden by default, shown and hidden from a tray icon.** `hostagent/console.py`: `textual` for the UI, `pystray` plus `Pillow` for the tray. The two llama-server consoles do not move into it - they stop existing (`-WindowStyle Hidden` on the embed server, `-Detached` on the router). Their *logs* move in, as tabs, read through the same `read_log` byte-offset delta that already feeds `/api/llm/logs` and the admin pane (0034). One reader, two consumers.

**The close button is deleted** (`GetSystemMenu` + `DeleteMenu(SC_CLOSE)`), rather than intercepted. `CTRL_CLOSE_EVENT` cannot be cancelled: a handler gets a few seconds to clean up and the process dies regardless, so "close hides to tray" is not implementable on a console window. Removing the affordance is honest; a close button that kills the backend controller by surprise is not. Quit lives on the tray menu and on `q`.

**`conhost.exe <command>`, not `pwsh.exe`.** On Windows 11 the default-terminal handoff sends the console to Windows Terminal, and `GetConsoleWindow()` then points at a window the visible UI does not live in. Hide-to-tray would move nothing and the close-button removal would apply to a window nobody can click. Opting out of the handoff is what makes every win32 call in this file mean what it says.

## The logon flash, and why hiding after the fact is not a fix

The console was hidden by `ShowWindow(SW_HIDE)` as the first thing the launcher script did. **Measured: 419 ms of visible console at every logon**, because a window that is hidden after creation was first created shown. Being *created* hidden - `STARTF_USESHOWWINDOW` + `SW_HIDE`, which is what `Start-Process -WindowStyle Hidden` sets - was measured as never observed visible, sampling every 8 ms across every `ConsoleWindowClass` window on the desktop.

Task Scheduler always starts its action shown and offers no way to pass `SW_HIDE`, so the task action cannot itself be the agent's console. Hence two stages:

1. task action: `conhost.exe --headless pwsh.exe ... run-agent.ps1 -Spawn`. A headless conhost has no window at all, so stage 1 cannot flash by construction.
2. `-Spawn` runs `Start-Process conhost.exe ... -WindowStyle Hidden` and exits. The agent's real console is born hidden.

Verified through a throwaway scheduled task, which is the only path that reproduces the logon conditions: window created at 340 ms, never observed visible, agent answering `/status`. A window created hidden still restores (`SW_RESTORE` from the tray) and still has no close button, both checked on that same window.

### Rejected

- **A GUI-subsystem launcher** (`wscript.exe` plus a three-line `.vbs`, or `mshta.exe`). Also zero-flash, and one process shorter, but it makes the agent depend on VBScript, which Microsoft has announced for removal. `--headless` is a conhost flag in continuous use by OpenSSH and WSL.
- **Shortening the 419 ms** by dropping the `Add-Type` C# compile that is most of it. Trades a visible defect for a smaller visible defect.

## What the UI is, and two defects it had

A status line, a tab per log (`router/main`, `embed`, `agent`), and key hints. `router/main` is a **display label only**: `router` remains the agent's key, the log file stem, the `read_log` argument and the `#log-*` id, and a test pins that a label change moves only what a person reads.

1. **The status line disappeared once a log filled.** `TabPane` and `ContentSwitcher` size to their content, so 400 lines made the Screen taller than the window; Textual grew a second scrollbar *outside* the log's own and scrolled the whole app, and the first row to leave was the one line the window exists to show. Fixed with `1fr` heights and `Screen { overflow: hidden }`, pinned by a test asserting `screen.virtual_size.height <= screen.size.height`, which fails at 29 <= 27 without it - exactly the two rows of `#status`.
2. **Every log line was double-spaced.** Lines land on disk as `\r\r\n`: llama-server writes CRLF and the Windows CRT translates the `\n` a second time, and `splitlines()` reads the orphan `\r` as a break. Fixed in `read_log`, at the source, so the admin pane got it too; fixing it in the console would have been the consumer-side workaround this project's rules forbid. A **lone** `\r` stays a break on purpose: llama.cpp uses it to rewrite progress in place, and a log file cannot render that any other way.

Textual's `Footer` is not used. It docks to the last row, which conhost clips when the window height is not a whole multiple of the cell height, and its first real compose waits on a binding-change event delivered only while the app has focus, which a classic console never reports. The key hints are a third `#status` line derived from `BINDINGS`, so the list cannot drift from what the keys do.

## The icon is the mark, and the mark is the status light

Added 2026-08-15. The tray started as two coloured bars, which answered "which of my two is up" and nothing else: a generic shape that had to be learned, and no relation to the project it controls.

It is now the **host agent mark** - the Episteme obelisk standing in a small neural network instead of the weave - drawn by `console.py:icon_image` and specified in [graphics/logo/README.md](../../graphics/logo/README.md). Same claim as the primary mark with one word changed: the solid is embedded in the field, and here the field is the model.

**Status is carried by parts of the mark rather than added to it.** The nodes are the decode server (:5001) and the edges are the embedder (:5002). Neither mapping is arbitrary: nodes are the units that produce, and they are what survives the downsample to a 16 px tile; an embedding *is* the relation between things rather than a thing, and the embedder is the one that stays resident. A single overall colour could not say "half-up", which is the normal overnight state.

A stopped backend is **grey, not red**. An icon that shows an error every night is an icon nobody reads by the second week.

The same `icon_image` is rendered to `graphics/logo/hostagent.ico` by `tools/build_hostagent_ico.py`, so the tray, the taskbar button and the shortcut are one picture by construction. Both servers are passed as up when it is generated: a shortcut is an identity, not a status light.

## One shortcut for both states

`hostagent/open-agent.ps1`, installed by `hostagent/install-shortcut.ps1` (Start menu by default, `-Desktop` on request). "Start it" and "show the running one" are the same intention from outside, so they are one entry point; a person should not have to know which state they are in.

It finds the running agent with `FindWindowW`, matched on **both** class (`ConsoleWindowClass`, which is what the `conhost.exe <command>` decision above produces) and title (`llama.cpp agent`, which `console.py:run` sets). That finds hidden windows, which is the only kind this one is when it is in the tray, and it needs no new HTTP route and no guessing at which of uv's, Python's or conhost's processes holds the window. Cold start goes through `run-agent.ps1 -Spawn`, the same path the scheduled task uses, so the console is still born hidden, and the script then waits for that window and shows it: the task wants it invisible at logon, a person double-clicking wants to see it, and that difference belongs in the shortcut rather than in a second way of starting the agent. If :5003 is bound but no window exists, it says so rather than starting a second instance that would fail to bind and die unseen.

The shortcut's target is `conhost.exe --headless pwsh ...`, for the reason in the flash section above.

Two measured facts, both recorded in the script:

- **PowerShell converts `$null` to `""` when binding a `[string]` parameter.** `FindWindowW($null, $Title)` therefore searches for a window class literally named empty-string, never matches, and the shortcut starts a second agent. Cost a clean 60-second timeout against a window that was sitting right there.
- **`SetForegroundWindow` is refused** to a process not itself in the foreground, so run by hand from another terminal the window comes up unfocused (measured: `visible: True, foreground: False`). Launched from the shortcut the shell grants the right. Not worth the `AttachThreadInput` dance: the window is up either way.

Verified end to end three ways on 2026-08-15: already-running (restores), cold start from the script (2.8 s to a visible window, `/status` answering), and cold start by launching the installed `.lnk`.

## Consequences

- The desktop gains a tray icon and a shortcut, and loses three windows.
- `hostagent/` now has a second file and three more dependencies, declared in `llama_agent.py`'s PEP-723 header rather than the repo's `pyproject.toml`: the agent still has no venv to maintain. `console.py` deliberately carries no header of its own, since `uv` resolves only the header of the script it is handed, and a second one would look authoritative while installing nothing.
- The palette is single-sourced from `web/static/style.css` by value, not by import - the agent cannot read the container's static files. A test pins the tokens.
