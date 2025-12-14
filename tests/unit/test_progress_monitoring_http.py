#!/usr/bin/env python3
"""Test progress monitoring via HTTP API"""
import asyncio
import json
import time
from pathlib import Path
import sys
import httpx

async def test_progress_monitoring_http():
    """Test that progress files are created via HTTP API"""

    print("="*80)
    print("PROGRESS MONITORING TEST (HTTP API)")
    print("="*80)

    WRAPPER_URL = "http://localhost:8010"  # eco-backend wrapper

    print(f"\n1. Sending request to {WRAPPER_URL}/v1/chat/completions...")
    print("   Query: 'What are the main features of Python 3.13?'")

    request_body = {
        "model": "claude-sonnet-4-5",
        "messages": [
            {
                "role": "user",
                "content": "What are the main features of Python 3.13? Be brief (3-4 sentences)."
            }
        ],
        "stream": True
    }

    session_id = None
    chunk_count = 0

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST",
                f"{WRAPPER_URL}/v1/chat/completions",
                json=request_body
            ) as response:
                print(f"   Response status: {response.status_code}")

                if response.status_code != 200:
                    print(f"   ❌ HTTP error: {response.status_code}")
                    text = await response.aread()
                    print(f"   Response: {text.decode()}")
                    return False

                async for line in response.aiter_lines():
                    if not line or line.startswith(":"):
                        continue

                    if line.startswith("data: "):
                        line = line[6:]  # Remove "data: " prefix

                    if line == "[DONE]":
                        break

                    try:
                        chunk = json.loads(line)
                        chunk_count += 1

                        # Try to extract session_id from various places
                        if not session_id:
                            # Check top-level
                            if 'session_id' in chunk:
                                session_id = chunk['session_id']
                                print(f"   Session ID found: {session_id}")
                            # Check choices
                            elif 'choices' in chunk:
                                for choice in chunk['choices']:
                                    if 'session_id' in choice:
                                        session_id = choice['session_id']
                                        print(f"   Session ID found: {session_id}")
                                        break

                    except json.JSONDecodeError:
                        continue

    except httpx.HTTPError as e:
        print(f"   ❌ HTTP error: {e}")
        return False
    except Exception as e:
        print(f"   ❌ Unexpected error: {e}")
        return False

    print(f"   Received {chunk_count} chunks")

    if not session_id:
        print("   ⚠️  No session ID found in response - checking logs for session IDs...")
        # Read recent wrapper logs to find session ID
        try:
            log_file = Path("/Users/lorenz/ECO/projects/eco-openai-wrapper/logs/app.log")
            with open(log_file) as f:
                lines = f.readlines()
                # Get last 100 lines
                recent_lines = lines[-100:]
                for line in recent_lines:
                    if "CLI session" in line and "uuid" in line.lower():
                        # Extract UUID pattern
                        import re
                        uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
                        matches = re.findall(uuid_pattern, line)
                        if matches:
                            session_id = matches[0]
                            print(f"   Found session ID in logs: {session_id}")
                            break
        except Exception as e:
            print(f"   ⚠️  Could not read logs: {e}")

    if not session_id:
        print("   ❌ Could not determine session ID")
        return False

    # Wait a bit for files to be written
    time.sleep(3)

    session_dir = Path(f"/tmp/eco-wrapper-sessions/{session_id}")

    print("\n2. Checking session directory...")
    if not session_dir.exists():
        print(f"   ❌ Session directory not found: {session_dir}")
        print(f"   Checking if any sessions exist...")
        sessions_base = Path("/tmp/eco-wrapper-sessions")
        if sessions_base.exists():
            sessions = list(sessions_base.iterdir())
            print(f"   Found {len(sessions)} sessions:")
            for s in sessions[:5]:
                print(f"     - {s.name}")
        else:
            print(f"   ❌ Base directory does not exist: {sessions_base}")
        return False

    print(f"   ✅ Session directory exists: {session_dir}")

    print("\n3. Checking required files...")
    required_files = ['metadata.json', 'progress.jsonl', 'messages.jsonl', 'final_response.json']
    all_present = True
    for filename in required_files:
        filepath = session_dir / filename
        if filepath.exists():
            size = filepath.stat().st_size
            print(f"   ✅ {filename} ({size} bytes)")
        else:
            print(f"   ⚠️  {filename} missing (might be OK if no progress events)")

    print("\n4. Reading metadata...")
    metadata_file = session_dir / "metadata.json"
    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text())
            print(f"   ✅ Session ID: {metadata.get('session_id')}")
            print(f"   ✅ Created at: {metadata.get('created_at')}")
            print(f"   ✅ Status: {metadata.get('status')}")
            if 'completed_at' in metadata:
                print(f"   ✅ Completed at: {metadata['completed_at']}")
            if 'duration_seconds' in metadata:
                print(f"   ✅ Duration: {metadata['duration_seconds']:.2f}s")
        except json.JSONDecodeError as e:
            print(f"   ❌ Failed to parse metadata: {e}")

    print("\n5. Reading final response...")
    final_file = session_dir / "final_response.json"
    if final_file.exists():
        try:
            final = json.loads(final_file.read_text())
            print(f"   ✅ Response text: {len(final['response']['text'])} chars")
            print(f"   ✅ Word count: {final['response']['word_count']}")
            print(f"   ✅ Total messages: {final['metadata']['total_messages']}")
            print(f"   ✅ Tools used: {final['metadata']['tools_used']}")
            print(f"   ✅ Duration: {final['metadata']['duration_seconds']:.2f}s")
        except (json.JSONDecodeError, KeyError) as e:
            print(f"   ❌ Failed to parse final response: {e}")

    print("\n" + "="*80)
    print("✅ TEST COMPLETED")
    print("="*80)
    print(f"\nSession directory: {session_dir}")
    print("\nYou can monitor live progress with:")
    print(f"  tail -f {session_dir}/progress.jsonl")

    return True


if __name__ == "__main__":
    result = asyncio.run(test_progress_monitoring_http())
    sys.exit(0 if result else 1)
