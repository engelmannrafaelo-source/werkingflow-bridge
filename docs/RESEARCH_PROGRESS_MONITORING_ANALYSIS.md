# Research Progress Monitoring - Analysis & Lösungsvorschläge

**Datum**: 2024-10-24
**Problem**: Während `/sc:research` läuft gibt es keine Zwischenergebnisse - "black box" Gefühl
**Status**: 📊 ANALYSE PHASE

---

## 🎯 Problem Statement

**User Experience Issue:**
> "während der recherche gibt es kein 'zwischenergebnis' und man sieht nicht wirklich ob der prozess läuft"

**Kontext:**
- `/sc:research` kann 5-30 Minuten dauern
- User sieht nur: Request → ... → Final Response
- Keine Visibility über:
  - Welche Phase läuft gerade?
  - Wie viele Searches wurden gemacht?
  - Ist das Ding überhaupt noch aktiv?
  - Wie viel Fortschritt?

---

## 🔍 Current State Analysis

### SDK Streaming Behavior

**claude_code_sdk.query()** returned Messages:
```python
async for message in query(prompt=prompt, options=options):
    # message types:
    # - SystemMessage (init, subtype='init')
    # - AssistantMessage (text, tool_use)
    # - UserMessage (tool_result)
    # - ResultMessage (final, subtype='success')
```

**Message Types (aus SDK Docs):**
1. **SystemMessage** - Init, MCP connections, session_id
2. **AssistantMessage** - Claude's responses:
   - `TextBlock` - Text output
   - `ToolUseBlock` - Tool calls (WebSearch, TodoWrite, Context7, etc.)
3. **UserMessage** - Tool results:
   - `ToolResultBlock` - Tool execution results
4. **ResultMessage** - Final completion marker

**Crucially: Jede Message wird SOFORT gestreamt!**

### TodoWrite Integration

**From `/sc:research` command definition:**
```markdown
### 3. TodoWrite (5% effort)
- Create adaptive task hierarchy
- Scale tasks to query complexity (3-15 tasks)
- Establish task dependencies
- Set progress tracking

### 5. Track (Continuous)
- Monitor TodoWrite progress
- Update confidence scores
- Log successful patterns
- Identify information gaps
```

**Das bedeutet:**
- ✅ Claude erstellt Todos für Research Phases
- ✅ Claude updated Todos während Research
- ✅ TodoWrite Messages werden gestreamt
- ❌ Wrapper zeigt die Todos NICHT an!

---

## 🚨 Root Cause: Progress Messages werden gefiltert!

### Wrapper Implementation (claude_cli.py:343)

```python
async for message in query(prompt=prompt, options=options):
    # Line 343: yield message
    yield message
```

**Problem**: Wrapper yielded ALLE Messages, aber wo gehen sie hin?

### OpenAI Compatibility Layer (main.py)

Lass mich checken was main.py mit den Messages macht...

**Hypothese**: OpenAI Chat Completions API Format hat KEIN Konzept von:
- TodoWrite updates
- Tool execution progress
- Research phase indicators

**OpenAI Format:**
```json
{
  "choices": [{
    "delta": {"content": "text chunk"},  // ← NUR TEXT!
    "finish_reason": null
  }]
}
```

**Claude SDK Messages:**
```python
AssistantMessage(content=[
  ToolUseBlock(name='TodoWrite', input={'todos': [...]})  // ← LOST!
])
```

**Ergebnis**: Alle non-text Messages (TodoWrite, ToolUse, etc.) werden NICHT zum Client gestreamt!

---

## 💡 Lösungsansätze

### Option 1: Server-Sent Events (SSE) - Text Annotations ⭐ EMPFOHLEN

**Konzept**: Text-basierte Progress Updates via SSE comments

**Implementation:**
```python
# In main.py streaming response
async for message in claude_cli_query(...):
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                # Progress annotation als SSE comment
                if block.name == 'TodoWrite':
                    yield f": [PROGRESS] TodoWrite: {len(block.input['todos'])} tasks\n\n"
                elif block.name == 'WebSearch':
                    query = block.input.get('query', 'searching...')
                    yield f": [PROGRESS] WebSearch: {query[:50]}...\n\n"
                elif block.name.startswith('mcp__context7'):
                    yield f": [PROGRESS] Context7: Loading documentation\n\n"

            elif isinstance(block, TextBlock):
                # Normal text response
                yield format_openai_chunk(block.text)

    elif isinstance(message, UserMessage):
        for block in message.content:
            if isinstance(block, ToolResultBlock) and not block.is_error:
                yield f": [PROGRESS] Tool completed successfully\n\n"
```

