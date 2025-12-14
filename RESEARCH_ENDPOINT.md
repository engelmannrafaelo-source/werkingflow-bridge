# Research Endpoint Documentation

## Overview

The `/v1/research` endpoint provides a dedicated API for executing Claude Code research tasks with automatic file discovery and host filesystem integration.

## Features

- **Custom Model Selection**: Choose which Claude model to use for research
- **SuperClaude Research Depth**: Control research thoroughness with 4 depth levels (quick, standard, deep, exhaustive)
- **Planning Strategies**: Choose between immediate, clarification-first, or collaborative planning modes
- **Advanced Research Control**: Fine-tune search hops, confidence thresholds, parallel searches, and source filtering
- **Automatic File Discovery**: Finds generated markdown reports in Claude Code sessions
- **Host Filesystem Integration**: Automatically saves research reports to your specified path
- **Docker-Optimized**: Seamless file transfer from container to host via volume mounts
- **Execution Tracking**: Returns execution time, file size, and session metadata

## API Reference

### Endpoint

```
POST /v1/research
```

### Request Model

```json
{
  "query": "string (required)",
  "model": "string (optional, default: claude-sonnet-4-5-20250929)",
  "output_path": "string (optional, default: /tmp/)",

  // SuperClaude Research Depth
  "depth": "quick|standard|deep|exhaustive (optional, default: standard)",

  // SuperClaude Planning Strategy
  "strategy": "planning|intent|unified (optional, default: unified)",

  // Advanced Research Options
  "max_hops": "integer (optional, 1-5, overrides depth setting)",
  "confidence_threshold": "float (optional, 0.0-1.0, default: 0.7)",
  "parallel_searches": "integer (optional, 1-5, default: 5)",
  "source_filter": "array (optional, values: tier_1, tier_2, tier_3, tier_4)",

  // General Options
  "max_tokens": "integer (optional, default: 4000)",
  "max_turns": "integer (optional, default: 30)"
}
```

### Request Parameters

#### Core Parameters
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | ✅ | - | Research question or topic to investigate |
| `model` | string | ❌ | `claude-sonnet-4-5-20250929` | Claude model to use (see Available Models) |
| `output_path` | string | ❌ | `/tmp/[filename].md` | Host filesystem path where report should be saved |

#### SuperClaude Research Depth
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `depth` | string | ❌ | `standard` | Research depth level:<br>• `quick`: 1-2 min, 1 hop, ~10 sources<br>• `standard`: 3-5 min, 2-3 hops, ~20 sources<br>• `deep`: 5-8 min, 3-4 hops, ~40 sources<br>• `exhaustive`: 8-15 min, 5 hops, 50+ sources |

#### SuperClaude Planning Strategy
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `strategy` | string | ❌ | `unified` | Planning approach:<br>• `planning`: Immediate execution, no clarification<br>• `intent`: Clarification questions first, then execution<br>• `unified`: Collaborative planning with user feedback |

#### Advanced Research Options
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `max_hops` | integer | ❌ | depth-dependent | Number of research iterations (1-5). Overrides `depth` setting if specified |
| `confidence_threshold` | float | ❌ | 0.7 | Minimum confidence score (0.0-1.0) required for research completion |
| `parallel_searches` | integer | ❌ | 5 | Number of parallel web searches per hop (1-5). Higher = faster but more API usage |
| `source_filter` | array | ❌ | all tiers | Source credibility filter:<br>• `tier_1`: Academic journals, government, official docs (0.9-1.0)<br>• `tier_2`: Established media, industry reports (0.7-0.9)<br>• `tier_3`: Community resources, verified social media (0.5-0.7)<br>• `tier_4`: Forums, unverified sources (0.3-0.5) |

#### General Options
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `max_tokens` | integer | ❌ | 4000 | Maximum tokens for response generation |
| `max_turns` | integer | ❌ | 30 | Maximum conversation turns for research task |

### Response Model

