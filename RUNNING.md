# How to actually run this

Two machines, two different commands. `python` on its own works on neither.

## On Pearce's laptop (Windows / PowerShell)

Windows intercepts a bare `python` with a Microsoft Store shortcut, so always
call the venv's interpreter directly:

```powershell
cd C:\Users\daysh\Documents\Fantasy-Manager
.\.venv\Scripts\python.exe scripts\login.py
```

Shorter, if you'd rather not type that every time — activate the venv first and
`python` means the right thing for the rest of that terminal session:

```powershell
.\.venv\Scripts\Activate.ps1
python scripts\login.py
```

## On the box (`jarvis`, Linux)

```bash
ssh -i ~/.ssh/optiplex_astra ironman@192.168.4.43
cd ~/Fantasy-Manager
./.venv/bin/python scripts/healthcheck.py
```

---

## The one-time login

**Run these one at a time.** The first opens a browser and then waits for you —
pasting all three at once runs the other two before you've logged in.

```powershell
# 1. Opens a Chromium window. Log into ESPN, come back, press ENTER.
.\.venv\Scripts\python.exe scripts\login.py

# 2. Proves the saved session works headless.
.\.venv\Scripts\python.exe scripts\login.py --verify

# 3. Copy it to the box.
scp data\espn-session.json ironman@192.168.4.43:~/Fantasy-Manager/data/
```

`data/espn-session.json` is a **live credential** — anyone holding it can act as
you on ESPN without a password. It is gitignored. Don't put it in Slack, email
or the vault; `scp` is fine because it's a direct encrypted transfer.

Disney sessions expire. When #fantasy starts reporting "Log in Required", run
these three again.

---

## Everything else

| What | Laptop | Box |
|---|---|---|
| Health check | `.\.venv\Scripts\python.exe scripts\healthcheck.py` | `./.venv/bin/python scripts/healthcheck.py` |
| Build the board | `... scripts\build_board.py` | `... scripts/build_board.py` |
| Rehearse the draft | `... scripts\simulate_draft.py -n 15` | same |
| Draft day | `... scripts\draft.py --no-click` | same |
| Daily sweep, no writes | `... scripts\manage.py --dry-run --no-agent` | same |
| Find selectors | `... scripts\discover_selectors.py --draft --headed` | laptop only (needs a visible browser) |
| Tests | `.\.venv\Scripts\python.exe -m pytest -q` | `./.venv/bin/python -m pytest -q` |

## The kill switch

```powershell
Get-Content ENABLED          # on / off
"on"  | Out-File ENABLED -Encoding ascii -NoNewline
"off" | Out-File ENABLED -Encoding ascii -NoNewline
```

On the box: `cat ENABLED`, `echo on > ENABLED`, `echo off > ENABLED`.

It ships **off**. Nothing in `core` ever turns it back on — that is always a
human decision.
