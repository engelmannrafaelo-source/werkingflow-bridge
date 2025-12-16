# AI-Bridge Client SDK

Smart URL resolution with optional fallback from Hetzner to local.

## Verhalten

```
1. Hetzner Health-Check (3s Timeout)
   └─ OK? → Nutze Hetzner
   └─ Fail? ↓
2. Local Health-Check (1s Timeout) [nur wenn fallback aktiviert]
   └─ OK? → Nutze Local + Warning
   └─ Fail? → AIBridgeConnectionError
```

## Python

### Installation

```bash
# Kopiere sdk/python/ in dein Projekt oder füge zum PYTHONPATH hinzu
cp -r sdk/python/ /path/to/your/project/ai_bridge_sdk/
```

### Usage

```python
from ai_bridge_sdk import get_bridge_url, create_client

# Nur Hetzner (Default, kein Fallback)
url = get_bridge_url()

# Mit Fallback auf Local
url = get_bridge_url(fallback_enabled=True)

# OpenAI-kompatiblen Client erstellen
client = create_client(fallback_enabled=True)
response = client.chat.completions.create(
    model="claude-sonnet-4-5-20250929",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

### Environment Variables

```bash
# Override URL (deaktiviert Fallback-Logik)
export WRAPPER_URL="http://custom:8000"

# Fallback global aktivieren
export AI_BRIDGE_FALLBACK=true
```

## TypeScript

### Installation

```bash
# Kopiere sdk/typescript/ in dein Projekt
cp sdk/typescript/ai-bridge-client.ts /path/to/your/project/lib/
```

### Usage

```typescript
import { getBridgeUrl, createClient } from './lib/ai-bridge-client';

// Nur Hetzner (Default, kein Fallback)
const url = await getBridgeUrl();

// Mit Fallback auf Local
const url = await getBridgeUrl({ fallbackEnabled: true });

// OpenAI-kompatiblen Client erstellen
const client = await createClient({ fallbackEnabled: true });
const response = await client.chat.completions.create({
    model: "claude-sonnet-4-5-20250929",
    messages: [{ role: "user", content: "Hello!" }]
});
```

## URLs

| Endpoint | URL |
|----------|-----|
| Hetzner (Production) | `http://95.217.180.242:8000` |
| Local (Development) | `http://localhost:8000` |

## Error Handling

```python
from ai_bridge_sdk import get_bridge_url, AIBridgeConnectionError

try:
    url = get_bridge_url(fallback_enabled=True)
except AIBridgeConnectionError as e:
    # Weder Hetzner noch Local erreichbar
    print(f"Error: {e}")
```

```typescript
import { getBridgeUrl, AIBridgeConnectionError } from './ai-bridge-client';

try {
    const url = await getBridgeUrl({ fallbackEnabled: true });
} catch (e) {
    if (e instanceof AIBridgeConnectionError) {
        // Weder Hetzner noch Local erreichbar
        console.error(e.message);
    }
}
```
