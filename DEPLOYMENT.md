# EDEAIBridge Deployment Guide

## Hetzner Cloud Deployment (Empfohlen)

**Kosten: €5.49/Monat** für einen CX33 Server (4 vCPU, 8GB RAM, 80GB SSD)

### Schritt 1: Hetzner Account & Server erstellen

1. **Account erstellen**: https://accounts.hetzner.com/signUp
2. **Neues Projekt** erstellen in der Hetzner Cloud Console
3. **Server erstellen**:
   - Location: Falkenstein oder Nürnberg (DE)
   - Image: Ubuntu 24.04
   - Type: CX33 (€5.49/Mo) oder CX22 (€3.79/Mo für weniger RAM)
   - SSH Key hinzufügen (wichtig!)
   - Hostname: `edeaibridge`

### Schritt 2: Server einrichten

```bash
# SSH zum Server
ssh root@YOUR_SERVER_IP

# System aktualisieren
apt update && apt upgrade -y

# Docker installieren
curl -fsSL https://get.docker.com | sh

# Docker Compose installieren (falls nicht enthalten)
apt install docker-compose-plugin -y

# User für Docker erstellen
useradd -m -s /bin/bash deploy
usermod -aG docker deploy

# Als deploy user wechseln
su - deploy
```

### Schritt 3: EDEAIBridge deployen

```bash
# Repository klonen
git clone https://github.com/YOURUSERNAME/EDEAIBridge.git
cd EDEAIBridge

# Secrets erstellen
mkdir -p secrets

# Claude OAuth Token einfügen (von deinem lokalen Rechner kopieren)
# Auf deinem Mac: cat ~/.claude/.credentials | grep token
echo "sk-ant-oat01-DEIN-TOKEN-HIER" > secrets/claude_token.txt
chmod 600 secrets/claude_token.txt

# Environment konfigurieren
cp .env.example .env
nano .env  # TAVILY_API_KEY eintragen

# Starten
./start.sh
```

### Schritt 4: Firewall konfigurieren

In der Hetzner Cloud Console:
1. Firewall → Create Firewall
2. Regeln hinzufügen:
   - SSH (TCP 22) - Nur deine IP
   - HTTP (TCP 8000) - 0.0.0.0/0 (oder nur Report Studio IPs)
3. Firewall dem Server zuweisen

### Schritt 5: Domain & SSL (Optional aber empfohlen)

```bash
# Caddy als Reverse Proxy installieren
apt install caddy -y

# Caddyfile erstellen
cat > /etc/caddy/Caddyfile << 'EOF'
edeaibridge.yourdomain.com {
    reverse_proxy localhost:8000
}
EOF

# Caddy starten (automatisches SSL!)
systemctl restart caddy
```

Jetzt erreichbar unter: `https://edeaibridge.yourdomain.com`

---

## Schnell-Test nach Deployment

```bash
# Health Check
curl http://YOUR_SERVER_IP:8000/health

# Chat Test
curl -X POST http://YOUR_SERVER_IP:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer test" \
  -d '{"model":"claude-sonnet-4","messages":[{"role":"user","content":"Hi!"}]}'
```

---

## Report Studio Integration

In deiner Report Studio `.env`:

```env
# Cloud deployment
EDEAIBRIDGE_URL=https://edeaibridge.yourdomain.com/v1

# Oder mit IP (ohne SSL)
EDEAIBRIDGE_URL=http://YOUR_SERVER_IP:8000/v1
```

In deinem Code:
```typescript
const AI_BASE_URL = process.env.EDEAIBRIDGE_URL || 'http://localhost:8000/v1';

const response = await fetch(`${AI_BASE_URL}/chat/completions`, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer any-key'
  },
  body: JSON.stringify({
    model: 'claude-sonnet-4',
    messages: [{ role: 'user', content: 'Generate report...' }]
  })
});
```

---

## Quick-Update Workflow (Häufig genutzt!)

**Wenn du lokale Änderungen auf Hetzner deployen willst:**

```bash
# 1. Lokal: Änderungen committen und pushen
cd /Users/rafael/Documents/GitHub/ai-bridge
git add -A && git commit -m "fix: beschreibung" && git push

# 2. SSH zum Hetzner Server
ssh root@95.217.180.242

# 3. Auf Hetzner: Pullen und neu bauen
cd /root/ai-bridge
git pull
docker-compose down && docker-compose build --no-cache && docker-compose up -d

# 4. Verifizieren
curl http://localhost:8000/health
curl http://localhost:8000/v1/privacy/status
```

**One-Liner für Hetzner (wenn schon per SSH verbunden):**
```bash
cd /root/ai-bridge && git pull && docker-compose down && docker-compose build --no-cache && docker-compose up -d && sleep 30 && curl http://localhost:8000/health
```

---

## Wartung & Monitoring

### Logs anschauen
```bash
cd ~/EDEAIBridge
./logs.sh
```

### Container neustarten
```bash
./restart.sh
```

### Updates einspielen
```bash
cd ~/EDEAIBridge
git pull
./restart.sh
```

### Systemd Service für Auto-Start (optional)

```bash
sudo cat > /etc/systemd/system/edeaibridge.service << 'EOF'
[Unit]
Description=EDEAIBridge
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/deploy/EDEAIBridge
ExecStart=/home/deploy/EDEAIBridge/start.sh
ExecStop=/home/deploy/EDEAIBridge/stop.sh
User=deploy

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable edeaibridge
sudo systemctl start edeaibridge
```

---

## Troubleshooting

### Container startet nicht
```bash
# Logs prüfen
docker logs edeaibridge

# Token prüfen
cat secrets/claude_token.txt | head -c 20
```

### Health Check schlägt fehl
```bash
# Container Status
docker ps -a

# Manuell im Container testen
docker exec -it edeaibridge curl http://localhost:8000/health
```

### Timeout bei langen Requests
Der Server ist für 40+ Minuten Requests konfiguriert. Bei Problemen:
```bash
# In docker-compose.yml prüfen
MAX_TIMEOUT=2400000  # 40 Minuten in ms
```

---

## Kosten-Übersicht

| Komponente | Kosten/Monat |
|------------|--------------|
| Hetzner CX33 | €5.49 |
| Domain (optional) | €1-2 |
| **Total** | **~€6-8** |

Claude API Kosten: **€0** (OAuth Token = kostenlos!)