```json
{
  "status": "success|error",
  "query": "string",
  "model": "string",
  "output_file": "string|null",
  "container_file": "string|null",
  "execution_time_seconds": "float|null",
  "file_size_bytes": "integer|null",
  "error": "string|null",
  "session_id": "string|null"
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Either `"success"` or `"error"` |
| `query` | string | Original research query |
| `model` | string | Claude model used |
| `output_file` | string\|null | **Host** filesystem path where report was saved |
| `container_file` | string\|null | **Container** filesystem path (internal) |
| `execution_time_seconds` | float\|null | Time taken to complete research |
| `file_size_bytes` | integer\|null | Size of generated report |
| `error` | string\|null | Error message if status is `"error"` |
| `session_id` | string\|null | Claude Code session ID |

## Available Models

```
claude-sonnet-4-5-20250929  # 🔥 Recommended - Best for coding & research
claude-sonnet-4-20250514    # Previous Sonnet 4
claude-opus-4-20250514      # Maximum intelligence
claude-haiku-4-5-20251022   # Fast & cost-effective
```

## Research Depth Levels Explained

### Quick (`depth: "quick"`)
**Best for**: Simple factual queries, definitions, quick overviews
- **Time**: 1-2 minutes
- **Hops**: 1 iteration
- **Sources**: ~10 sources
- **Confidence Target**: 0.6
- **Use case**: "What is Docker?", "Define quantum computing"

### Standard (`depth: "standard"`) ⭐ Default
**Best for**: General research questions, moderate complexity topics
- **Time**: 3-5 minutes
- **Hops**: 2-3 iterations
- **Sources**: ~20 sources
- **Confidence Target**: 0.7
- **Use case**: "Compare Python frameworks", "Explain microservices architecture"

### Deep (`depth: "deep"`)
**Best for**: Complex multi-faceted topics, detailed analysis
- **Time**: 5-8 minutes
- **Hops**: 3-4 iterations
- **Sources**: ~40 sources
- **Confidence Target**: 0.8
- **Use case**: "Analyze AI safety landscape 2025", "Deep dive into Kubernetes networking"

### Exhaustive (`depth: "exhaustive"`)
**Best for**: Comprehensive research, critical decisions, academic-level detail
- **Time**: 8-15 minutes
- **Hops**: 5 iterations
- **Sources**: 50+ sources
- **Confidence Target**: 0.9
- **Use case**: "Complete analysis of enterprise security frameworks", "Comprehensive market research"

## Planning Strategies Explained

### Planning (`strategy: "planning"`)
- **Behavior**: Immediate execution without clarification
- **Best for**: Clear, specific queries with well-defined scope
- **Interaction**: No user questions, straight to research
- **Example**: "Research Docker networking concepts"

### Intent (`strategy: "intent"`)
- **Behavior**: Clarification questions first (max 3), then execution
- **Best for**: Ambiguous queries, multiple possible interpretations
- **Interaction**: Claude asks clarifying questions before starting
- **Example**: "Research latest AI developments" → Claude asks about specific areas of interest

### Unified (`strategy: "unified"`) ⭐ Default
- **Behavior**: Collaborative planning with user feedback
- **Best for**: Complex research requiring iterative refinement
- **Interaction**: Presents research plan, gets user approval, executes
- **Example**: Multi-faceted topics where user input helps refine approach

## Usage Examples

### Basic Research

```bash
curl -X POST http://localhost:8000/v1/research \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer test-key' \
  -d '{
    "query": "What are the latest AI developments in 2025?"
  }'
```

**Response:**
```json
{
  "status": "success",
  "query": "What are the latest AI developments in 2025?",
  "model": "claude-sonnet-4-5-20250929",
  "output_file": "/tmp/output.md",
  "container_file": "/app/instances/2025-11-08-1200_.../claudedocs/output.md",
  "execution_time_seconds": 127.45,
  "file_size_bytes": 24576,
  "error": null,
  "session_id": "2025-11-08-1200_a1b2c3d4-..."
}
```

### Custom Model & Output Path

```bash
curl -X POST http://localhost:8000/v1/research \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer test-key' \
  -d '{
    "query": "Explain quantum computing advances in 2024",
    "model": "claude-opus-4-20250514",
    "output_path": "/app/research_output/quantum_2024.md"
  }'
