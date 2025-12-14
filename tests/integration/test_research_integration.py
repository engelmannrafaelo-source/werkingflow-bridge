"""
Integration Test - Research with /sc:research

Testet echte Research-Anfragen über den OpenAI Wrapper unter Verwendung
des SuperClaude /sc:research Commands.

WICHTIG: Dieser Test:
1. Macht ECHTE API Calls zum Wrapper
2. Verwendet ECHTE Claude CLI OAuth Authentication
3. Führt ECHTE Research durch (dauert mehrere Minuten)
4. Erstellt ECHTE Research-Reports in claudedocs/

Setup Requirements:
- Wrapper muss laufen (./start-wrappers.sh)
- Claude CLI OAuth muss authentifiziert sein
- WRAPPER_URL und WRAPPER_API_KEY env vars müssen gesetzt sein
"""

import pytest
import os
import httpx
import asyncio
import json
from pathlib import Path
from datetime import datetime

# Skip wenn nicht explizit angefordert
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_RESEARCH_TESTS"),
    reason="Research tests nur mit RUN_RESEARCH_TESTS=1 ausführen"
)


# ============================================================================
# Configuration
# ============================================================================

WRAPPER_BASE_URL = os.environ.get("WRAPPER_URL", "http://localhost:8000")
WRAPPER_API_KEY = os.environ.get("WRAPPER_API_KEY", "")
WRAPPER_TIMEOUT = 600  # 10 minutes für Research


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def wrapper_client():
    """HTTP Client für Wrapper API."""
    headers = {}
    if WRAPPER_API_KEY:
        headers["Authorization"] = f"Bearer {WRAPPER_API_KEY}"

    return httpx.Client(
        base_url=WRAPPER_BASE_URL,
        headers=headers,
        timeout=WRAPPER_TIMEOUT
    )


@pytest.fixture
def research_output_dir():
    """Directory für Research Outputs."""
    output_dir = Path(__file__).parent / "research_outputs"
    output_dir.mkdir(exist_ok=True)
    return output_dir


# ============================================================================
# Helper Functions
# ============================================================================

def check_wrapper_health(client: httpx.Client) -> bool:
    """Check ob Wrapper läuft."""
    try:
        response = client.get("/health")
        return response.status_code == 200
    except Exception as e:
        return False


def check_claudedocs_directory() -> Path:
    """Finde claudedocs/ directory wo Research Reports landen."""
    # Check verschiedene mögliche Locations
    possible_paths = [
        Path("/Users/lorenz/ECO/projects/eco-openai-wrapper/claudedocs"),
        Path.cwd() / "claudedocs",
        Path.home() / "claudedocs"
    ]

    for path in possible_paths:
        if path.exists():
            return path

    # Wenn nicht gefunden, return expected location
    return possible_paths[0]


def find_latest_research_report(claudedocs_dir: Path) -> Path | None:
    """Finde neuesten Research Report in claudedocs/."""
    if not claudedocs_dir.exists():
        return None

    # Find all markdown files
    md_files = list(claudedocs_dir.glob("*.md"))
    if not md_files:
        return None

    # Return newest file
    return max(md_files, key=lambda p: p.stat().st_mtime)


# ============================================================================
# Test Class: Basic Research
# ============================================================================

