# AI-Caller – KI-Anrufbeantworter / AI Answering Machine

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/Flask-2.3%2B-green?logo=flask" alt="Flask">
  <img src="https://img.shields.io/badge/VoIP-SIP%20%2F%20FritzBox-orange" alt="VoIP">
  <img src="https://img.shields.io/badge/KI-Ollama%20%7C%20Groq%20%7C%20OpenAI-purple" alt="AI">
  <img src="https://img.shields.io/badge/Lizenz-MIT-lightgrey" alt="MIT License">
</p>

---

## 🇩🇪 Deutsch

**AI-Caller** ist ein intelligenter, KI-gestützter Anrufbeantworter, der eingehende Telefonanrufe entgegennimmt, in Echtzeit transkribiert und mithilfe eines Large Language Models (LLM) verständnisvoll antwortet. Er kann Termine vereinbaren, Nachrichten zusammenfassen und Benachrichtigungen per Telegram oder E-Mail versenden.

### ✨ Funktionen

| Funktion | Beschreibung |
|---|---|
| 📞 SIP / FritzBox | Automatische Annahme von Anrufen über VoIP-SIP oder FritzBox TR-064 |
| 🎤 STT | Spracherkennung: lokal (faster-whisper) oder Cloud (OpenAI, Groq) |
| 🔊 TTS | Sprachausgabe: lokal (Piper), Edge/Microsoft, ElevenLabs, Google, Azure |
| 🤖 LLM | Konversation: lokal (Ollama) oder Cloud (Groq, OpenAI) |
| 📅 Kalender | Integration mit Google Calendar und Nextcloud CalDAV |
| 🔍 Web-Suche | Brave Search API für aktuelle Informationen |
| 📨 Posteingang | Web-Oberfläche zum Lesen und Beantworten von Anrufzusammenfassungen |
| 🔔 Benachrichtigungen | Telegram-Bot und E-Mail für Anrufbenachrichtigungen |
| ⏰ Zeitplan | Konfigurierbare Aktivierungszeiten (Tage, Uhrzeiten) |
| 🔐 Auth | Benutzerverwaltung mit optionaler 2-Faktor-Authentifizierung |
| 🌐 Web-UI | Vollständiges Admin-Dashboard im Browser |

### 🚀 Schnellinstallation (Proxmox LXC / Debian / Ubuntu)

**Via curl (empfohlen):**
```bash
curl -sSL https://raw.githubusercontent.com/redstar1978/ai-caller/main/install.sh | bash
```

**Mit eigenem Installationsverzeichnis:**
```bash
INSTALL_DIR=/opt/ai-caller PORT=8080 bash <(curl -sSL https://raw.githubusercontent.com/redstar1978/ai-caller/main/install.sh)
```

**Manuell (lokale Kopie):**
```bash
git clone https://github.com/redstar1978/ai-caller.git /opt/ai-caller
cd /opt/ai-caller
bash install.sh
```

### 📋 Voraussetzungen

- Debian 12 / Ubuntu 22.04+ (oder Proxmox LXC-Container darauf basierend)
- Python 3.10 oder neuer
- 2 GB RAM (empfohlen: 4 GB für lokale KI-Modelle)
- 5 GB freier Speicher (10 GB mit lokalen TTS/STT-Modellen)
- SIP-fähiger Router (z. B. FritzBox) oder externer SIP-Anbieter

### ⚙️ Konfiguration

Nach der Installation:

1. **Service starten:**
   ```bash
   systemctl start ai-caller
   ```
2. **Web-Interface öffnen:** `http://<IP>:5000`
3. **Erstes Admin-Konto anlegen** (beim ersten Aufruf automatisch)
4. Im Admin-Bereich konfigurieren:
   - **Begrüßung & Verhalten:** Name des Assistenten, Ansagetext, KI-Persönlichkeit
   - **KI-Einstellungen:** STT/TTS/LLM-Provider auswählen
   - **Telefonanschluss:** FritzBox automatisch erkennen oder manuell SIP eingeben
   - **Benachrichtigungen:** Telegram-Bot oder E-Mail einrichten

### 🔌 Integrationen