```

### Quick Research (Fast, Simple Queries)

```bash
curl -X POST http://localhost:8000/v1/research \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer test-key' \
  -d '{
    "query": "What is Docker and how does it work?",
    "depth": "quick",
    "strategy": "planning",
    "model": "claude-haiku-4-5-20251022",
    "output_path": "/app/research_output/docker_basics.md"
  }'
```

**Expected**: 1-2 minutes, ~10 sources, immediate execution

### Deep Research (Complex Topics)

```bash
curl -X POST http://localhost:8000/v1/research \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer test-key' \
  -d '{
    "query": "Comprehensive analysis of Kubernetes security best practices 2025",
    "depth": "deep",
    "strategy": "unified",
    "confidence_threshold": 0.85,
    "source_filter": ["tier_1", "tier_2"],
    "output_path": "/app/research_output/k8s_security_deep.md"
  }'
```

**Expected**: 5-8 minutes, ~40 sources, high-quality sources only

### Exhaustive Research (Maximum Thoroughness)

```bash
curl -X POST http://localhost:8000/v1/research \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer test-key' \
  -d '{
    "query": "Complete market analysis: Enterprise AI adoption trends 2024-2025",
    "depth": "exhaustive",
    "strategy": "unified",
    "max_hops": 5,
    "confidence_threshold": 0.9,
    "parallel_searches": 5,
    "source_filter": ["tier_1", "tier_2"],
    "max_turns": 40,
    "output_path": "/app/research_output/ai_market_exhaustive.md"
  }'
```

**Expected**: 8-15 minutes, 50+ sources, comprehensive coverage

### Custom Fine-Tuned Research

```bash
curl -X POST http://localhost:8000/v1/research \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer test-key' \
  -d '{
    "query": "Latest developments in Rust async programming",
    "max_hops": 3,
    "confidence_threshold": 0.75,
    "parallel_searches": 4,
    "source_filter": ["tier_1", "tier_2", "tier_3"],
    "strategy": "intent",
    "output_path": "/app/research_output/rust_async.md"
  }'
```

**Note**: Using `max_hops` directly overrides the `depth` setting

### Python Example

```python
import requests

# Basic Research
response = requests.post(
    "http://localhost:8000/v1/research",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer test-key"
    },
    json={
        "query": "Compare Python 3.12 vs 3.13 features",
        "depth": "standard",
        "model": "claude-sonnet-4-5-20250929",
        "output_path": "/app/research_output/python_comparison.md"
    }
)

result = response.json()
print(f"Research saved to: {result['output_file']}")
print(f"Execution time: {result['execution_time_seconds']}s")
print(f"File size: {result['file_size_bytes']} bytes")

# Deep Research with Advanced Options
deep_research = requests.post(
    "http://localhost:8000/v1/research",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer test-key"
    },
    json={
        "query": "Comprehensive analysis of GraphQL vs REST API design patterns",
        "depth": "deep",
        "strategy": "unified",
        "confidence_threshold": 0.85,
        "source_filter": ["tier_1", "tier_2"],
        "parallel_searches": 5,
        "max_turns": 35,
        "output_path": "/app/research_output/api_patterns_deep.md"
    },
    timeout=600  # 10 minute timeout
)

result = deep_research.json()
if result["status"] == "success":
    print(f"✅ Deep research complete: {result['output_file']}")
    print(f"   Sources analyzed: ~40 sources across {result['execution_time_seconds']:.1f}s")
else:
    print(f"❌ Research failed: {result['error']}")