class TestBasicResearch:
    """Tests für einfache Research Anfragen."""

    def test_wrapper_is_running(self, wrapper_client):
        """Wrapper sollte erreichbar sein."""
        is_healthy = check_wrapper_health(wrapper_client)

        assert is_healthy, (
            f"❌ Wrapper nicht erreichbar unter {WRAPPER_BASE_URL}\n"
            "Starte Wrapper mit: ./start-wrappers.sh"
        )

    def test_research_simple_topic(self, wrapper_client, research_output_dir):
        """
        /sc:research mit einfachem Topic.

        Topic: "Python async/await best practices"
        Erwartung: Research Report wird erstellt
        """
        print("\n" + "="*80)
        print("🔬 Starting Research: Python async/await best practices")
        print("="*80)

        # Research Query via OpenAI-compatible API
        research_query = "/sc:research Python async/await best practices 2024"

        request_payload = {
            "model": "claude-sonnet-4-20250514",
            "messages": [
                {
                    "role": "user",
                    "content": research_query
                }
            ],
            "stream": False,
            "enable_tools": True  # KRITISCH: Research braucht Tools!
        }

        # Execute Research
        start_time = datetime.now()
        print(f"⏱️  Start: {start_time.strftime('%H:%M:%S')}")

        # System Info for Debugging
        import platform
        import sys
        print(f"\n🖥️  System Info:")
        print(f"   Hostname: {platform.node()}")
        print(f"   Python: {sys.version.split()[0]}")
        print(f"   Platform: {platform.platform()}")
        print(f"   Wrapper URL: {WRAPPER_BASE_URL}")

        response = wrapper_client.post(
            "/v1/chat/completions",
            json=request_payload
        )

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        print(f"⏱️  End: {end_time.strftime('%H:%M:%S')} (Duration: {duration:.1f}s)")

        # Check Response
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"

        result = response.json()
        assert "choices" in result
        assert len(result["choices"]) > 0

        assistant_message = result["choices"][0]["message"]["content"]

        # DEBUGGING: Print full response for analysis
        print(f"\n📊 Response Analysis:")
        print(f"   Length: {len(assistant_message)} characters")
        print(f"   First 200 chars: {assistant_message[:200]}")
        if len(assistant_message) < 500:
            print(f"\n⚠️  FULL SHORT RESPONSE:\n{assistant_message}\n")

        assert len(assistant_message) > 100, f"Research response too short: {len(assistant_message)} chars. Response: {assistant_message[:200]}"

        print(f"✅ Response received: {len(assistant_message)} characters")

        # Save response
        output_file = research_output_dir / f"research_async_await_{start_time.strftime('%Y%m%d_%H%M%S')}.txt"
        output_file.write_text(assistant_message)
        print(f"💾 Saved to: {output_file}")

        # Check for Research Report in claudedocs/
        claudedocs_dir = check_claudedocs_directory()
        print(f"📂 Checking for research report in: {claudedocs_dir}")

        if claudedocs_dir.exists():
            latest_report = find_latest_research_report(claudedocs_dir)
            if latest_report:
                print(f"📄 Found research report: {latest_report.name}")
                print(f"   Size: {latest_report.stat().st_size / 1024:.1f} KB")
                print(f"   Modified: {datetime.fromtimestamp(latest_report.stat().st_mtime).strftime('%H:%M:%S')}")
            else:
                print("⚠️  No research report found in claudedocs/")
        else:
            print(f"⚠️  claudedocs/ directory not found at {claudedocs_dir}")


# ============================================================================
# Test Class: Structured Research
# ============================================================================

class TestStructuredResearch:
    """Tests für strukturierte Research Anfragen mit spezifischen Parameters."""

    def test_research_with_depth_specification(self, wrapper_client, research_output_dir):
        """
        /sc:research mit expliziter Depth-Angabe.

        Topic: "FastAPI performance optimization techniques"
        Depth: Deep analysis
        """
        print("\n" + "="*80)
        print("🔬 Starting Deep Research: FastAPI Performance Optimization")
        print("="*80)

        research_query = """
/sc:research FastAPI performance optimization techniques

Requirements:
- Focus on production environments
- Include benchmarking data
- Cover async best practices
- Depth: comprehensive analysis
"""

        request_payload = {
            "model": "claude-sonnet-4-20250514",
            "messages": [
                {
                    "role": "user",
                    "content": research_query
                }
            ],
            "stream": False,
            "enable_tools": True  # KRITISCH: Research braucht Tools!
        }

        start_time = datetime.now()
        print(f"⏱️  Start: {start_time.strftime('%H:%M:%S')}")

        # System Info for Debugging
        import platform
        import sys
        print(f"\n🖥️  System Info:")
        print(f"   Hostname: {platform.node()}")
        print(f"   Python: {sys.version.split()[0]}")
        print(f"   Platform: {platform.platform()}")
        print(f"   Wrapper URL: {WRAPPER_BASE_URL}")

        response = wrapper_client.post(
            "/v1/chat/completions",
            json=request_payload
        )

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        print(f"⏱️  End: {end_time.strftime('%H:%M:%S')} (Duration: {duration:.1f}s)")

        assert response.status_code == 200
        result = response.json()

        assistant_message = result["choices"][0]["message"]["content"]

        # DEBUGGING: Print full response for analysis
        print(f"\n📊 Response Analysis:")
        print(f"   Length: {len(assistant_message)} characters")
        print(f"   First 200 chars: {assistant_message[:200]}")
        if len(assistant_message) < 500:
            print(f"\n⚠️  FULL SHORT RESPONSE:\n{assistant_message}\n")

        # Deep research sollte mehr Inhalt haben
        assert len(assistant_message) > 500, f"Deep research response too short: {len(assistant_message)} chars. Response: {assistant_message[:200]}"

        print(f"✅ Deep research completed: {len(assistant_message)} characters")

        # Check for structured content
        content_lower = assistant_message.lower()
        has_structure = (
            "performance" in content_lower and
            ("benchmark" in content_lower or "optimization" in content_lower)
        )

        assert has_structure, "Research should contain structured performance content"
        print("✅ Research contains structured performance analysis")

        # Save
        output_file = research_output_dir / f"research_fastapi_{start_time.strftime('%Y%m%d_%H%M%S')}.txt"
        output_file.write_text(assistant_message)
        print(f"💾 Saved to: {output_file}")


