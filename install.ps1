# ==============================================================================
# Spotify Downloader — Automated 1-Line Installer for Windows
# Usage: irm https://raw.githubusercontent.com/mehuljain866/spotify-dl/main/install.ps1 | iex
# ==============================================================================

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "  ===========================================" -ForegroundColor Green
Write-Host "     Spotify Downloader — Windows Installer  " -ForegroundColor Green
Write-Host "  ===========================================" -ForegroundColor Green
Write-Host ""

# 1. Locate or Install Python
Write-Host "[*] Checking Python installation..." -ForegroundColor Cyan
$pythonExe = $null

if (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonExe = (Get-Command python).Source
} elseif (Test-Path "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe") {
    $pythonExe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
} else {
    $foundPy = Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python" -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($foundPy) {
        $pythonExe = $foundPy.FullName
    }
}

if (-not $pythonExe) {
    Write-Host "[!] Python 3 not detected. Attempting automatic installation via winget..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
        $pythonExe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    } else {
        Write-Host "[X] winget not found. Please install Python 3.10+ from https://www.python.org/downloads/ and check 'Add to PATH'." -ForegroundColor Red
        return
    }
}

Write-Host "    Found Python: $pythonExe" -ForegroundColor DarkGray

# Determine pythonw
$pythonwExe = $pythonExe -replace "python\.exe$", "pythonw.exe"
if (-not (Test-Path $pythonwExe)) {
    $pythonwExe = $pythonExe
}

# 2. Setup Destination Folder
$installDir = "$env:USERPROFILE\spotify-dl"
Write-Host "[*] Installing to: $installDir" -ForegroundColor Cyan

if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
}

# 3. Clone or Download Project
if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "[*] Fetching latest files via Git..." -ForegroundColor Cyan
    if (Test-Path "$installDir\.git") {
        git -C $installDir pull --quiet
    } else {
        git clone --depth 1 https://github.com/mehuljain866/spotify-dl.git $installDir
    }
} else {
    Write-Host "[*] Downloading project archive..." -ForegroundColor Cyan
    $zipUrl = "https://github.com/mehuljain866/spotify-dl/archive/refs/heads/main.zip"
    $zipPath = "$env:TEMP\spotify-dl-main.zip"
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath "$env:TEMP\spotify-dl-extract" -Force
    Copy-Item -Path "$env:TEMP\spotify-dl-extract\spotify-dl-main\*" -Destination $installDir -Recurse -Force
    Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
    Remove-Item -Path "$env:TEMP\spotify-dl-extract" -Recurse -Force -ErrorAction SilentlyContinue
}

# 4. Install Dependencies
Write-Host "[*] Installing required packages (CustomTkinter, yt-dlp, mutagen, spotapi)..." -ForegroundColor Cyan
& $pythonExe -m pip install -r "$installDir\requirements.txt" --quiet --upgrade

# 5. Create Desktop Shortcut
Write-Host "[*] Creating Desktop shortcut..." -ForegroundColor Cyan
$desktopDir = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopDir "Spotify Downloader.lnk"
$iconPath = Join-Path $installDir "icon.ico"

$wscript = New-Object -ComObject WScript.Shell
$shortcut = $wscript.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonwExe
$shortcut.Arguments = "`"$installDir\gui.py`""
$shortcut.WorkingDirectory = $installDir
if (Test-Path $iconPath) {
    $shortcut.IconLocation = $iconPath
}
$shortcut.Description = "Spotify Downloader"
$shortcut.Save()

Write-Host ""
Write-Host "  ===========================================" -ForegroundColor Green
Write-Host "     Installation Complete Successfully!     " -ForegroundColor Green
Write-Host "  ===========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Shortcut placed on your Desktop: 'Spotify Downloader'" -ForegroundColor Yellow
Write-Host "  Launching Spotify Downloader now..." -ForegroundColor Cyan
Write-Host ""

Start-Process $pythonwExe -ArgumentList "`"$installDir\gui.py`"" -WorkingDirectory $installDir
