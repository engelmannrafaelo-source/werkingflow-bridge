#!/bin/bash
# =============================================================================
# Claude Account Switcher
# Wechselt zwischen 3 vordefinierten Accounts (OHNE neue Tokens zu generieren!)
# =============================================================================

SECRETS_DIR="/root/projekte/werkingflow/bridge/secrets"
HETZNER="root@49.12.72.66"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

case "$1" in
  1|2|3)
    ACCOUNT=$1
    TOKEN=$(cat "$SECRETS_DIR/claude_token_account${ACCOUNT}.txt" 2>/dev/null)

    if [ -z "$TOKEN" ]; then
      echo -e "${RED}Token $ACCOUNT nicht gefunden!${NC}"
      echo "Bitte zuerst einrichten: ./setup-tokens.sh"
      exit 1
    fi

    echo -e "${YELLOW}Wechsle zu Account $ACCOUNT...${NC}"

    # 1. Lokal setzen
    echo "$TOKEN" > ~/.claude_token
    export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"
    echo -e "${GREEN}✓ Lokal gesetzt${NC}"

    # 2. Hetzner Worker updaten
    ssh $HETZNER "echo '$TOKEN' > /root/werkingflow-bridge/secrets/claude_token_account1.txt"
    ssh $HETZNER "cd /root/werkingflow-bridge/docker && docker compose -f docker-compose.multi.yml restart worker1" 2>/dev/null
    echo -e "${GREEN}✓ Hetzner Worker neu gestartet${NC}"

    echo -e "\n${GREEN}Fertig! Starte VSCode neu für lokale Änderung.${NC}"
    ;;

  status)
    echo "=== Token Status ==="
    for i in 1 2 3; do
      if [ -f "$SECRETS_DIR/claude_token_account${i}.txt" ]; then
        TOKEN=$(cat "$SECRETS_DIR/claude_token_account${i}.txt")
        echo "Account $i: ${TOKEN:0:30}..."
      else
        echo "Account $i: NICHT GESETZT"
      fi
    done
    ;;

  *)
    echo "Usage: $0 <1|2|3|status>"
    echo ""
    echo "  1/2/3  = Wechsle zu Account"
    echo "  status = Zeige alle Tokens"
    ;;
esac