```

### TypeScript Example

```typescript
// Type Definitions
type ResearchDepth = "quick" | "standard" | "deep" | "exhaustive";
type PlanningStrategy = "planning" | "intent" | "unified";
type SourceTier = "tier_1" | "tier_2" | "tier_3" | "tier_4";

interface ResearchRequest {
  // Core Parameters
  query: string;
  model?: string;
  output_path?: string;

  // SuperClaude Research Depth
  depth?: ResearchDepth;

  // SuperClaude Planning Strategy
  strategy?: PlanningStrategy;

  // Advanced Research Options
  max_hops?: number;
  confidence_threshold?: number;
  parallel_searches?: number;
  source_filter?: SourceTier[];

  // General Options
  max_tokens?: number;
  max_turns?: number;
}

interface ResearchResponse {
  status: "success" | "error";
  query: string;
  model: string;
  output_file: string | null;
  container_file: string | null;
  execution_time_seconds: number | null;
  file_size_bytes: number | null;
  error: string | null;
  session_id: string | null;
}

// Basic Research Function
async function conductResearch(
  query: string,
  depth: ResearchDepth = "standard"
): Promise<ResearchResponse> {
  const response = await fetch("http://localhost:8000/v1/research", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer test-key"
    },
    body: JSON.stringify({
      query,
      depth,
      model: "claude-sonnet-4-5-20250929",
      output_path: "/app/research_output/report.md"
    })
  });

  return response.json();
}

// Advanced Research Function
async function conductDeepResearch(
  query: string,
  options?: Partial<ResearchRequest>
): Promise<ResearchResponse> {
  const request: ResearchRequest = {
    query,
    depth: "deep",
    strategy: "unified",
    confidence_threshold: 0.85,
    source_filter: ["tier_1", "tier_2"],
    max_turns: 35,
    output_path: `/app/research_output/${Date.now()}_research.md`,
    ...options
  };

  const response = await fetch("http://localhost:8000/v1/research", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer test-key"
    },
    body: JSON.stringify(request)
  });

  return response.json();
}

// Usage Examples
const result1 = await conductResearch("Latest trends in AI safety");
console.log(`Report: ${result1.output_file}`);

const result2 = await conductDeepResearch(
  "Comprehensive analysis of enterprise Kubernetes security patterns",
  {
    depth: "exhaustive",
    max_hops: 5,
    confidence_threshold: 0.9
  }
);
console.log(`Deep research: ${result2.output_file} (${result2.execution_time_seconds}s)`);
```

## Docker Setup

### Volume Mount Configuration

The research endpoint requires a volume mount to save files from container to host:

**docker-compose.yml:**
```yaml
services:
  wrapper-1:
    volumes:
      - ~/eco-research-output:/app/research_output
```

### Output Path Guidelines

**✅ Recommended Paths** (accessible via volume mount):
```bash
/app/research_output/my_research.md       # → ~/eco-research-output/my_research.md
/app/research_output/reports/ai_2025.md  # → ~/eco-research-output/reports/ai_2025.md
```

**⚠️ Container-Only Paths** (not accessible on host without docker cp):
```bash
/tmp/research.md                # Only in container
/app/instances/.../output.md    # Only in container (session directory)
```

### Accessing Research Files

**Option 1: Via Volume Mount** (Recommended)
```bash
# Request with volume-mounted path
curl -X POST http://localhost:8000/v1/research \
  -d '{"query": "...", "output_path": "/app/research_output/report.md"}'

# File appears immediately on host
cat ~/eco-research-output/report.md
```

**Option 2: Via docker cp** (Fallback)
```bash
# Request without output_path (saves to /tmp/)
curl -X POST http://localhost:8000/v1/research \
  -d '{"query": "..."}'

