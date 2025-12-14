# File Discovery Feature

**File Discovery** ermöglicht automatisches Sammeln von Files, die während Claude Code Execution erstellt wurden, und gibt sie als base64-encoded Content in der Response zurück.

## 🎯 Use Cases

- **Code-Generierung**: Erstelle Files und bekomme direkt den Content zurück
- **Report-Generierung**: Markdown/JSON Reports ohne Server-Zugriff
- **Multi-File-Workflows**: Mehrere Files in einem Request erstellen und abrufen

## 🔧 Aktivierung

File Discovery ist **opt-in** und muss explizit aktiviert werden.

### **Voraussetzungen**

1. **`enable_tools: true`** im Request Body (PFLICHT)
   - Ohne Tools kann Claude keine Files erstellen
   - File Discovery wird ignoriert wenn `enable_tools=false`

2. **Model-Wahl** (optional)
   - Beliebiges Claude Model via `model` Parameter
   - Beispiele: `claude-sonnet-4-5-20250929`, `claude-opus-4-20250514`
   - Siehe `/v1/models` Endpoint für verfügbare Models

3. **Multi-Turn Support** (empfohlen für komplexe Tasks)
   - Header: `X-Claude-Max-Turns` (optional, Standard: `1` ohne Tools, `10` mit Tools)
   - Wert: Integer als String (z.B. `"5"`, `"20"`)
   - Zweck: Erlaubt Claude mehrere Tool-Aufrufe (z.B. Read → Analyze → Write)

### **Methode 1: Header (Opt-In)**

```python
import httpx

response = await client.post(
    "http://localhost:8000/v1/chat/completions",
    json={
        "model": "<your-model-choice>",  # z.B. "claude-sonnet-4-5-20250929"
        "messages": [
            {"role": "user", "content": "Erstelle main.py und models.py"}
        ],
        "enable_tools": True  # REQUIRED für File Discovery
    },
    headers={
        "X-Claude-File-Discovery": "enabled",  # Aktiviert File Discovery
        "X-Claude-Max-Turns": "10"  # Optional: Multi-Turn für komplexe Tasks
    }
)
```

**Header-Werte:**
- **`X-Claude-File-Discovery`** (PFLICHT für Discovery):
  - `"enabled"` oder `"true"` oder `"1"` → File Discovery aktiv
  - Beliebiger anderer Wert oder fehlend → File Discovery inaktiv

- **`X-Claude-Max-Turns`** (Optional):
  - Format: Integer als String (z.B. `"5"`, `"20"`)
  - Zweck: Anzahl erlaubter Tool-Aufrufe
  - Standard: `1` (ohne Tools) oder `10` (mit Tools)
  - Beispiel: `/sc:research` benötigt oft `"15"` bis `"30"` Turns

### **Methode 2: /sc:research (Automatisch)**

File Discovery ist **automatisch aktiv** bei `/sc:research` Requests (ohne Header):

```python
response = await client.post(
    "http://localhost:8000/v1/chat/completions",
    json={
        "model": "<your-model-choice>",
        "messages": [
            {"role": "user", "content": "/sc:research --depth quick\n\nPython async best practices"}
        ],
        "enable_tools": True  # REQUIRED
    },
    headers={
        "X-Claude-Max-Turns": "20"  # Empfohlen: Research benötigt mehr Turns
    }
)
```

**Kein `X-Claude-File-Discovery` Header nötig** - wird automatisch erkannt wenn Prompt `/sc:research` enthält.

## 📦 Response Format

Bei aktivierter File Discovery enthält die Response ein `x_claude_metadata` Feld:

```json
{
    "id": "chatcmpl-abc123",
    "choices": [{
        "message": {
            "content": "Ich habe main.py und models.py erstellt..."
        }
    }],
    "x_claude_metadata": {
        "files_created": [
            {
                "path": "/absolute/path/to/main.py",
                "relative_path": "claudedocs/main.py",
                "size_bytes": 1024,
                "mime_type": "text/x-python",
                "created_at": "2025-11-06T14:30:00",
                "checksum": "sha256:a3f7e9b2...",
                "content_base64": "aW1wb3J0IGFzeW5jaW8..."
            }
        ],
        "session_tracking": {
            "cli_session_id": "a3f7e9b2-...",
            "research_dir": "/path/to/session"
        },
        "discovery_status": "success",
        "discovery_method": "sdk_parsing"
    }
}
```

