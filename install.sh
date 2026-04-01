#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║              AI-Caller – Web-Installer / Web Installer                      ║
# ║  Nutzung / Usage:                                                           ║
# ║    curl -sSL https://raw.githubusercontent.com/USERNAME/ai-caller/main/install.sh | bash
# ║  Oder lokal / Or locally:                                                  ║
# ║    bash install.sh                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
set -e

# ── Farben / Colors ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step()    { echo -e "\n${BOLD}${BLUE}──── $1 ────${NC}"; }

# ── Konfiguration / Configuration ─────────────────────────────────────────────
REPO_URL="https://github.com/USERNAME/ai-caller"
INSTALL_DIR="${INSTALL_DIR:-/opt/ai-caller}"
SERVICE_USER="${SERVICE_USER:-www-data}"
PORT="${PORT:-5000}"
WHISPER_MODEL="${WHISPER_MODEL:-base}"

# ── Banner ─────────────────────────────────────────────────────────────────────
echo -e "${BOLD}${CYAN}"
echo "  ╔═══════════════════════════════════════╗"
echo "  ║        AI-Caller Installer v1.2       ║"
echo "  ║  KI-gestützter Anrufbeantworter /     ║"
echo "  ║  AI-powered answering machine         ║"
echo "  ╚═══════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  Installationsverzeichnis / Install dir: ${YELLOW}${INSTALL_DIR}${NC}"
echo -e "  Service-Port:                           ${YELLOW}${PORT}${NC}"
echo -e "  Whisper-Modell / Whisper model:         ${YELLOW}${WHISPER_MODEL}${NC}"
echo ""

# ── Root-Check ─────────────────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    error "Bitte als root ausführen / Please run as root: sudo bash $0"
fi

# ── OS-Erkennung / OS Detection ────────────────────────────────────────────────
step "1/7 System prüfen / Check system"
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_NAME="$NAME"
    OS_ID="$ID"
else
    OS_NAME="Unknown"
    OS_ID="unknown"
fi
info "Erkanntes System / Detected system: $OS_NAME"

# Proxmox LXC Container erkennen / Detect Proxmox LXC container
if [ -f /proc/1/environ ] && grep -q "container=lxc" /proc/1/environ 2>/dev/null; then
    info "Läuft in Proxmox LXC Container / Running in Proxmox LXC container"
fi

# Debian / Ubuntu Paketmanager
if ! command -v apt-get &>/dev/null; then
    error "Dieses System verwendet kein apt-get. Bitte Debian/Ubuntu/Proxmox LXC verwenden.\nThis system does not use apt-get. Please use Debian/Ubuntu/Proxmox LXC."
fi

# ── Abhängigkeiten / Dependencies ─────────────────────────────────────────────
step "2/7 Systemabhängigkeiten installieren / Install system dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git \
    curl \
    wget \
    espeak-ng \
    ffmpeg \
    libsndfile1 \
    portaudio19-dev \
    build-essential \
    sqlite3
success "Systemabhängigkeiten installiert / System dependencies installed"

# ── Projektquellen herunterladen / Download project sources ───────────────────
step "3/7 Projektdateien herunterladen / Download project files"
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Vorhandenes Repository wird aktualisiert / Updating existing repository..."
    git -C "$INSTALL_DIR" pull --quiet
else
    info "Klone Repository / Cloning repository..."
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"
fi
success "Projektdateien heruntergeladen / Project files downloaded"

cd "$INSTALL_DIR"

# ── Python-Virtualenv ──────────────────────────────────────────────────────────
step "4/7 Python-Umgebung einrichten / Set up Python environment"
VENV_DIR="$INSTALL_DIR/venv"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    info "Virtualenv erstellt / Virtualenv created"
fi
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r "$INSTALL_DIR/requirements.txt" -q
success "Python-Pakete installiert / Python packages installed"

# ── Verzeichnisse & Berechtigungen / Directories & permissions ────────────────
step "5/7 Verzeichnisse erstellen / Create directories"
mkdir -p "$INSTALL_DIR/data/recordings"
mkdir -p "$INSTALL_DIR/models/piper"
mkdir -p "$INSTALL_DIR/ssl"

# Service-User prüfen / Check service user
if id "$SERVICE_USER" &>/dev/null; then
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data" 2>/dev/null || true
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/models" 2>/dev/null || true
fi
success "Verzeichnisse erstellt / Directories created"

