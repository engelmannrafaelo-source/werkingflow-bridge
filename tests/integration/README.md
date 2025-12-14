# Integration Tests - Research with /sc:research

## Übersicht

Diese Integration Tests führen **ECHTE Research-Anfragen** über den OpenAI Wrapper durch und verwenden SuperClaude's `/sc:research` Command.

⚠️ **WICHTIG**: Diese Tests sind NICHT Teil der normalen Test-Suite und müssen explizit aktiviert werden!

## Prerequisites

### 1. Wrapper muss laufen

```bash
cd /Users/lorenz/ECO/projects/eco-openai-wrapper
./start-wrappers.sh
```

### 2. Claude CLI Authentication

```bash
# Check ob authenticated
claude --print "Hello"

# Falls nicht authenticated:
claude login
```

### 3. Environment Variables

```bash
export RUN_RESEARCH_TESTS=1  # WICHTIG: Aktiviert die Tests!
export WRAPPER_URL="http://localhost:8000"
export WRAPPER_API_KEY=""  # Optional, falls API_KEY env var gesetzt
```

## Tests Ausführen

### Alle Research Tests

```bash
# Mit Poetry (empfohlen)
RUN_RESEARCH_TESTS=1 poetry run pytest tests/integration/test_research_integration.py -v -s

# Mit venv
source venv/bin/activate
RUN_RESEARCH_TESTS=1 pytest tests/integration/test_research_integration.py -v -s
```

### Nur schnelle Tests (ohne `@pytest.mark.slow`)

```bash
pytest tests/integration/test_research_integration.py -v -s -m "not slow"
```

### Einzelner Test

```bash
pytest tests/integration/test_research_integration.py::TestBasicResearch::test_research_simple_topic -v -s
```

## Test Cases

### 1. `test_wrapper_is_running` ✅
- **Dauer**: < 1s
- **Prüft**: `/health` endpoint erreichbar
- **Erwartung**: 200 OK

### 2. `test_research_simple_topic` 🔬
- **Dauer**: 2-5 Minuten
- **Topic**: "Python async/await best practices"
- **Prüft**:
  - Research wird durchgeführt
  - Response enthält sinnvolle Inhalte
  - Optional: Report in `claudedocs/` erstellt

### 3. `test_research_with_depth_specification` 🔬
- **Dauer**: 3-7 Minuten
- **Topic**: "FastAPI performance optimization"
- **Prüft**:
  - Deep research mit spezifischen Requirements
  - Strukturierte Antwort mit Performance-Daten

### 4. `test_research_handles_invalid_topic` 🧪
- **Dauer**: 1-2 Minuten
- **Topic**: Nonsensical query
- **Prüft**: Graceful error handling

### 5. `test_research_completes_within_timeout` ⏱️ (slow)
- **Dauer**: 2-5 Minuten
- **Prüft**: Research completed < 600s timeout

## Output Locations

### Test Outputs
```
tests/integration/research_outputs/
├── research_async_await_20251012_143022.txt
├── research_fastapi_20251012_143530.txt
└── ...
```

### Research Reports (SuperClaude)
```
/Users/lorenz/ECO/projects/eco-openai-wrapper/claudedocs/
├── research_report_20251012_143045.md
└── ...
```

⚠️ **Note**: SuperClaude Research Agent erstellt nicht immer Files in `claudedocs/`. Das ist normales Verhalten - die Research-Ergebnisse sind trotzdem in der API Response enthalten!

## Expected Behavior

### ✅ Erfolgreiche Research

```
=================================================================================
🔬 Starting Research: Python async/await best practices
=================================================================================
⏱️  Start: 14:30:22
⏱️  End: 14:33:45 (Duration: 203.2s)
✅ Response received: 8543 characters
💾 Saved to: tests/integration/research_outputs/research_async_await_20251012_143022.txt
📂 Checking for research report in: /Users/lorenz/ECO/projects/eco-openai-wrapper/claudedocs
📄 Found research report: research_report_20251012_143045.md
   Size: 12.3 KB
   Modified: 14:33:45
```

### ❌ Häufige Probleme

#### "Wrapper nicht erreichbar"
```bash
# Check health endpoint
curl http://localhost:8000/health

# Falls nicht erreichbar:
./start-wrappers.sh
```

#### "Authentication failed"
```bash
# Test authentication
claude --print "Hello"

# Falls Fehler:
claude login
```

#### "Test timeout"
- Research kann 2-5 Minuten dauern
- Normal für comprehensive research
- Falls > 10 Minuten: Check wrapper logs

#### "No research report found in claudedocs/"
- **Normal!** Research Agent erstellt nicht immer Files
- Response enthält trotzdem Research-Ergebnisse
- Check `tests/integration/research_outputs/` für gespeicherte Responses

## Performance Notes

- **Simple Research**: 2-3 Minuten
- **Deep Research**: 3-7 Minuten
- **Network**: Abhängig von Search API responses
- **Parallelization**: Research läuft parallel intern

## Integration mit CLAUDE.md Session Context

Diese Tests gehören zu **Phase 3: E2E Tests** im Test Plan, nicht Phase 1 Unit Tests!

Siehe: `/Users/lorenz/ECO/projects/eco-openai-wrapper/temp_debugging_lorenz_20251011_083328/TEST_PLAN.md`

## Debugging

### Verbose Output
```bash
pytest tests/integration/test_research_integration.py -v -s --log-cli-level=DEBUG
```

### Check Wrapper Logs
```bash
# Main wrapper log
tail -f /Users/lorenz/ECO/projects/eco-openai-wrapper/logs/wrapper_main_*.log

# Session logs
tail -f /Users/lorenz/ECO/projects/eco-openai-wrapper/logs/wrapper_main_session_*.log
```

### Manual Testing
```bash
# Test via curl
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4",
    "messages": [{"role": "user", "content": "/sc:research Python testing best practices"}],
    "stream": false
  }'
```

## Disable Tests

Tests sind standardmäßig disabled (via `skipif`).

Um zu disablen:
```bash
unset RUN_RESEARCH_TESTS
# ODER
export RUN_RESEARCH_TESTS=0
```

## Contributing

Bei neuen Research Test Cases:
1. Add to appropriate Test Class
2. Use descriptive test names
3. Add `@pytest.mark.slow` für Tests > 2 Minuten
4. Document expected duration in docstring
5. Save outputs to `research_outputs/`
