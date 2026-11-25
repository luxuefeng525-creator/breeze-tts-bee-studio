# ====================================================================
# install-hooks.ps1 — install team git hooks into .git/hooks/
#
# Usage:
#   .\scripts\install-hooks.ps1                  # dry-run (show what it WOULD do)
#   .\scripts\install-hooks.ps1 -Force           # install (overwrite existing)
#   .\scripts\install-hooks.ps1 -Cleanup         # restore .git/hooks/ from team templates
#   .\scripts\install-hooks.ps1 -OptOut          # set git config hooks.route-lint.enabled=false
#   .\scripts\install-hooks.ps1 -OptIn           # unset opt-out config (default)
#
# Prerequisites:
#   - Run from repo root (detected via git rev-parse)
#   - Git for Windows >= 2.40 (auto-supports .bat hook files)
#
# Safe defaults:
#   - Without -Force: prints plan but does NOT modify .git/hooks/
#   - Never installs into a different repo (validates git toplevel)
# ====================================================================

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Cleanup,
    [switch]$OptOut,
    [switch]$OptIn
)

$ErrorActionPreference = "Stop"

# ---- 1. locate repo root ----
$repoRoot = git rev-parse --show-toplevel 2>$null
if (-not $repoRoot) {
    Write-Host "FAIL: not inside a git repository" -ForegroundColor Red
    exit 1
}
$repoRoot = $repoRoot.Trim()
Write-Host "Repo root: $repoRoot" -ForegroundColor Cyan

# ---- 2. config toggles (standalone, not tied to install) ----
if ($OptOut) {
    git config hooks.route-lint.enabled false
    Write-Host "OK  hooks.route-lint.enabled = false (route-lint hook will SKIP)" -ForegroundColor Yellow
    exit 0
}
if ($OptIn) {
    git config --unset hooks.route-lint.enabled 2>$null | Out-Null
    Write-Host "OK  hooks.route-lint.enabled unset (hook is ACTIVE unless --no-verify)" -ForegroundColor Green
    exit 0
}

# ---- 3. locate source hooks dir and target ----
$srcDir  = Join-Path $repoRoot "hooks"
$dstDir  = Join-Path $repoRoot ".git\hooks"

if (-not (Test-Path $srcDir)) {
    Write-Host "FAIL: team hooks directory not found: $srcDir" -ForegroundColor Red
    Write-Host "      expected to contain pre-commit.bat" -ForegroundColor DarkGray
    exit 1
}
if (-not (Test-Path $dstDir)) {
    Write-Host "FAIL: .git/hooks not found at $dstDir" -ForegroundColor Red
    exit 1
}

$hookFiles = Get-ChildItem -Path $srcDir -File | Where-Object { $_.Name -notlike "*.sample" }
if (-not $hookFiles) {
    Write-Host "WARN: no hook files in $srcDir" -ForegroundColor Yellow
    exit 0
}

# ---- 4. plan ----
Write-Host ""
Write-Host ($(if ($Cleanup) { "CLEANUP" } else { "INSTALL" }) + " plan:") -ForegroundColor Cyan
foreach ($f in $hookFiles) {
    $dst = Join-Path $dstDir $f.Name
    $exists = Test-Path $dst
    $action = if ($Cleanup) { "SKIP (templates only)" } elseif ($exists -and -not $Force) { "SKIP exists (-Force to overwrite)" } else { "COPY" }
    $color  = if ($action -like "SKIP*") { "DarkGray" } else { "Green" }
    Write-Host ("  {0,-50} -> {1}  [{2}]" -f $f.Name, $dst, $action) -ForegroundColor $color
}

if (-not $Force -and -not $Cleanup) {
    Write-Host ""
    Write-Host "DRY RUN — no files changed." -ForegroundColor Yellow
    Write-Host "  use -Force to install" -ForegroundColor DarkGray
    Write-Host "  use -OptOut / -OptIn for config toggles" -ForegroundColor DarkGray
    exit 0
}

# ---- 5. execute ----
if ($Cleanup) {
    Write-Host ""
    Write-Host "Cleanup does nothing — templates are never removed." -ForegroundColor Yellow
    Write-Host "  to manually remove a hook: del .git\hooks\pre-commit.bat" -ForegroundColor DarkGray
    exit 0
}

$ok = 0; $fail = 0
foreach ($f in $hookFiles) {
    $dst = Join-Path $dstDir $f.Name
    try {
        Copy-Item -Path $f.FullName -Destination $dst -Force
        Write-Host "  OK  $($f.Name)" -ForegroundColor Green
        $ok++
    } catch {
        Write-Host "  FAIL $($f.Name) — $($_.Exception.Message)" -ForegroundColor Red
        $fail++
    }
}

Write-Host ""
Write-Host ("Done. OK={0}  FAIL={1}" -f $ok, $fail) -ForegroundColor Cyan
Write-Host ""
Write-Host "Verify:" -ForegroundColor Cyan
Write-Host "  git config --bool hooks.route-lint.enabled" -ForegroundColor DarkGray
Write-Host "  (empty = ACTIVE)" -ForegroundColor DarkGray