**Client sieht:**
```
data: {"choices":[{"delta":{"content":"I'll research..."}}]}

: [PROGRESS] TodoWrite: 5 tasks
: [PROGRESS] WebSearch: Python 3.13 release date...
: [PROGRESS] WebSearch: Python async best practices...
: [PROGRESS] Context7: Loading documentation
: [PROGRESS] Tool completed successfully

data: {"choices":[{"delta":{"content":"## Results\n\n"}}]}
```

**Vorteile:**
- ✅ OpenAI-kompatibel (SSE comments werden ignoriert von Standard-Clients)
- ✅ Backward compatible
- ✅ Einfach zu parsen für Custom Clients
- ✅ Keine API Breaking Changes

**Nachteile:**
- ⚠️ Erfordert Custom Client für UI rendering
- ⚠️ Standard OpenAI clients zeigen nichts

---

### Option 2: Custom Headers - Progress Metadata

**Konzept**: HTTP Headers für Progress Updates

```python
# In streaming response
response.headers['X-Research-Phase'] = 'searching'
response.headers['X-Todo-Progress'] = '3/5'
response.headers['X-Current-Tool'] = 'WebSearch'
```

**Vorteile:**
- ✅ Non-intrusive
- ✅ Easy to implement

**Nachteile:**
- ❌ Headers können nur EINMAL gesetzt werden (nicht während stream)
- ❌ Nicht für progressive updates geeignet

---

### Option 3: Separate WebSocket Channel

**Konzept**: Paralleler WebSocket für Progress Updates

```python
# Client öffnet 2 Connections:
# 1. POST /v1/chat/completions (main response)
# 2. WS /v1/progress/{session_id} (progress updates)

# Progress WebSocket sendet:
{
  "type": "todo_update",
  "todos": [
    {"content": "Search web", "status": "completed"},
    {"content": "Analyze results", "status": "in_progress"}
  ]
}
```

**Vorteile:**
- ✅ Clean separation of concerns
- ✅ Rich progress data structure
- ✅ Real-time bidirectional

**Nachteile:**
- ❌ Erfordert WebSocket support
- ❌ Komplexere Client-Implementierung
- ❌ Session Management nötig

---

### Option 4: Extended OpenAI Format - Custom Events

**Konzept**: Erweitere OpenAI SSE Format mit custom event types

**Standard OpenAI:**
```
data: {"choices":[{"delta":{"content":"text"}}]}
```

**Extended Format:**
```
event: progress
data: {"type":"tool_use","tool":"WebSearch","query":"Python 3.13"}

event: progress
data: {"type":"todo_update","completed":3,"total":5}

data: {"choices":[{"delta":{"content":"text"}}]}
```

**Client Parsing:**
```typescript
const eventSource = new EventSource('/v1/chat/completions');

eventSource.addEventListener('progress', (event) => {
  const data = JSON.parse(event.data);
  if (data.type === 'tool_use') {
    showProgress(`${data.tool}: ${data.query}`);
  }
});

eventSource.addEventListener('message', (event) => {
  // Standard OpenAI response
  const data = JSON.parse(event.data);
  renderText(data.choices[0].delta.content);
});
```

**Vorteile:**
- ✅ SSE native event types
- ✅ Structured data
- ✅ Backward compatible (clients ignore unknown events)

**Nachteile:**
- ⚠️ Nicht "Standard OpenAI"
- ⚠️ Braucht custom client

---

## 🎯 Empfohlene Lösung: Hybrid Approach

**Kombination aus Option 1 + Option 4:**

### Phase 1: SSE Comments (Quick Win)
**Aufwand**: ~2 Stunden
**Breaking**: Nein

```python
# main.py - Minimal changes
async for message in claude_cli_query(...):
    # Progress als SSE comments
    if is_progress_message(message):
        yield format_progress_comment(message)

    # Normal text response
    if is_text_message(message):
        yield format_openai_chunk(message)
```

**User sieht im Terminal:**
```bash
curl -N http://localhost:8010/v1/chat/completions \
  -d '{"messages":[{"role":"user","content":"/sc:research Python 3.13"}]}'

: [PROGRESS] Starting research...
: [PROGRESS] TodoWrite: 5 research tasks created
: [PROGRESS] WebSearch: Python 3.13 release date
: [PROGRESS] WebSearch: Python 3.13 new features
: [PROGRESS] Context7: Loading Python documentation
: [PROGRESS] TodoWrite: 3/5 tasks completed
data: {"choices":[{"delta":{"content":"## Python 3.13..."}}]}
```

### Phase 2: Custom Events (Better UX)
**Aufwand**: ~1 Tag
**Breaking**: Nein (optional enhancement)

