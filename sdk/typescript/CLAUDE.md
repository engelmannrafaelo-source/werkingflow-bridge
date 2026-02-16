# AI-Bridge TypeScript SDK

**Stack**: TypeScript, Node.js
**Entry**: `ai-bridge-client.ts`

## Architektur

OpenAI-kompatibler Client für AI-Bridge mit Smart URL Resolution und automatischem Fallback.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│ Application │────→│ AI-Bridge SDK│────→│ AI-Bridge   │
└─────────────┘     └──────────────┘     └─────────────┘
                           │                    │
                           ▼                    ▼
                    ┌─────────────┐     ┌─────────────┐
                    │ Hetzner     │ or  │ localhost   │
                    │ :8000       │     │ :8000       │
                    └─────────────┘     └─────────────┘
```

## Quick Start

```typescript
import { getBridgeUrl, createClient } from './ai-bridge-client';

// Einfach (nur Hetzner, Fehler wenn nicht erreichbar)
const url = await getBridgeUrl();

// Mit Fallback zu localhost
const url = await getBridgeUrl({ fallbackEnabled: true });

// OpenAI-kompatiblen Client erstellen
const client = await createClient({ fallbackEnabled: true });
```

## Kritische Dateien

| Datei | Zweck |
|-------|-------|
| `ai-bridge-client.ts` | SDK Implementation (232 Zeilen) |
| `package.json` | Dependencies (OpenAI SDK) |

## API

### URL Resolution

| Funktion | Beschreibung |
|----------|--------------|
| `getBridgeUrl(options?)` | Async URL Resolution mit Fallback-Logic |
| `getBridgeUrlSync()` | Synchroner Zugriff auf gecachte URL |
| `getBridgeUrlOrDefault()` | Fallback zu HETZNER_URL wenn nicht initialisiert |
| `initializeBridgeUrl(options?)` | URL cachen für späteren sync Zugriff |
| `isInitialized()` | Prüfen ob SDK initialisiert |

### Client & Health

| Funktion | Beschreibung |
|----------|--------------|
| `createClient(options?)` | OpenAI-kompatiblen Client erstellen |
| `healthCheck(url, timeout)` | Prüfen ob Backend erreichbar |

### Konstanten

| Konstante | Wert |
|-----------|------|
| `HETZNER_URL` | `http://49.12.72.66:8000` |
| `LOCAL_URL` | `http://localhost:8000` |
| `HETZNER_TIMEOUT` | 3000ms |
| `LOCAL_TIMEOUT` | 1000ms |

## Environment Variables

| Variable | Beschreibung |
|----------|--------------|
| `AI_BRIDGE_API_KEY` | API Key für Authentifizierung (PFLICHT) |
| `WRAPPER_URL` | Überschreibt URL-Auflösung (deaktiviert Fallback) |
| `AI_BRIDGE_FALLBACK` | Fallback global aktivieren (`true`, `1`, `yes`) |

## URL Resolution Priority

1. `WRAPPER_URL` env var (expliziter Override, kein Fallback)
2. Hetzner (wenn erreichbar)
3. localhost:8000 (wenn Fallback aktiviert UND Hetzner nicht erreichbar)
4. `AIBridgeConnectionError` (wenn nichts erreichbar)

## Defensive Programming

- **Fail Loud**: `AIBridgeConnectionError` wenn kein Backend erreichbar
- **Timeout-basierte Health Checks**: 3s Hetzner, 1s Local
- **Keine Silent Failures**: Fehlende API Keys werfen sofort Error

## Abhängigkeiten

- **Verwendet**: OpenAI SDK (`openai`)
- **Wird verwendet von**: `werkingflow/platform`, `apps/*`, `tecc-safety-expert`
- **Verbindet zu**: AI-Bridge Backend (Hetzner oder lokal)

---

*Aktualisiert: 2026-01-30*