# ── Whisper-Modell / Whisper model ────────────────────────────────────────────
step "6/7 KI-Modelle vorbereiten / Prepare AI models"
info "Lade Whisper-Spracherkennungsmodell / Downloading Whisper speech recognition model ($WHISPER_MODEL)..."
source "$VENV_DIR/bin/activate"
python3 -c "
from faster_whisper import WhisperModel
import os
model = os.environ.get('WHISPER_MODEL', 'base')
print(f'  Lade Modell: {model}')
m = WhisperModel(model, device='cpu', compute_type='int8')
print(f'  Whisper-Modell \"{model}\" bereit.')
" WHISPER_MODEL="$WHISPER_MODEL" 2>/dev/null || warn "Whisper-Download fehlgeschlagen – wird beim ersten Anruf geladen / Download failed – will load on first call"

# ── Systemd-Service & Startskript / Systemd service & start script ─────────────
step "7/7 Service einrichten / Set up service"

# start.sh erstellen / Create start.sh
cat > "$INSTALL_DIR/start.sh" << STARTSCRIPT
#!/bin/bash
PROJECT_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
source "\$PROJECT_DIR/venv/bin/activate"
cd "\$PROJECT_DIR"
exec gunicorn --worker-class eventlet -w 1 \\
    --bind 0.0.0.0:${PORT} \\
    --timeout 120 \\
    --log-level info \\
    "app:app"
STARTSCRIPT
chmod +x "$INSTALL_DIR/start.sh"

# Systemd-Service erstellen / Create systemd service
SERVICE_FILE="/etc/systemd/system/ai-caller.service"
cat > "$SERVICE_FILE" << SERVICEEOF
[Unit]
Description=AI-Caller – KI-Anrufbeantworter / AI answering machine
After=network.target
Documentation=https://github.com/USERNAME/ai-caller

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:${PORT} --timeout 120 app:app
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable ai-caller --quiet
success "Service konfiguriert / Service configured"

# ── Optionale Piper-TTS Modelle / Optional Piper TTS models ───────────────────
MODELS_DIR="$INSTALL_DIR/models/piper"
echo ""
echo -e "${BOLD}${YELLOW}══════ Optionaler Schritt / Optional step ══════${NC}"
echo -e "Für lokale Sprachausgabe (kein Internet nötig) Piper-TTS installieren:"
echo -e "For local text-to-speech (no internet required) install Piper TTS:"
echo ""
echo -e "  ${CYAN}# Thorsten-Low (~30 MB, Deutsch / German):${NC}"
echo "  wget -P $MODELS_DIR \\"
echo "    https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/low/de_DE-thorsten-low.onnx \\"
echo "    https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/low/de_DE-thorsten-low.onnx.json"
echo ""
echo -e "  ${CYAN}# Thorsten-Medium (~75 MB, bessere Qualität / better quality):${NC}"
echo "  wget -P $MODELS_DIR \\"
echo "    https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx \\"
echo "    https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json"
echo ""
echo "  Dann im Web-Interface: Admin → KI-Einstellungen → TTS-Modus = Lokal (Piper)"
echo "  Then in web interface: Admin → AI Settings → TTS mode = Local (Piper)"
echo ""

# ── Abschluss / Finish ────────────────────────────────────────────────────────
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║  ✓ Installation abgeschlossen / Installation done!  ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  ${BOLD}Service starten / Start service:${NC}"
echo -e "    ${CYAN}systemctl start ai-caller${NC}"
echo ""
echo -e "  ${BOLD}Web-Interface:${NC}"
echo -e "    ${CYAN}http://${HOST_IP}:${PORT}${NC}"
echo ""
echo -e "  ${BOLD}Logs / Logs:${NC}"
echo -e "    ${CYAN}journalctl -u ai-caller -f${NC}"
echo ""
echo -e "  ${BOLD}Nächste Schritte / Next steps:${NC}"
echo "  1. Web-Interface öffnen und Admin-Konto anlegen"
echo "     Open web interface and create admin account"
echo "  2. Admin → Begrüßung & Verhalten konfigurieren"
echo "     Admin → Configure greeting & behavior"
echo "  3. Admin → KI-Einstellungen (STT/TTS/LLM)"
echo "     Admin → AI settings (STT/TTS/LLM)"
echo "  4. Admin → Telefonanschluss verbinden (FritzBox oder SIP)"
echo "     Admin → Connect phone line (FritzBox or SIP)"
echo "  5. Optional: Admin → Benachrichtigungen (Telegram/E-Mail)"
echo "     Optional: Admin → Notifications (Telegram/email)"
echo ""
