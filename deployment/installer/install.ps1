<#
HCWMA installer for a fresh Windows PC (build step 18).

Run it from the repository folder:   deployment\installer\install.cmd
(or: powershell -NoProfile -ExecutionPolicy Bypass -File deployment\installer\install.ps1)

What it does, in order. It never installs system software and never downloads the model
without asking:
  1. checks Windows and Python 3.10+ (if Python is missing it says where to get it, and stops)
  2. creates .venv in the repository (once) and installs the pinned requirements.txt into it
  3. checks Ollama is installed and running on 127.0.0.1:11434 (if not: says where to get it)
  4. asks before pulling the model qwen3.5:4b (about 3.3 GB)
  5. creates the per-install API token in the Windows Credential Manager (not shown)
  6. runs the HCWMA health check
  7. asks whether to put an "HCWMA" shortcut on the desktop and whether to start with Windows

Options:  -DryRun  only say what would be done     -SkipModel  do not offer the model pull
          -Yes     answer yes to the two shortcut questions (never to the model download)
Running it again is safe: finished steps are skipped.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipModel,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Venv = Join-Path $Repo ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$Model = "qwen3.5:4b"
$StartScript = Join-Path $Repo "deployment\scripts\start_hcwma.cmd"

function Step([string]$Text) { Write-Host ""; Write-Host "== $Text" -ForegroundColor Cyan }
function Ok([string]$Text) { Write-Host "   ok: $Text" -ForegroundColor Green }
function Info([string]$Text) { Write-Host "   $Text" }
function Fail([string]$Text) { Write-Host "   STOP: $Text" -ForegroundColor Red; exit 1 }
function Ask([string]$Question, [bool]$AutoYes = $false) {
    if ($AutoYes) { Info "$Question -> yes (-Yes)"; return $true }
    $answer = Read-Host "   $Question [y/N]"
    return $answer -match '^(y|yes)$'
}
function Run([string]$What, [scriptblock]$Action) {
    if ($DryRun) { Info "(dry run) would: $What"; return }
    & $Action
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { Fail "$What failed (exit code $LASTEXITCODE)" }
}

Write-Host "HCWMA installer  (repository: $Repo)"
if ($DryRun) { Write-Host "DRY RUN: nothing will be changed." -ForegroundColor Yellow }

# 1. Windows + Python ---------------------------------------------------------------------
Step "1/7 Windows and Python"
if ($env:OS -ne "Windows_NT") { Fail "this installer is for Windows." }
Ok "Windows"
$BasePython = $null
$BaseArgs = @()
$version = $null
foreach ($candidate in @(@("py", "-3"), @("python"))) {
    $exe = $candidate[0]
    $extra = @($candidate | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $found = & $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch { $found = $null }
    if ($found -and ([version]$found -ge [version]"3.10")) {
        $BasePython = $exe; $BaseArgs = $extra; $version = $found; break
    }
}
if (-not $BasePython -and -not (Test-Path $VenvPython)) {
    Fail "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this installer again."
}
if ($BasePython) { Ok "Python $version ($BasePython $($BaseArgs -join ' '))" }

# 2. .venv + requirements -----------------------------------------------------------------
Step "2/7 Python packages (.venv, pinned requirements.txt; PySide6 is about 640 MB)"
if (Test-Path $VenvPython) {
    Ok ".venv already exists"
} else {
    Run "create .venv" { & $BasePython @BaseArgs -m venv $Venv }
}
Run "install requirements.txt into .venv" {
    & $VenvPython -m pip install --disable-pip-version-check -q -r (Join-Path $Repo "requirements.txt")
}
if (-not $DryRun) { Ok "packages installed" }

# 3. Ollama -------------------------------------------------------------------------------
Step "3/7 Ollama (the local model runtime)"
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    Info "Ollama is not installed. Get it from https://ollama.com/download/windows and run this installer again."
    Info "(HCWMA itself is installed; it needs Ollama to answer.)"
} else {
    Ok "Ollama found: $($ollama.Source)"
    $up = $false
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 5
        $up = $true
    } catch { $up = $false }
    if ($up) { Ok "Ollama is running on 127.0.0.1:11434" } else { Info "Ollama is installed but not running: start the Ollama app." }

    # 4. Model ----------------------------------------------------------------------------
    Step "4/7 The model ($Model)"
    $have = $false
    if ($up) {
        try {
            $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 10
            $have = @($tags.models | Where-Object { $_.name -eq $Model }).Count -gt 0
        } catch { $have = $false }
    }
    if ($have) {
        Ok "$Model is already downloaded"
    } elseif ($SkipModel -or -not $up) {
        Info "not checked or skipped. Later: ollama pull $Model"
    } elseif (Ask "Download $Model now (about 3.3 GB)?") {
        Run "ollama pull $Model" { & ollama pull $Model }
    } else {
        Info "skipped. Later: ollama pull $Model"
    }
}

# 5. Token --------------------------------------------------------------------------------
Step "5/7 Per-install API token (kept in the Windows Credential Manager, not shown)"
Run "create the API token" {
    Push-Location $Repo
    try { & $VenvPython -c "from backend.auth.token import get_or_create_token; get_or_create_token()" }
    finally { Pop-Location }
}
if (-not $DryRun) { Ok "token ready" }

# 6. Health check -------------------------------------------------------------------------
Step "6/7 Health check"
Run "python -m backend.reliability health" {
    Push-Location $Repo
    try { & $VenvPython -m backend.reliability health } finally { Pop-Location }
}

# 7. Shortcuts ----------------------------------------------------------------------------
Step "7/7 Shortcuts"
$desktop = [Environment]::GetFolderPath("Desktop")
$link = Join-Path $desktop "HCWMA.lnk"
if (Test-Path $link) {
    Ok "desktop shortcut already there"
} elseif (Ask "Put an 'HCWMA' shortcut on your desktop?" $Yes) {
    Run "create the desktop shortcut" {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($link)
        $shortcut.TargetPath = $StartScript
        $shortcut.WorkingDirectory = $Repo
        $shortcut.WindowStyle = 7
        $shortcut.Description = "Start HCWMA"
        $shortcut.Save()
    }
}
if (Ask "Start HCWMA when you sign in to Windows? (you can change this in the tray later)" $Yes) {
    Run "turn on Start with Windows" {
        Push-Location $Repo
        try { & $VenvPython -m backend.reliability startup on } finally { Pop-Location }
    }
}

Write-Host ""
Write-Host "Done. Start HCWMA with the desktop shortcut, or: deployment\scripts\start_hcwma.cmd" -ForegroundColor Green