# Response contains container_file path
# Copy from container to host
docker cp eco-wrapper-1:/tmp/output.md ~/research/output.md
```

## Error Handling

### Common Errors

**1. Invalid Model**
```json
{
  "status": "error",
  "error": "Model 'invalid-model' not found"
}
```

**2. Research Execution Failure**
```json
{
  "status": "error",
  "error": "No result received from Claude Code execution",
  "execution_time_seconds": 0.5
}
```

**3. File Discovery Failure** (non-critical)
```json
{
  "status": "success",
  "output_file": null,
  "container_file": null,
  "error": null
}
```
*Note: Research completed but no markdown file was generated*

### HTTP Status Codes

- `200 OK`: Research completed (check `status` field for success/error)
- `401 Unauthorized`: Invalid or missing API key
- `422 Unprocessable Entity`: Invalid request parameters
- `500 Internal Server Error`: Server error (check logs)

## Performance Notes

### Execution Time by Depth Level

| Depth Level | Time Range | Hops | Sources | Best Use Case |
|-------------|------------|------|---------|---------------|
| **Quick** | 1-2 min | 1 | ~10 | Simple queries, definitions |
| **Standard** ⭐ | 3-5 min | 2-3 | ~20 | General research |
| **Deep** | 5-8 min | 3-4 | ~40 | Complex analysis |
| **Exhaustive** | 8-15 min | 5 | 50+ | Comprehensive coverage |

Research time depends on:
- **Depth level**: Higher depth = more hops and sources
- **Query complexity**: Broad topics require more exploration
- **Tavily API latency**: Web search response times
- **Model processing**: Sonnet/Opus slower but more thorough than Haiku
- **Parallel searches**: More parallel searches = faster but higher API usage
- **Confidence threshold**: Higher threshold may trigger additional searches

### Recommended Configurations

**Quick Answers** (1-2 min):
```json
{
  "query": "What is Redis and when to use it?",
  "depth": "quick",
  "strategy": "planning",
  "model": "claude-haiku-4-5-20251022"
}
```

**Standard Research** (3-5 min) ⭐ Default:
```json
{
  "query": "Compare React Server Components vs traditional SSR",
  "depth": "standard",
  "strategy": "unified",
  "model": "claude-sonnet-4-5-20250929"
}
```

**Deep Analysis** (5-8 min):
```json
{
  "query": "Comprehensive analysis of microservices security patterns",
  "depth": "deep",
  "strategy": "unified",
  "confidence_threshold": 0.85,
  "source_filter": ["tier_1", "tier_2"],
  "model": "claude-sonnet-4-5-20250929"
}
```

**Exhaustive Research** (8-15 min):
```json
{
  "query": "Complete enterprise cloud migration strategy 2025",
  "depth": "exhaustive",
  "strategy": "unified",
  "max_hops": 5,
  "confidence_threshold": 0.9,
  "source_filter": ["tier_1", "tier_2"],
  "max_turns": 40,
  "model": "claude-opus-4-20250514"
}
```

### Performance Optimization Tips

**Faster Research**:
- Use `depth: "quick"` for simple queries
- Set `strategy: "planning"` to skip clarification phase
- Use `claude-haiku-4-5-20251022` model
- Reduce `parallel_searches` to 3 if Tavily API is slow
- Lower `confidence_threshold` to 0.6

**More Thorough Research**:
- Use `depth: "deep"` or `depth: "exhaustive"`
- Set `strategy: "unified"` for collaborative planning
- Use `claude-opus-4-20250514` model for maximum intelligence
- Increase `parallel_searches` to 5 (max)
- Raise `confidence_threshold` to 0.85-0.9
- Add `source_filter: ["tier_1", "tier_2"]` for high-quality sources only

## Monitoring

### Health Check

```bash
curl http://localhost:8000/health
```

### View Active Research

```bash
# Check Docker logs
docker logs eco-wrapper-1 --tail 50 --follow

# Look for research activity
docker logs eco-wrapper-1 | grep -E "🔬|Research|research"
```

### Check Output Directory

```bash
# List all research files
ls -lh ~/eco-research-output/

