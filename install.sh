#!/usr/bin/env bash
# ==============================================================================
# Spotify Downloader — Automated 1-Line Installer for macOS & Linux
# Usage: curl -fsSL https://raw.githubusercontent.com/mehuljain866/spotify-dl/main/install.sh | bash
# ==============================================================================

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}   Spotify Downloader — macOS / Linux Setup ${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""

OS_TYPE="$(uname -s)"

# 1. Check Python 3
echo -e "${CYAN}[*] Checking Python 3...${NC}"
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info.major >= 3 else 1)'; then
    PYTHON_CMD="python"
else
    echo -e "${RED}[X] Python 3 is required but was not found.${NC}"
    if [ "$OS_TYPE" = "Darwin" ]; then
        echo -e "${YELLOW}Install Python with Homebrew: brew install python3${NC}"
    else
        echo -e "${YELLOW}Install Python via your package manager: sudo apt install python3 python3-pip python3-venv${NC}"
    fi
    exit 1
fi

echo -e "    Found: $("$PYTHON_CMD" --version)"

# 2. Check FFmpeg (recommended)
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo -e "${YELLOW}[!] FFmpeg not found in PATH. Audio conversion works best with FFmpeg installed.${NC}"
    if [ "$OS_TYPE" = "Darwin" ]; then
        echo -e "    To install: brew install ffmpeg"
    else
        echo -e "    To install: sudo apt install ffmpeg  (or sudo dnf install ffmpeg / sudo pacman -S ffmpeg)"
    fi
fi

# 3. Setup Install Directory
INSTALL_DIR="$HOME/.spotify-dl"
echo -e "${CYAN}[*] Installing to: $INSTALL_DIR...${NC}"
mkdir -p "$INSTALL_DIR"

if command -v git >/dev/null 2>&1; then
    if [ -d "$INSTALL_DIR/.git" ]; then
        git -C "$INSTALL_DIR" pull --quiet
    else
        git clone --depth 1 https://github.com/mehuljain866/spotify-dl.git "$INSTALL_DIR" --quiet
    fi
else
    echo -e "${CYAN}[*] Downloading repository archive...${NC}"
    ARCHIVE_URL="https://github.com/mehuljain866/spotify-dl/archive/refs/heads/main.tar.gz"
    curl -fsSL "$ARCHIVE_URL" | tar -xz -C "$INSTALL_DIR" --strip-components=1
fi

# 4. Setup Python Virtual Environment & Dependencies
echo -e "${CYAN}[*] Setting up virtual environment...${NC}"
"$PYTHON_CMD" -m venv "$INSTALL_DIR/venv"
VENV_PIP="$INSTALL_DIR/venv/bin/pip"
VENV_PY="$INSTALL_DIR/venv/bin/python"

echo -e "${CYAN}[*] Installing dependencies (CustomTkinter, yt-dlp, mutagen, spotapi)...${NC}"
"$VENV_PIP" install --upgrade pip --quiet
"$VENV_PIP" install -r "$INSTALL_DIR/requirements.txt" --quiet

# 5. Create CLI command 'spotify-dl'
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
cat << EOF > "$BIN_DIR/spotify-dl"
#!/usr/bin/env bash
exec "$VENV_PY" "$INSTALL_DIR/gui.py" "\$@"
EOF
chmod +x "$BIN_DIR/spotify-dl"

# 6. Desktop Launcher creation
if [ "$OS_TYPE" = "Darwin" ]; then
    # macOS: Create a .command launcher on Desktop
    LAUNCHER="$HOME/Desktop/Spotify Downloader.command"
    cat << EOF > "$LAUNCHER"
#!/usr/bin/env bash
exec "$VENV_PY" "$INSTALL_DIR/gui.py"
EOF
    chmod +x "$LAUNCHER"
    echo -e "${GREEN}[+] Created Desktop launcher:${NC} ~/Desktop/Spotify Downloader.command"
else
    # Linux: Create .desktop entry
    DESKTOP_ENTRY_DIR="$HOME/.local/share/applications"
    mkdir -p "$DESKTOP_ENTRY_DIR"
    cat << EOF > "$DESKTOP_ENTRY_DIR/spotify-dl.desktop"
[Desktop Entry]
Name=Spotify Downloader
Comment=Download Spotify tracks and playlists with full tags & cover art
Exec=$VENV_PY $INSTALL_DIR/gui.py
Icon=$INSTALL_DIR/icon.ico
Terminal=false
Type=Application
Categories=AudioVideo;Audio;
EOF
    chmod +x "$DESKTOP_ENTRY_DIR/spotify-dl.desktop"
    echo -e "${GREEN}[+] Created desktop entry:${NC} Spotify Downloader (available in application launcher)"
fi

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}   Installation Complete!                   ${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "Run via terminal:  ${YELLOW}spotify-dl${NC}  (ensure ~/.local/bin is in your PATH)"
echo -e "Launching Spotify Downloader now..."
echo ""

nohup "$VENV_PY" "$INSTALL_DIR/gui.py" >/dev/null 2>&1 &
