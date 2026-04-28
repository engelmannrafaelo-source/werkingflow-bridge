#!/bin/bash
# =============================================================================
# Setup Script fuer Production Reserve Server (Server 2)
# =============================================================================
#
# Voraussetzungen:
#   - Neuer Hetzner Server mit Docker + Docker Compose
#   - SSH-Zugang eingerichtet
#   - Dieses Repo geklont nach /root/werkingflow-bridge
#
# Usage:
#   ./setup-prod-server.sh <PRIMARY_BRIDGE_IP> <PROD_SERVER_IP>
#
# Beispiel:
#   ./setup-prod-server.sh 49.12.72.66 <NEUE_SERVER_IP>
#
# =============================================================================

set -euo pipefail

PRIMARY_IP="${1:?Usage: $0 <PRIMARY_BRIDGE_IP> <PROD_SERVER_IP>}"
PROD_IP="${2:?Usage: $0 <PRIMARY_BRIDGE_IP> <PROD_SERVER_IP>}"

echo "============================================"
echo "  Bridge Production Reserve Setup"
echo "============================================"
echo "  Primary Bridge (Server 1): $PRIMARY_IP"
echo "  Production Reserve (Server 2): $PROD_IP"
echo "============================================"
echo ""

# --- Schritt 1: Secrets pruefen ---
echo "[1/5] Pruefe Secrets..."
if [ ! -f secrets/claude_token_prod.txt ]; then
    echo "FEHLER: secrets/claude_token_prod.txt nicht gefunden!"
    echo "  → Erstelle einen dedizierten Claude Account fuer Production"
    echo "  → Kopiere den OAuth Token nach secrets/claude_token_prod.txt"
    exit 1
fi

# Pruefen ob es ein Placeholder ist
if grep -q "PLACEHOLDER" secrets/claude_token_prod.txt 2>/dev/null; then
    echo "FEHLER: secrets/claude_token_prod.txt ist noch ein Placeholder!"
    echo "  → Ersetze den Inhalt mit dem echten OAuth Token"
    exit 1
fi
echo "  ✓ claude_token_prod.txt vorhanden"

# --- Schritt 2: .env erstellen ---
echo "[2/5] Erstelle .env fuer Production Server..."
cat > docker/.env.prod <<EOF
# Production Reserve Server Konfiguration
BRIDGE_PRIMARY_HOST=$PRIMARY_IP
TAVILY_API_KEY=${TAVILY_API_KEY:-}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
OPENAI_API_KEY=${OPENAI_API_KEY:-}
API_KEY=${API_KEY:-}
CONVERTAPI_SECRET=${CONVERTAPI_SECRET:-}
EOF
echo "  ✓ docker/.env.prod erstellt"

# --- Schritt 3: Build ---
echo "[3/5] Baue Docker Images..."
cd docker
docker compose -f docker-compose-prod.yml --env-file .env.prod build
echo "  ✓ Images gebaut"

# --- Schritt 4: Start ---
echo "[4/5] Starte Production Reserve..."
docker compose -f docker-compose-prod.yml --env-file .env.prod up -d
echo "  ✓ Container gestartet"

# --- Schritt 5: Health Check ---
echo "[5/5] Warte auf Health Check..."
sleep 10
if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "  ✓ Production Reserve ist HEALTHY"
else
    echo "  ⚠ Health Check fehlgeschlagen — pruefe docker compose logs"
    docker compose -f docker-compose-prod.yml logs --tail=20
    exit 1
fi

echo ""
echo "============================================"
echo "  Production Reserve ist LIVE!"
echo "============================================"
echo ""
echo "Naechste Schritte:"
echo "  1. Server 1 ($PRIMARY_IP) updaten:"
echo "     export BRIDGE_PROD_HOST=$PROD_IP"
echo "     cd docker && docker compose up -d --build"
echo ""
echo "  2. Testen:"
echo "     # Production-Call via Server 1 → sollte an Server 2 gehen"
echo "     curl -H 'X-Priority: production' http://$PRIMARY_IP:8000/health"
echo ""
echo "     # Direkt Server 2 testen"
echo "     curl http://$PROD_IP:8000/health"
echo ""
echo "  3. Workflows: X-Priority: production Header setzen"
echo "============================================"
