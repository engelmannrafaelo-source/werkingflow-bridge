# Research Permission Fix - Zusammenfassung aller Änderungen

**Datum:** 2025-10-25
**Problem:** `/sc:research` Command konnte keine Research-Reports schreiben - nur 401-427 Wörter statt ~10,000 Wörter
**Lösung:** Permission und CWD Konfiguration für Claude Agent SDK

---

## ✅ Änderungen die zum Erfolg geführt haben

### 1. **Permission Mode aktivieren** (`claude_cli.py`)

**Problem:** Agent hatte keine Write-Permission für Files außerhalb seiner Sandbox.

**Lösung:**
```python
# claude_cli.py - Lines 244-248
# Set permission mode if specified via environment variable
permission_mode = os.getenv("CLAUDE_PERMISSION_MODE")
if permission_mode:
    options.permission_mode = permission_mode
    logger.info(f"🔓 Permission mode set to: {permission_mode}")
```

**Start-Wrapper Konfiguration:**
```bash
# start-wrappers.sh - Line 160
CLAUDE_PERMISSION_MODE="acceptEdits" DISABLE_MCPS="false" PORT=8010 ...
```

**Effekt:** `permission_mode='acceptEdits'` erlaubt dem Agent automatisch Files zu schreiben ohne User-Confirmation.

---

### 2. **Working Directory (cwd) auf Wrapper Root setzen** (`claude_cli.py`)

**Problem:** Agent versuchte nach `/eco-openai-wrapper/claudedocs/` zu schreiben, aber `cwd` war auf `/instances/eco-backend/` gesetzt (außerhalb der Sandbox).

**Lösung:**
```python
# claude_cli.py - Lines 230-237
# Special handling for /sc:research - needs access to claudedocs/
# /sc:research writes to wrapper-level claudedocs/, need to go up 2 levels
# instances/eco-backend → instances → eco-openai-wrapper
research_cwd = self.cwd
if '/sc:research' in prompt:
    research_cwd = str(Path(self.cwd).parent.parent)
    logger.info(f"🔬 Research mode: Using wrapper root for claudedocs/ access")
    logger.info(f"   cwd: {research_cwd}")
```

**Effekt:**
- Normal: `cwd = /Users/lorenz/ECO/projects/eco-openai-wrapper/instances/eco-backend`
- Research: `cwd = /Users/lorenz/ECO/projects/eco-openai-wrapper/` (2 Ebenen höher)
- Jetzt kann Agent in `claudedocs/` schreiben (innerhalb der neuen Sandbox)

---

### 3. **HTTP Client Timeout erhöhen** (`simple_research_client.py`)

**Problem:** Research dauert ~30 Minuten (Academic Mode), aber Client Timeout war nur 20 Minuten (1200s).

**Lösung:**
```python
# simple_research_client.py - Line 79
self.client = OpenAI(
    base_url=self.base_url,
    api_key=os.getenv("WRAPPER_API_KEY", "dummy-key"),
    timeout=2400.0  # 40 minutes (matches wrapper timeout-keep-alive)
)

# simple_research_client.py - Line 152
response = self.client.chat.completions.create(
    ...
    timeout=2400  # 40 minutes (matches wrapper timeout-keep-alive)
)
```

**Effekt:** Client wartet jetzt 40 Minuten statt 20 - genug Zeit für vollständige Research.

---

### 4. **Model auf Sonnet 4.5 konfigurierbar machen** (`simple_research_client.py` + `.env`)

**Problem:** Model war hardcoded auf `claude-sonnet-4-20250514` (Sonnet 4, Mai 2025).

**Lösung:**

**Backend .env:**
```bash
# /Users/lorenz/ECO/projects/eco-backend/.env - Lines 13-15
# Research Model Configuration
# Use Sonnet 4.5 for best research quality
RESEARCH_MODEL=claude-sonnet-4-5-20250929
```

**Client Code:**
```python
# simple_research_client.py - Lines 131-137
# Configure model from env
research_model = os.getenv("RESEARCH_MODEL", "claude-sonnet-4-5-20250929")  # Default: Sonnet 4.5

if self.verbose:
    self.log(f"🔬 Starting {depth} research...")
    self.log(f"   Server: {self.base_url}")
    self.log(f"   Model: {research_model}")
    ...

# simple_research_client.py - Line 145
response = self.client.chat.completions.create(
    model=research_model,  # statt hardcoded "claude-sonnet-4-20250514"
    ...
)
```

**Effekt:** Sonnet 4.5 (neuestes Model, bessere Research-Qualität) wird automatisch genutzt.