### **Felder**

| **Feld** | **Typ** | **Beschreibung** |
|----------|---------|------------------|
| `path` | string | Absoluter Pfad zur Datei |
| `relative_path` | string | Relativer Pfad zum Wrapper-Root |
| `size_bytes` | integer | Dateigröße in Bytes |
| `mime_type` | string | MIME-Type (z.B. `text/x-python`) |
| `created_at` | string | ISO 8601 Timestamp |
| `checksum` | string | SHA256 Checksum (`sha256:hexdigest`) |
| `content_base64` | string | Base64-encoded Dateiinhalt |

## 🔍 Discovery-Strategien

File Discovery nutzt zwei Strategien in Reihenfolge:

### **Strategy 1: SDK Message Parsing** (PRIMARY)

- Parst SDK Messages nach `Write` Tool-Calls
- Extrahiert `file_path` aus Tool-Input
- Validiert File-Existenz und Timestamp
- **Vorteil**: Präzise, nur tatsächlich geschriebene Files

### **Strategy 2: Directory Scan** (FALLBACK)

- Scannt `claudedocs/` Directory nach neuen Files
- Filtert nach Patterns: `*.md`, `*.json`
- Prüft `mtime > session_start`
- **Vorteil**: Findet Files auch wenn SDK-Parsing fehlschlägt

## 📊 Discovery Status

| **Status** | **Bedeutung** |
|------------|---------------|
| `success` | Files gefunden und erfolgreich gelesen |
| `no_files_found` | Discovery lief, aber keine Files gefunden |

### **Discovery Method**

| **Method** | **Bedeutung** |
|------------|---------------|
| `sdk_parsing` | Files wurden via SDK Write-Tool-Calls gefunden (präzise) |
| `directory_scan` | Fallback: Files via Directory-Scan gefunden (weniger präzise) |

**Empfehlung:** Bei `directory_scan` Logs prüfen warum SDK-Parsing fehlschlug.

**Wichtig**: Wenn `discovery_status=no_files_found`, enthält die Response `discovery_details` mit Diagnose-Informationen.

## 🚫 Keine Files gefunden

Wenn Discovery läuft aber keine Files findet:

```json
{
    "x_claude_metadata": {
        "files_created": [],
        "discovery_status": "no_files_found",
        "discovery_details": {
            "sdk_parsing_attempted": true,
            "sdk_parsing_failures": 0,
            "directory_scan_attempted": true,
            "directory_scan_failures": 0,
            "possible_causes": [
                "Request created no files (text-only response)",
                "Files were created but discovery logic failed",
                "Files were created outside expected directories"
            ],
            "suggested_actions": [
                "Check claudedocs/ directory manually",
                "Review wrapper logs for parsing errors"
            ]
        }
    }
}
```

## ⚠️ Edge Cases

### **Multi-Tool Request ohne Files**

```python
# Request mit Tools, aber Claude erstellt keine Files
{
    "messages": [
        {"role": "user", "content": "Analysiere main.py und gib Feedback"}
    ],
    "enable_tools": True
}
headers = {"X-Claude-File-Discovery": "enabled"}
```

**Result**:
- Discovery läuft
- Keine Files gefunden
- Response enthält `x_claude_metadata` mit `discovery_status: "no_files_found"` und `discovery_details`

### **File Discovery ohne enable_tools**

```python
{
    "messages": [...],
    "enable_tools": False  # Tools disabled!
}
headers = {"X-Claude-File-Discovery": "enabled"}
```

**Result**:
- Discovery wird **ignoriert** (keine Tools = keine File-Erstellung möglich)
- Keine `x_claude_metadata` in Response

## 🎯 Best Practices

### **1. Nur aktivieren wenn nötig**

```python
# ✅ GOOD: Nur bei File-Creation-Tasks
if task_creates_files:
    headers["X-Claude-File-Discovery"] = "enabled"

# ❌ BAD: Immer aktiv (Performance-Impact)
headers["X-Claude-File-Discovery"] = "enabled"  # für ALLE Requests
```

### **2. Performance-Überlegungen**