# Monitor for new files
watch -n 2 "ls -lh ~/eco-research-output/"
```

## Security

### API Key Protection

The research endpoint requires the same authentication as other endpoints:

```bash
# Set API_KEY in environment or generate at startup
export API_KEY="your-secure-key"

# Use in requests
curl -H "Authorization: Bearer your-secure-key" ...
```

### File Access Control

- Research reports are saved with container user permissions (`claude`)
- Host files inherit your user permissions via volume mount
- No sensitive data should be included in research queries (logged)

## Troubleshooting

### Research Not Starting

**Check container logs:**
```bash
docker logs eco-wrapper-1 --tail 100 | grep -i "error\|failed"
```

**Verify Tavily API key:**
```bash
docker exec eco-wrapper-1 env | grep TAVILY_API_KEY
```

### Files Not Appearing on Host

**Verify volume mount:**
```bash
docker inspect eco-wrapper-1 | grep -A5 "Mounts"
```

**Check output path:**
```bash
# Use volume-mounted path
output_path: "/app/research_output/file.md"  # ✅ Works

# NOT accessible without docker cp
output_path: "/tmp/file.md"  # ❌ Container-only
```

### Slow Research Execution

**Increase timeout:**
```yaml
# docker-compose.yml
environment:
  - MAX_TIMEOUT=3600000  # 60 minutes (default: 40min)
```

**Reduce turns:**
```json
{
  "max_turns": 10  // Faster but less thorough
}
```

## Integration with Other Systems

### eco-backend Integration

```python
from pathlib import Path
from typing import Optional, List, Literal
import httpx

# Type aliases
ResearchDepth = Literal["quick", "standard", "deep", "exhaustive"]
PlanningStrategy = Literal["planning", "intent", "unified"]
SourceTier = Literal["tier_1", "tier_2", "tier_3", "tier_4"]

class ResearchClient:
    def __init__(self, wrapper_url: str, api_key: str):
        self.wrapper_url = wrapper_url
        self.api_key = api_key

    async def conduct_research(
        self,
        query: str,
        output_dir: Path,
        depth: ResearchDepth = "standard",
        strategy: PlanningStrategy = "unified",
        confidence_threshold: float = 0.7,
        source_filter: Optional[List[SourceTier]] = None,
        model: str = "claude-sonnet-4-5-20250929"
    ) -> Path:
        """Conduct research with SuperClaude options and return path to report"""
        request_data = {
            "query": query,
            "depth": depth,
            "strategy": strategy,
            "confidence_threshold": confidence_threshold,
            "model": model,
            "output_path": f"/app/research_output/{output_dir.name}.md"
        }

        if source_filter:
            request_data["source_filter"] = source_filter

        response = await httpx.post(
            f"{self.wrapper_url}/v1/research",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=request_data,
            timeout=900.0  # 15 minutes for exhaustive research
        )

        result = response.json()
        if result["status"] == "error":
            raise Exception(result["error"])

        # Return host path
        return Path.home() / "eco-research-output" / f"{output_dir.name}.md"

    async def quick_research(self, query: str, output_dir: Path) -> Path:
        """Quick research for simple queries (1-2 min)"""
        return await self.conduct_research(
            query=query,
            output_dir=output_dir,
            depth="quick",
            strategy="planning",
            model="claude-haiku-4-5-20251022"
        )

    async def deep_research(self, query: str, output_dir: Path) -> Path:
        """Deep research for complex topics (5-8 min)"""
        return await self.conduct_research(
            query=query,
            output_dir=output_dir,
            depth="deep",
            strategy="unified",
            confidence_threshold=0.85,
            source_filter=["tier_1", "tier_2"]
        )

# Usage example
client = ResearchClient(
    wrapper_url="http://localhost:8010/v1",
    api_key="test-key"
)

# Quick research
quick_result = await client.quick_research(
    "What is FastAPI?",
    Path("fastapi_basics")
)

