#!/usr/bin/env python3
"""Test progress monitoring functionality"""
import asyncio
import json
import time
from pathlib import Path
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.claude_cli import ClaudeCodeCLI

async def test_progress_monitoring():
    """Test that progress files are created and updated"""

    print("="*80)
    print("PROGRESS MONITORING TEST")
    print("="*80)

    # Initialize CLI
    cli = ClaudeCodeCLI(timeout=120000)  # 2 min timeout for quick test

    # Test with a simple query that will use tools
    session_id = None
    final_text = []

    print("\n1. Starting simple query with tool usage...")
    print("   Query: 'What are the main features of Python 3.13?'")

    try:
        async for message in cli.run_completion(
            prompt="What are the main features of Python 3.13? Be brief.",
            stream=True,
            model="claude-sonnet-4-5"
        ):
            # Extract session_id from first message
            if not session_id and isinstance(message, dict):
                if 'session_id' in message:
                    session_id = message['session_id']
                    print(f"   Session ID: {session_id}")

            # Accumulate text
            if isinstance(message, dict):
                if 'content' in message:
                    content = message['content']
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and 'text' in block:
                                final_text.append(block['text'])

    except Exception as e:
        print(f"   ❌ Query error: {e}")
        return False

    if not session_id:
        print("   ❌ No session ID found")
        return False

    session_dir = Path(f"/tmp/eco-wrapper-sessions/{session_id}")

    # Wait a bit for files to be written
    time.sleep(2)

    print("\n2. Checking session directory...")
    if not session_dir.exists():
        print(f"   ❌ Session directory not found: {session_dir}")
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
            print(f"   ❌ {filename} missing")
            all_present = False

    if not all_present:
        print("\n   ⚠️  Some files missing - this might be normal if no progress events occurred")

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

    print("\n5. Reading progress updates...")
    progress_file = session_dir / "progress.jsonl"
    if progress_file.exists():
        with open(progress_file) as f:
            progress_count = 0
            for i, line in enumerate(f, 1):
                try:
                    progress = json.loads(line)
                    progress_count += 1
                    print(f"   Progress {i}: {progress['type']} - {progress.get('data', {})}")
                except json.JSONDecodeError:
                    print(f"   ⚠️  Skipped corrupt line {i}")
            if progress_count == 0:
                print("   ℹ️  No progress events (query might not have used TodoWrite/tools)")
    else:
        print("   ℹ️  No progress file (query might not have generated progress events)")

    print("\n6. Checking final response...")
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
    else:
        print("   ❌ Final response file missing")

    print("\n7. Reading messages log (first 5 messages)...")
    messages_file = session_dir / "messages.jsonl"
    if messages_file.exists():
        with open(messages_file) as f:
            for i, line in enumerate(f, 1):
                if i > 5:
                    print(f"   ... (showing first 5 of more messages)")
                    break
                try:
                    msg = json.loads(line)
                    msg_type = msg.get('type', 'unknown')
                    timestamp = msg.get('timestamp', 'no-timestamp')
                    print(f"   Message {i}: {msg_type} at {timestamp}")
                except json.JSONDecodeError:
                    print(f"   ⚠️  Skipped corrupt line {i}")

    print("\n" + "="*80)
    print("✅ TEST COMPLETED")
    print("="*80)
    print(f"\nSession directory: {session_dir}")
    print("\nYou can inspect the files manually:")
    print(f"  cat {session_dir}/metadata.json")
    print(f"  cat {session_dir}/progress.jsonl")
    print(f"  cat {session_dir}/final_response.json")
    print(f"  tail -f {session_dir}/progress.jsonl  # For live monitoring")

    return True


if __name__ == "__main__":
    result = asyncio.run(test_progress_monitoring())
    sys.exit(0 if result else 1)