**File Discovery Overhead:**
- SDK Message Parsing: ~10-50ms (abhängig von Message-Anzahl)
- Directory Scan (Fallback): ~50-200ms (abhängig von File-Anzahl)
- Base64 Encoding: ~5ms pro MB Datei

**Beispiel:** 3 Markdown-Files (je 100KB) → ~15ms Overhead

**Optimierung:**
```python
# Nur aktivieren wenn Files erwartet werden
if "create file" in user_prompt or "/sc:research" in user_prompt:
    headers["X-Claude-File-Discovery"] = "enabled"
```

### **3. Checksum validieren**

```python
import hashlib
import base64

content_bytes = base64.b64decode(file_info["content_base64"])
actual_checksum = f"sha256:{hashlib.sha256(content_bytes).hexdigest()}"

if actual_checksum != file_info["checksum"]:
    raise ValueError("Checksum mismatch - content corrupted!")
```

### **4. Error-Handling**

```python
data = response.json()

if "x_claude_metadata" not in data:
    # Keine Files erstellt (normal bei Q&A-Tasks)
    pass
elif data["x_claude_metadata"]["discovery_status"] != "success":
    # Discovery fehlgeschlagen
    details = data["x_claude_metadata"]["discovery_details"]
    logger.warning(f"File discovery failed: {details}")
else:
    # Files erfolgreich discovered
    files = data["x_claude_metadata"]["files_created"]
```

## 📡 Streaming Support

Bei `stream: true` wird `x_claude_metadata` als **separates SSE Event** am Ende des Streams gesendet:

```python
import httpx
import json

async with httpx.stream(
    "POST",
    "http://localhost:8000/v1/chat/completions",
    json={
        "model": "<your-model-choice>",
        "messages": [{"role": "user", "content": "Create main.py"}],
        "enable_tools": True,
        "stream": True
    },
    headers={"X-Claude-File-Discovery": "enabled"}
) as response:
    metadata_received = False

    async for line in response.aiter_lines():
        if line.startswith("data: "):
            data = line[6:]
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            # Process content chunk
            print(chunk["choices"][0]["delta"].get("content", ""), end="")

        elif line.startswith("event: x_claude_metadata"):
            # Next line contains metadata
            metadata_received = True

        elif metadata_received and line.startswith("data: "):
            metadata = json.loads(line[6:])
            print(f"\n\nFiles: {len(metadata['files_created'])}")
            for file in metadata["files_created"]:
                print(f"  - {file['relative_path']}")
            metadata_received = False
```

**SSE Event Format:**
```
event: x_claude_metadata
data: {"files_created":[...],"session_tracking":{...},"discovery_status":"success"}
```

## 📁 Session Directory Struktur

Jede Request mit File Discovery erstellt ein Session-Directory:

```
instances/<instance_name>/<timestamp>_<cli_session_id>/
├── metadata.json          # Session-Metadata (SDK Options, Status, Timestamps)
├── prompt.txt             # Exakter Prompt der an Claude ging
├── claudedocs/            # Output-Files (Reports, Code, etc.)
│   ├── output.md
│   └── analysis.json
├── progress.jsonl         # Tool-Usage-Tracking (optional)
├── messages.jsonl         # SDK-Messages Debug (optional)
└── final_response.json    # Komplette Antwort (optional)
```

**Beispiel-Pfad:**
```
instances/eco-wrapper-1/2025-11-07-1445_a3f7e9b2.../claudedocs/report.md
```

**Session-Info via API abrufen:**
```python
# Session-ID aus Response-Header
session_id = response.headers.get("X-Claude-Session-ID")

# Session-Info abrufen
session_info = requests.get(f"http://localhost:8000/v1/cli-sessions/{session_id}").json()
research_dir = session_info.get("research_dir")

# Manueller Zugriff zu Files (Fallback wenn Discovery fehlschlägt)
import os
if research_dir:
    files = os.listdir(f"{research_dir}/claudedocs")
```

## 🔬 Testing

Siehe [examples/file_discovery_example.py](../examples/file_discovery_example.py) für vollständige Beispiele.

```bash
# Test mit File Discovery
python examples/file_discovery_example.py
```

## 📝 Logging

File Discovery loggt ausführlich:

```
✅ File Discovery enabled (method: header, header_value: enabled)
🔍 Starting file discovery (enabled via header or /sc:research)
✅ SDK message parsing discovered 2 files
📦 Yielded file metadata: 2 files
```

