@echo off
REM ====================================================================
REM pre-commit hook: route-lint for Bee Studio API router
REM
REM What it does:
REM   - Runs tools/route-lint.py against api/router.py when route-
REM     related files are staged (router.py, panel.py, script_routes.py,
REM     or route-lint.py itself).
REM   - Blocks commit on lint ERRORs. Does NOT auto-fix anything.
REM   - Mirrors .github/workflows/route-lint.yml trigger scope.
REM
REM Team-install location: hooks/pre-commit.bat  (tracked in git)
REM Installed-to:          .git/hooks/pre-commit.bat  (local, per clone)
REM Install command:       powershell -File scripts/install-hooks.ps1
REM
REM Local opt-out (emergency):
REM   git config hooks.route-lint.enabled false
REM Or bypass for a single commit:
REM   git commit --no-verify
REM ====================================================================

setlocal enabledelayedexpansion

REM --- repo root ---
for /f "delims=" %%i in ('git rev-parse --show-toplevel 2^>nul') do set ROOT=%%i
if not defined ROOT exit /b 1

REM --- opt-out check ---
for /f "delims=" %%v in ('git config --bool hooks.route-lint.enabled 2^>nul') do set HOOK_ON=%%v
if "!HOOK_ON!"=="false" exit /b 0

REM --- find python ---
set PY=
if exist "%ROOT%\.venv\Scripts\python.exe" (
    set PY=%ROOT%\.venv\Scripts\python.exe
) else (
    where python >nul 2>nul && set PY=python
)
if not defined PY exit /b 0

REM --- route-lint.py must exist ---
set LINT=%ROOT%\tools\route-lint.py
if not exist "%LINT%" exit /b 0

REM --- detect route-related staged files (mirrors CI workflow) ---
set NEED_LINT=0
for %%F in (api/router.py api\router.py panel.py script_routes.py script_routes\*.py tools/route-lint.py tools\tokenify.py) do (
    git diff --cached --name-only 2>nul | findstr /i /c:"%%F" >nul
    if !errorlevel! equ 0 set NEED_LINT=1
)
if !NEED_LINT! equ 0 exit /b 0

REM --- run ---
echo [pre-commit] route-lint api/router.py ...
"%PY%" "%LINT%" api\router.py
if !errorlevel! neq 0 (
    echo.
    echo [pre-commit] BLOCKED: route-lint found issues.
    echo              Fix them, then git add + commit again.
    echo              (Or git commit --no-verify to bypass once.)
    exit /b 1
)

exit /b 0