```python
# Zusätzlich zu comments: Structured events
event: research_progress
data: {"phase":"searching","tool":"WebSearch","query":"..."}

event: todo_update
data: {"todos":[{"content":"...","status":"completed"}]}
```

**Custom UI kann dann:**
- Progress bar anzeigen (3/5 tasks)
- Tool usage visualisieren
- Research phase indicators

---

## 📊 Implementation Plan

### Quick Win (SSE Comments) - 2h

**Files to Change:**
1. `main.py` - Add progress comment formatting
2. `claude_cli.py` - Pass through all messages
3. Test with curl

**Steps:**
```python
# 1. Create progress formatter
def format_progress_comment(message: Any) -> str:
    """Convert SDK message to SSE comment"""
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                return f": [PROGRESS] {block.name}: {summarize_input(block.input)}\n\n"
    return ""

# 2. Inject into stream
async def stream_chat_completion(...):
    async for message in claude_cli.query(...):
        # Progress comments
        progress = format_progress_comment(message)
        if progress:
            yield progress

        # Regular response
        text = extract_text(message)
        if text:
            yield format_openai_chunk(text)
```

**Testing:**
```bash
# Test with curl
curl -N http://localhost:8010/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "messages": [{"role": "user", "content": "/sc:research --depth quick\n\nPython 3.13"}],
    "stream": true
  }' | grep -E "(PROGRESS|data:)"
```

**Expected Output:**
```
: [PROGRESS] TodoWrite: 5 tasks
: [PROGRESS] WebSearch: Python 3.13 release
: [PROGRESS] WebSearch: Python async features
: [PROGRESS] Context7: Python documentation
: [PROGRESS] TodoWrite: Task completed
data: {"choices":[...]}
```

### Enhanced Version (Custom Events) - 1 Day

**Additional Features:**
- Structured JSON events
- Todo progress tracking
- Research phase indicators
- Tool execution timeline

---

## 🔬 Research Example - What User Would See

**Current (Black Box):**
```
POST /v1/chat/completions
→ ... [5 minutes of silence] ...
→ {"choices":[{"message":{"content":"# Research Report\n\n..."}}]}
```

**With Progress (SSE Comments):**
```
POST /v1/chat/completions --stream
→ : [PROGRESS] Research initialized - 5 tasks planned
→ : [PROGRESS] Phase 1: Query Analysis
→ : [PROGRESS] WebSearch: Python 3.13 release date
→ : [PROGRESS] WebSearch: Python 3.13 key features
→ : [PROGRESS] WebSearch: Python async improvements
→ : [PROGRESS] Context7: Loading Python official docs
→ : [PROGRESS] Phase 2: Evidence Collection
→ : [PROGRESS] TodoWrite: 3/5 tasks completed
→ : [PROGRESS] Phase 3: Synthesis
→ data: {"choices":[{"delta":{"content":"# Python 3.13..."}}]}
→ data: {"choices":[{"delta":{"content":"Release Date..."}}]}
```

---

## ✅ Benefits Analysis

### User Experience:
- ✅ **Visibility**: User sieht was passiert
- ✅ **Confidence**: Prozess läuft, nicht gehängt
- ✅ **Patience**: 5 Min warten ist OK wenn man Progress sieht
- ✅ **Debugging**: Bei Problemen sieht man wo es stuck ist

### Development:
- ✅ **Backward Compatible**: Existing clients funktionieren weiter
- ✅ **Incremental**: Phase 1 (quick), Phase 2 (enhanced)
- ✅ **Minimal Changes**: Nur main.py + claude_cli.py
- ✅ **No Breaking Changes**: OpenAI API bleibt kompatibel

### Limitations:
- ⚠️ Standard OpenAI clients ignorieren comments (brauchen custom UI)
- ⚠️ SSE comments sind informativ, nicht interaktiv
- ⚠️ Keine bidirektionale Kommunikation

---

## 🚀 Next Steps

**WARTE AUF USER APPROVAL:**

**Fragen:**
1. Soll ich Phase 1 (SSE Comments) implementieren?
2. Reicht Terminal output oder brauchst du ein UI?
3. Welche Progress Info ist am wichtigsten?
   - Tool names (WebSearch, Context7)?
   - Todo progress (3/5 completed)?
   - Research phase (Searching, Analyzing, Synthesizing)?
   - Time estimates?

**Vorschlag:**
- ✅ Implementiere Phase 1 (SSE Comments) - 2h Aufwand
- ✅ Teste mit curl + Terminal
- ✅ Dann entscheiden ob Phase 2 nötig

---

**Dokumentiert von**: Claude (AI Assistant)
**Status**: ⏳ PENDING USER APPROVAL
**Next**: Implementation nach User Freigabe