# ============================================================================
# Test Class: Error Handling
# ============================================================================

class TestResearchErrorHandling:
    """Tests für Error Handling bei Research Anfragen."""

    def test_research_handles_invalid_topic(self, wrapper_client):
        """
        /sc:research sollte graceful mit invalid topics umgehen.
        """
        print("\n" + "="*80)
        print("🧪 Testing Error Handling: Invalid Research Topic")
        print("="*80)

        # Completely nonsensical query
        research_query = "/sc:research xyzabc123invalidtopic98765"

        request_payload = {
            "model": "claude-sonnet-4-20250514",
            "messages": [
                {
                    "role": "user",
                    "content": research_query
                }
            ],
            "stream": False,
            "enable_tools": True  # KRITISCH: Research braucht Tools!
        }

        response = wrapper_client.post(
            "/v1/chat/completions",
            json=request_payload
        )

        # Should still return 200 (not crash)
        assert response.status_code == 200

        result = response.json()
        assistant_message = result["choices"][0]["message"]["content"]

        # Should respond with something (even if it's an error message)
        assert len(assistant_message) > 0
        print(f"✅ Handled invalid topic gracefully: {len(assistant_message)} chars")


# ============================================================================
# Test Class: Performance & Concurrency
# ============================================================================

@pytest.mark.slow
class TestResearchPerformance:
    """Performance Tests für Research (markiert als slow)."""

    def test_research_completes_within_timeout(self, wrapper_client):
        """
        Research sollte innerhalb des Timeouts fertig werden.
        """
        print("\n" + "="*80)
        print("⏱️  Performance Test: Research Timeout Compliance")
        print("="*80)

        research_query = "/sc:research OAuth 2.0 security best practices"

        request_payload = {
            "model": "claude-sonnet-4-20250514",
            "messages": [
                {
                    "role": "user",
                    "content": research_query
                }
            ],
            "stream": False,
            "enable_tools": True  # KRITISCH: Research braucht Tools!
        }

        start_time = datetime.now()

        response = wrapper_client.post(
            "/v1/chat/completions",
            json=request_payload
        )

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print(f"⏱️  Duration: {duration:.1f}s")

        # Should complete within timeout (600s)
        assert duration < WRAPPER_TIMEOUT, f"Research took {duration}s, timeout is {WRAPPER_TIMEOUT}s"
        print(f"✅ Completed within timeout: {duration:.1f}s < {WRAPPER_TIMEOUT}s")


# ============================================================================
# Manual Run Instructions
# ============================================================================

"""
===========================================
MANUAL RUN INSTRUCTIONS
===========================================

1. Start Wrapper:
   cd /Users/lorenz/ECO/projects/eco-openai-wrapper
   ./start-wrappers.sh

2. Set Environment Variables:
   export RUN_RESEARCH_TESTS=1
   export WRAPPER_URL="http://localhost:8000"
   export WRAPPER_API_KEY="your-key-if-needed"

3. Run Tests:
   # Alle Research Tests
   source venv/bin/activate
   pytest tests/integration/test_research_integration.py -v -s

   # Nur schnelle Tests (ohne slow)
   pytest tests/integration/test_research_integration.py -v -s -m "not slow"

   # Nur ein spezifischer Test
   pytest tests/integration/test_research_integration.py::TestBasicResearch::test_research_simple_topic -v -s

4. Check Results:
   # Test Outputs
   ls -lah tests/integration/research_outputs/

   # Research Reports (falls erstellt)
   ls -lah /Users/lorenz/ECO/projects/eco-openai-wrapper/claudedocs/

===========================================
EXPECTED BEHAVIOR
===========================================

✅ test_wrapper_is_running
   - Checks /health endpoint
   - Should return 200 OK

✅ test_research_simple_topic
   - Sends /sc:research command
   - Should return research results
   - May create report in claudedocs/

✅ test_research_with_depth_specification
   - Deep research with requirements
   - Should return comprehensive analysis

✅ test_research_handles_invalid_topic
   - Tests error handling
   - Should not crash

⏱️ test_research_completes_within_timeout (slow)
   - Performance test
   - Should complete < 600s

===========================================
TROUBLESHOOTING
===========================================

❌ "Wrapper nicht erreichbar"
   → Start wrapper: ./start-wrappers.sh
   → Check: curl http://localhost:8000/health

❌ "Authentication failed"
   → Check: claude login
   → Test: claude --print "Hello"

❌ "No research report found"
   → Normal! Research agent may not always create files
   → Check response content instead

❌ Test timeout
   → Increase WRAPPER_TIMEOUT
   → Research can take 2-5 minutes

===========================================
"""