# Deep research
deep_result = await client.deep_research(
    "Comprehensive analysis of Python async patterns",
    Path("python_async_deep")
)
```

### eco-diagnostics Integration

```typescript
import { readFile } from 'fs/promises';

type ResearchDepth = "quick" | "standard" | "deep" | "exhaustive";
type PlanningStrategy = "planning" | "intent" | "unified";
type SourceTier = "tier_1" | "tier_2" | "tier_3" | "tier_4";

interface DiagnosticResearch {
  query: string;
  reportPath: string;
  analysis: string;
  executionTime: number;
}

interface ResearchOptions {
  depth?: ResearchDepth;
  strategy?: PlanningStrategy;
  confidenceThreshold?: number;
  sourceTiers?: SourceTier[];
}

class DiagnosticsResearchClient {
  constructor(
    private wrapperUrl: string = 'http://localhost:8020/v1',
    private apiKey: string = 'test-key'
  ) {}

  async analyzeWithResearch(
    issue: string,
    options: ResearchOptions = {}
  ): Promise<DiagnosticResearch> {
    const {
      depth = "standard",
      strategy = "unified",
      confidenceThreshold = 0.7,
      sourceTiers = ["tier_1", "tier_2"]
    } = options;

    const response = await fetch(`${this.wrapperUrl}/research`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${this.apiKey}`
      },
      body: JSON.stringify({
        query: `Diagnose and analyze: ${issue}`,
        depth,
        strategy,
        confidence_threshold: confidenceThreshold,
        source_filter: sourceTiers,
        output_path: '/app/research_output/diagnostic_report.md'
      })
    });

    const result = await response.json();

    if (result.status === 'error') {
      throw new Error(`Research failed: ${result.error}`);
    }

    const reportContent = await readFile(
      `${process.env.HOME}/eco-research-output/diagnostic_report.md`,
      'utf-8'
    );

    return {
      query: issue,
      reportPath: result.output_file,
      analysis: reportContent,
      executionTime: result.execution_time_seconds
    };
  }

  async quickDiagnosis(issue: string): Promise<DiagnosticResearch> {
    return this.analyzeWithResearch(issue, {
      depth: "quick",
      strategy: "planning"
    });
  }

  async deepDiagnosis(issue: string): Promise<DiagnosticResearch> {
    return this.analyzeWithResearch(issue, {
      depth: "deep",
      strategy: "unified",
      confidenceThreshold: 0.85
    });
  }
}

// Usage example
const client = new DiagnosticsResearchClient();

// Quick diagnosis for simple issues
const quickResult = await client.quickDiagnosis(
  "User login button not responding on mobile"
);

// Deep diagnosis for complex issues
const deepResult = await client.deepDiagnosis(
  "Intermittent API timeouts during peak hours with database deadlocks"
);

console.log(`Deep analysis complete in ${deepResult.executionTime}s`);
```

## Changelog

### v1.1.0 (2025-11-08)
- ✨ **SuperClaude Integration**: Full research depth control
  - Added `depth` parameter: quick, standard, deep, exhaustive
  - Added `strategy` parameter: planning, intent, unified
  - Added advanced options: max_hops, confidence_threshold, parallel_searches, source_filter
- 📚 **Enhanced Documentation**: Comprehensive parameter explanations
  - Detailed depth level descriptions with use cases
  - Planning strategy explanations with interaction patterns
  - Source tier credibility matrix
  - Performance optimization tips
- 🔧 **Request Model Updates**: Extended ResearchRequest with SuperClaude options
- 📊 **Performance Guidance**: Time estimates and configuration recommendations

### v1.0.0 (2025-11-08)
- ✨ Initial release
- Dedicated `/v1/research` endpoint
- Custom model selection
- Automatic file discovery
- Docker volume mount integration
- Execution time tracking

## Support

For issues or questions:
- GitHub: https://github.com/eco-system/eco-openai-wrapper
- Documentation: https://github.com/eco-system/eco-openai-wrapper/docs

---

**Last Updated**: 2025-11-08