#### Lokale KI (kein Internet erforderlich)
- **LLM:** [Ollama](https://ollama.ai) – beliebige lokale Modelle (llama3, qwen, mistral, ...)
- **STT:** faster-whisper (base, small, medium, large-v3)
- **TTS:** [Piper](https://github.com/rhasspy/piper) – deutsche Stimmen vorinstallierbar

#### Cloud KI
- **Groq** – schnelles Inference (kostenloser Tier verfügbar)
- **OpenAI** – GPT-4, Whisper, TTS
- **ElevenLabs** – hochwertige KI-Stimmen
- **Google Cloud TTS / Azure TTS**

#### Kommunikation
- **Telegram:** Bot-Benachrichtigungen, 2FA-Codes, Rückantworten aus dem Chat
- **E-Mail:** SMTP-Benachrichtigungen (Gmail, Outlook, eigener Server)
- **Nextcloud CalDAV:** Kalenderabfrage und Terminbuchung

### 🐳 Proxmox LXC Setup (Schritt-für-Schritt)

1. **LXC-Container erstellen** (Debian 12 Minimal):
   - 2 CPU-Kerne, 2 GB RAM, 10 GB Disk
   - Netzwerk: statische IP empfohlen

2. **In den Container einloggen und installieren:**
   ```bash
   curl -sSL https://raw.githubusercontent.com/redstar1978/ai-caller/main/install.sh | bash
   ```

3. **FritzBox-SIP einrichten:**
   - FritzBox → Telefonie → Telefoniegeräte → Neues Gerät hinzufügen → Telefon (VoIP)
   - Zugangsdaten im AI-Caller Admin-Panel eintragen

4. **Optional: Piper TTS-Modell herunterladen:**
   ```bash
   mkdir -p /opt/ai-caller/models/piper
   cd /opt/ai-caller/models/piper
   wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx
   wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json
   ```

### 📁 Projektstruktur

```
ai-caller/
├── app.py                  # Flask-Anwendung, Routen
├── ai_engine.py            # STT/TTS/LLM-Logik
├── sip_handler.py          # VoIP/SIP-Integration (pyVoIP)
├── fritzbox_setup.py       # FritzBox TR-064 Autodiscovery
├── ollama_discovery.py     # Lokale Ollama-Instanz erkennen
├── agent_tools.py          # KI-Werkzeuge (Kalender, Websuche)
├── notifications.py        # Telegram / E-Mail Benachrichtigungen
├── auth.py                 # Authentifizierung / 2FA
├── database.py             # SQLite-Datenbankzugriff
├── templates/              # HTML-Templates (Jinja2)
│   ├── admin/              # Admin-Einstellungsseiten
│   └── *.html              # Öffentliche Seiten
├── static/                 # CSS / JavaScript
├── install.sh              # Web-Installer
├── requirements.txt        # Python-Abhängigkeiten
└── data/                   # Laufzeitdaten (wird per .gitignore ausgeschlossen)
    └── ai_caller.db        # SQLite-Datenbank (nach Installation)
```

### 🔒 Sicherheitshinweise

- Die Datenbank `data/ai_caller.db` enthält alle Konfigurationsdaten inkl. API-Schlüssel
- Zugangsbeschränkung auf das Web-Interface empfohlen (Firewall, VPN, nginx-Proxy)
- SSL-Zertifikate können in `ssl/cert.pem` + `ssl/key.pem` hinterlegt werden
- Regelmäßige Sicherung der `data/`-Verzeichnisse empfohlen

---

## 🇬🇧 English

**AI-Caller** is an intelligent, AI-powered answering machine that answers incoming phone calls, transcribes them in real-time, and responds intelligently using a Large Language Model (LLM). It can schedule appointments, summarize messages, and send notifications via Telegram or email.

### ✨ Features

| Feature | Description |
|---|---|
| 📞 SIP / FritzBox | Automatic call answering via VoIP-SIP or FritzBox TR-064 |
| 🎤 STT | Speech recognition: local (faster-whisper) or cloud (OpenAI, Groq) |
| 🔊 TTS | Text-to-speech: local (Piper), Edge/Microsoft, ElevenLabs, Google, Azure |
| 🤖 LLM | Conversation: local (Ollama) or cloud (Groq, OpenAI) |
| 📅 Calendar | Integration with Google Calendar and Nextcloud CalDAV |
| 🔍 Web search | Brave Search API for current information |
| 📨 Inbox | Web interface for reading and replying to call summaries |
| 🔔 Notifications | Telegram bot and email for call notifications |
| ⏰ Schedule | Configurable activation times (days, hours) |
| 🔐 Auth | User management with optional two-factor authentication |
| 🌐 Web UI | Full admin dashboard in the browser |

### 🚀 Quick Installation (Proxmox LXC / Debian / Ubuntu)

**Via curl (recommended):**
```bash
curl -sSL https://raw.githubusercontent.com/redstar1978/ai-caller/main/install.sh | bash
```

**With custom install directory:**
```bash
INSTALL_DIR=/opt/ai-caller PORT=8080 bash <(curl -sSL https://raw.githubusercontent.com/redstar1978/ai-caller/main/install.sh)
```

**Manual (local copy):**
```bash
git clone https://github.com/redstar1978/ai-caller.git /opt/ai-caller
cd /opt/ai-caller
bash install.sh
```

### 📋 Requirements

- Debian 12 / Ubuntu 22.04+ (or Proxmox LXC container based on these)
- Python 3.10 or newer
- 2 GB RAM (recommended: 4 GB for local AI models)
- 5 GB free storage (10 GB with local TTS/STT models)
- SIP-capable router (e.g. FritzBox) or external SIP provider

### ⚙️ Configuration

After installation:

1. **Start the service:**
   ```bash
   systemctl start ai-caller
   ```
2. **Open web interface:** `http://<IP>:5000`
3. **Create first admin account** (automatic on first visit)
4. Configure in the admin area:
   - **Greeting & Behavior:** Assistant name, greeting text, AI personality
   - **AI Settings:** Choose STT/TTS/LLM provider
   - **Phone Connection:** Auto-detect FritzBox or enter SIP credentials manually
   - **Notifications:** Set up Telegram bot or email

### 🔌 Integrations

#### Local AI (no internet required)
- **LLM:** [Ollama](https://ollama.ai) – any local model (llama3, qwen, mistral, ...)
- **STT:** faster-whisper (base, small, medium, large-v3)
- **TTS:** [Piper](https://github.com/rhasspy/piper) – German voices installable

#### Cloud AI
- **Groq** – fast inference (free tier available)
- **OpenAI** – GPT-4, Whisper, TTS
- **ElevenLabs** – high-quality AI voices
- **Google Cloud TTS / Azure TTS**

#### Communication
- **Telegram:** bot notifications, 2FA codes, replies from chat
- **Email:** SMTP notifications (Gmail, Outlook, custom server)
- **Nextcloud CalDAV:** calendar queries and appointment booking

### 🐳 Proxmox LXC Setup (Step-by-Step)

1. **Create LXC container** (Debian 12 Minimal):
   - 2 CPU cores, 2 GB RAM, 10 GB disk
   - Network: static IP recommended

2. **Log into the container and install:**
   ```bash
   curl -sSL https://raw.githubusercontent.com/redstar1978/ai-caller/main/install.sh | bash
   ```

3. **Set up FritzBox SIP:**
   - FritzBox → Telephony → Telephony Devices → Add new device → Phone (VoIP)
   - Enter credentials in the AI-Caller admin panel

4. **Optional: Download Piper TTS model:**
   ```bash
   mkdir -p /opt/ai-caller/models/piper
   cd /opt/ai-caller/models/piper
   wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx
   wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json
   ```

### 🔒 Security Notes

- The database `data/ai_caller.db` contains all config data including API keys
- Restricting access to the web interface is recommended (firewall, VPN, nginx proxy)
- SSL certificates can be stored in `ssl/cert.pem` + `ssl/key.pem`
- Regular backups of the `data/` directory are recommended

### 🤝 Contributing

Pull requests and issues are welcome! Please ensure no personal data, API keys, or passwords are included in contributions.

### 📜 License

[MIT License](LICENSE) – free to use, modify, and distribute.