---

## 📋 Ergebnis

**Vorher:**
- ❌ 401-427 Wörter (nur Summary)
- ❌ Permission Errors bei Write
- ❌ Timeout nach 20 Minuten
- ❌ Sonnet 4 (älteres Model)

**Nachher:**
- ✅ **9,317 Wörter** (vollständiger Research Report)
- ✅ **68 KB File** in `claudedocs/`
- ✅ Keine Permission Errors
- ✅ 13.2 Minuten Runtime (kein Timeout)
- ✅ Sonnet 4.5 (neuestes Model)

**File Location:**
```
/Users/lorenz/ECO/projects/eco-openai-wrapper/claudedocs/GW_ST_POELTEN_PHASE2_WEB_RESEARCH_20251025.md
```

---

## ⚠️ Änderungen mit unsicherer Wirkung

Diese Änderungen wurden durchgeführt, trugen aber möglicherweise NICHT zur Lösung bei:

### 1. **DISABLE_MCPS auf "false" gesetzt** (`start-wrappers.sh`)

**Änderung:**
```bash
# start-wrappers.sh - Line 160 (vorher: DISABLE_MCPS="true")
DISABLE_MCPS="false"
```

**Unsicherheit:**
- `DISABLE_MCPS` blockiert MCP Tools (mcp__*), nicht native Claude Code Tools (Write, Read, etc.)
- Native Tools sollten auch mit `DISABLE_MCPS="true"` funktionieren
- Die Permission kam von `CLAUDE_PERMISSION_MODE="acceptEdits"`, nicht von DISABLE_MCPS

**Vermutung:** Diese Änderung war wahrscheinlich NICHT notwendig für die Lösung.

---

### 2. **Research Conductor - Dual-Key Fallback** (`research_conductor.py`)

**Änderung:**
```python
# research_conductor.py - Lines 73-82
# Try 'text' (current format) then 'content' (legacy fallback)
research_text = research_result.get('text') or research_result.get('content')

# FAIL LOUD if both keys are missing or empty
if not research_text:
    raise KeyError(
        f"Research result missing both 'text' and 'content' keys!\n"
        f"Available keys: {list(research_result.keys())}\n"
        f"This indicates the wrapper returned an unexpected format."
    )
```

**Unsicherheit:**
- SimpleResearchClient gab immer `'text'` zurück
- Der ursprüngliche Code erwartete `'content'` → KeyError
- Diese Änderung fixt einen Response-Format-Mismatch
- ABER: Das eigentliche Problem war dass KEINE Response kam (Permission Error), nicht falscher Key

**Vermutung:** Diese Änderung war notwendig um den KeyError zu vermeiden, trug aber nicht zur 9,317-Wort-Lösung bei (da Response vorher leer war).

---

## 🔍 Root Cause Analysis

**Das eigentliche Problem:**
1. Agent versuchte Write nach `/eco-openai-wrapper/claudedocs/`
2. Aber `cwd` war `/eco-openai-wrapper/instances/eco-backend/`
3. Permission Mode `manual` (default) → User müsste Permission geben
4. Keine User-Interaktion möglich → Write failed
5. Agent gab nur Text-Summary zurück (401 Wörter)

**Die Lösung:**
1. `cwd` auf `/eco-openai-wrapper/` erweitern (`.parent.parent`)
2. `permission_mode='acceptEdits'` aktivieren
3. Timeout erhöhen für volle Research-Dauer

**Key Insight:** Claude Agent SDK's `permission_mode` Parameter war der Schlüssel, nicht MCP-Konfiguration.

---

## 📝 Testing

**Test Command:**
```bash
cd /Users/lorenz/ECO/projects/eco-backend
python run_pipeline.py --phases 2 --project GewiStPoelten
```

**Success Indicators:**
- File wird geschrieben: `/eco-openai-wrapper/claudedocs/GW_ST_POELTEN_*.md`
- Word Count: >9,000 Wörter
- Wrapper Logs zeigen: `🔬 Research mode: Using wrapper root for claudedocs/ access`
- Wrapper Logs zeigen: `🔓 Permission mode set to: acceptEdits`

---

## 🚀 Future Work

**Potential Improvements:**
1. Pipeline sollte Research File im richtigen Pfad suchen (aktuell sucht sie unter falschem Pfad)
2. SuperClaude `/sc:research` sollte relativen Pfad nutzen statt absolutem
3. Dokumentation für `CLAUDE_PERMISSION_MODE` ENV Variable

---

**Ende der Zusammenfassung**