Log-Level für Debugging: `LOG_LEVEL=DEBUG`

## 🐛 Troubleshooting

### **Problem: Keine x_claude_metadata in Response**

**Ursachen:**
1. Header nicht gesetzt oder falscher Wert
2. `enable_tools=False` (Tools disabled)
3. Claude hat keine Files erstellt
4. Discovery lief aber fand keine Files

**Lösung:**
```python
# 1. Header prüfen
assert headers["X-Claude-File-Discovery"] == "enabled"

# 2. Tools aktiviert?
assert request["enable_tools"] == True

# 3. Logs prüfen
grep "File Discovery" logs/app.log
```

### **Problem: Files erstellt aber nicht gefunden**

**Ursachen:**
1. Files außerhalb Session-Directory erstellt
2. SDK-Parsing fehlgeschlagen
3. Directory-Scan fehlgeschlagen

**Lösung:**
```bash
# Check Session-Directory manuell
ls -lah instances/eco-wrapper-1/2025-11-06-1430_*/

# Check Logs für Fehler
grep "Failed to" logs/app.log | grep -i file
```

## ⚠️ Limitations

### **1. File Size Limits**
- **Keine explizite Größen-Beschränkung** im Wrapper
- **Praktisches Limit:** ~10MB pro File (Base64 Overhead ~33%)
- **Empfehlung:** Große Binaries nicht via Discovery übertragen, stattdessen via Session-Directory-Zugriff

### **2. File Types**
- **Unterstützt:** Alle Text-Files (`.md`, `.py`, `.json`, `.txt`, `.yaml`, etc.)
- **Eingeschränkt:** Binaries (Base64-Overhead, schwer zu debuggen)
- **Nicht unterstützt:** Symlinks, Device-Files, Directories

### **3. Directory Scope**
- **Primary:** `claudedocs/` im Session-Directory
- **Fallback Patterns:** Nur `*.md` und `*.json` werden gescannt (andere Typen nur via SDK-Parsing gefunden)

### **4. Timing**
- **Files müssen existieren** wenn SDK Query abgeschlossen ist
- **Timestamp-Check:** Nur Files mit `mtime > session_start` werden gefunden
- **Delete/Edit-Operations:** Werden NICHT getracked (nur neu erstellte Files)

## ❓ FAQ

**Q: Funktioniert File Discovery mit allen Models?**
A: Ja, File Discovery ist model-agnostic. Es analysiert nur SDK Tool-Calls und Directory-Contents, unabhängig vom verwendeten Model.

**Q: Kann ich File Discovery ohne Tools nutzen?**
A: Nein. Ohne `enable_tools=true` kann Claude keine Files erstellen → Discovery wird ignoriert.

**Q: Was wenn Claude Files LÖSCHT oder EDITIERT statt erstellt?**
A: Delete/Edit-Operations werden NICHT getracked. Discovery findet nur neu erstellte Files (via timestamp `mtime > session_start`).

**Q: Funktioniert Discovery mit `continue_session`?**
A: Ja, aber NUR Files aus der aktuellen Session werden gefunden (timestamp-basierter Filter).

**Q: Kann ich Discovery für spezifische File-Types einschränken?**
A: Nein. Discovery findet ALLE Files die Claude via Write-Tool erstellt hat. Filtern Sie client-seitig via `mime_type` Feld.

**Q: Wie kann ich die Session-Directory finden wenn Discovery fehlschlägt?**
A: Response-Header `X-Claude-Session-ID` auslesen → GET `/v1/cli-sessions/{session_id}` → `research_dir` Feld enthält absoluten Pfad zum Session-Directory.

**Q: Welche Models unterstützen Tool-Calls und damit File Discovery?**
A: Alle aktuellen Claude Models (Sonnet, Opus, Haiku). Liste via GET `/v1/models` abrufen.

## 🔗 Verwandte Dokumentation

- [Research Guide](./RESEARCH_GUIDE.md) - `/sc:research` Workflow
- [Tool Configuration](./TOOLS.md) - `enable_tools` Parameter
- [Session Management](./SESSIONS.md) - Session-Directory-Struktur

---

**Version**: 1.1
**Last Updated**: 2025-11-07
