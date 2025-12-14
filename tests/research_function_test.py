#!/usr/bin/env python3
"""
Research Function Test - Parallel Testing for All 3 Wrapper Instances

Tests /sc:research --depth quick across all wrapper instances with:
- Parallel execution
- File recovery validation
- Content quality checks
- Minimal implementation based on GUIDE_LLM_WRAPPER_RESEARCH_CLIENT.md
"""

import asyncio
import base64
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Tuple
import httpx

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Test configuration - each instance gets a different topic
WRAPPER_INSTANCES = [
    {
        "name": "eco-wrapper-1",
        "port": 8000,
        "topic": "Python async/await best practices",
        "keywords": ["async", "await", "asyncio", "python"]
    },
    {
        "name": "eco-wrapper-2",
        "port": 8010,
        "topic": "Docker container security hardening",
        "keywords": ["docker", "container", "security", "hardening"]
    },
    {
        "name": "eco-wrapper-3",
        "port": 8020,
        "topic": "React hooks performance optimization",
        "keywords": ["react", "hooks", "performance", "optimization"]
    }
]

TIMEOUT = 600.0  # 10 minutes for quick research
OUTPUT_DIR = Path(__file__).parent / "temp_test_output"


class ResearchTestError(Exception):
    """Base exception for research test failures"""
    pass


class FileRecoveryError(ResearchTestError):
    """File recovery validation failed"""
    pass


class ContentValidationError(ResearchTestError):
    """Content quality validation failed"""
    pass


def decode_base64_content(content_base64: str, file_path: str) -> str:
    """
    Decode base64 content with proper error handling.

    LAW 1: Never Silent Failures - explicit errors on all decode failures
    """
    if not content_base64:
        raise ValueError(f"Empty content_base64 for {file_path}")

    try:
        content_bytes = base64.b64decode(content_base64)
    except (base64.binascii.Error, ValueError) as e:
        raise ValueError(f"Failed to decode base64 for {file_path}: {e}") from e

    try:
        return content_bytes.decode('utf-8')
    except UnicodeDecodeError as e:
        raise UnicodeDecodeError(
            e.encoding, e.object, e.start, e.end,
            f"Failed to decode UTF-8 for {file_path}: {e.reason}"
        ) from e


def verify_checksum(content: str, expected: str, file_path: str):
    """Verify SHA256 checksum of decoded content"""
    actual = f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"
    if actual != expected:
        raise ValueError(
            f"Checksum mismatch for {file_path}\n"
            f"  Expected: {expected}\n"
            f"  Actual: {actual}"
        )


def validate_research_content(content: str, session_id: str) -> Dict[str, Any]:
    """
    Validate that content is actual research, not a meta-response.

    Checks:
    - Minimum length (>500 chars for quick research)
    - Contains research indicators (headings, lists, technical terms)
    - NOT just error messages or permission requests
    """
    if len(content) < 500:
        raise ContentValidationError(
            f"Content too short ({len(content)} chars) for research "
            f"(session: {session_id})"
        )

    # Check for meta-response patterns (indicates failure)
    meta_patterns = [
        "I need permission",
        "cannot conduct",
        "Status Update:",
        "permission restrictions",
        "access web research tools"
    ]

    content_lower = content.lower()
    for pattern in meta_patterns:
        if pattern.lower() in content_lower:
            raise ContentValidationError(
                f"Content appears to be meta-response, not research "
                f"(pattern: '{pattern}', session: {session_id})"
            )

    # Check for research indicators (positive signals)
    research_indicators = {
        "headings": ["##", "###"],  # Markdown headings
        "technical": ["async", "await", "function", "class", "best practice"],
        "structure": ["\n- ", "\n* ", "\n1. "]  # Lists
    }

    indicator_count = 0
    for category, patterns in research_indicators.items():
        if any(p in content for p in patterns):
            indicator_count += 1

    if indicator_count < 2:
        raise ContentValidationError(
            f"Content lacks research indicators (only {indicator_count}/3 categories, "
            f"session: {session_id})"
        )

    # Calculate quality metrics
    word_count = len(content.split())
    line_count = len(content.splitlines())

    return {
        "valid": True,
        "word_count": word_count,
        "line_count": line_count,
        "char_count": len(content),
        "indicator_count": indicator_count
    }


async def test_research_instance(
    instance: Dict[str, Any],
    prompt: str,
    timeout: float
) -> Dict[str, Any]:
    """
    Test research function for a single wrapper instance.

    Returns test result with session_id, files, validation status.
    """
    instance_name = instance["name"]
    port = instance["port"]
    topic = instance["topic"]
    keywords = instance["keywords"]
    base_url = f"http://localhost:{port}"

    logger.info(f"🔬 Testing {instance_name} (port {port})...")
    logger.info(f"   Topic: {topic}")

    start_time = datetime.now()
    session_id = None

    try:
        # 1. Send research request
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{base_url}/v1/chat/completions",
                    json={
                        "model": "claude-sonnet-4-5-20250929",
                        "messages": [
                            {"role": "user", "content": f"/sc:research --depth quick\n\n{prompt}"}
                        ],
                        "stream": False,
                        "enable_tools": True  # REQUIRED
                    },
                    headers={
                        "X-Claude-Max-Turns": "20",
                        "X-Claude-Allowed-Tools": "*"
                    },
                    timeout=timeout
                )
                response.raise_for_status()

            except httpx.HTTPStatusError as e:
                session_id = e.response.headers.get("X-Claude-Session-ID", "unknown")
                error_detail = e.response.text[:500] if e.response.text else "No detail"

                if e.response.status_code >= 500:
                    raise ResearchTestError(
                        f"Server error (session: {session_id}): {error_detail}"
                    ) from e
                elif e.response.status_code == 400:
                    raise ResearchTestError(
                        f"Invalid request (session: {session_id}): {error_detail}"
                    ) from e
                else:
                    raise ResearchTestError(
                        f"HTTP {e.response.status_code} (session: {session_id}): {error_detail}"
                    ) from e

            except httpx.TimeoutException as e:
                raise ResearchTestError(
                    f"Timeout after {timeout}s for {instance_name}"
                ) from e

        # 2. Extract session ID (LAW 1: Always in header)
        session_id = response.headers.get("X-Claude-Session-ID")
        if not session_id:
            raise ResearchTestError(
                f"Missing X-Claude-Session-ID header for {instance_name}"
            )

        logger.info(f"  Session ID: {session_id}")

        # 3. Validate response structure
        data = response.json()

        if "x_claude_metadata" not in data:
            raise FileRecoveryError(
                f"Missing x_claude_metadata (session: {session_id})"
            )

        metadata = data["x_claude_metadata"]
        discovery_status = metadata.get("discovery_status", "unknown")

        if discovery_status != "success":
            raise FileRecoveryError(
                f"File discovery failed: {discovery_status} (session: {session_id})"
            )

        files_created = metadata.get("files_created", [])
        logger.info(f"  Files discovered: {len(files_created)}")

        if len(files_created) == 0:
            raise FileRecoveryError(
                f"No files created by research (session: {session_id})"
            )

        # 4. Decode and validate files
        decoded_files = []
        research_content = None

        for file_info in files_created:
            file_path = file_info["relative_path"]

            # LAW 2: Check content_base64 exists before decode
            if "content_base64" not in file_info:
                raise FileRecoveryError(
                    f"Missing content_base64 for {file_path} (session: {session_id})"
                )

            # Decode content
            try:
                content = decode_base64_content(
                    file_info["content_base64"],
                    file_path
                )
            except (ValueError, UnicodeDecodeError) as e:
                raise FileRecoveryError(
                    f"Decode failed for {file_path} (session: {session_id}): {e}"
                ) from e

            # Verify checksum (SHOULD)
            try:
                verify_checksum(content, file_info["checksum"], file_path)
            except ValueError as e:
                logger.warning(f"Checksum mismatch (non-critical): {e}")

            decoded_files.append({
                "path": file_path,
                "content": content,
                "size_bytes": file_info["size_bytes"]
            })

            # Save first markdown as research content
            if file_path.endswith('.md') and not research_content:
                research_content = content

        # 5. Validate research content quality
        if not research_content:
            raise FileRecoveryError(
                f"No markdown content found (session: {session_id})"
            )

        content_validation = validate_research_content(research_content, session_id)

        # Validate that content matches the requested topic
        content_lower = research_content.lower()
        keywords_found = [kw for kw in keywords if kw.lower() in content_lower]

        if len(keywords_found) < 2:
            raise ContentValidationError(
                f"Content does not match topic '{topic}'. "
                f"Expected keywords: {keywords}, found: {keywords_found}"
            )

        logger.info(
            f"  Content validated: {content_validation['word_count']} words, "
            f"{content_validation['line_count']} lines"
        )
        logger.info(f"  Topic keywords found: {keywords_found}")

        # 6. Save files to output directory
        instance_output_dir = OUTPUT_DIR / instance_name / session_id
        instance_output_dir.mkdir(parents=True, exist_ok=True)

        for file_data in decoded_files:
            output_file = instance_output_dir / Path(file_data["path"]).name
            output_file.write_text(file_data["content"], encoding='utf-8')
            logger.info(f"  Saved: {output_file}")

        # Calculate duration
        duration = (datetime.now() - start_time).total_seconds()

        # Success!
        return {
            "instance": instance_name,
            "success": True,
            "session_id": session_id,
            "duration_seconds": duration,
            "files_count": len(decoded_files),
            "files": decoded_files,
            "content_validation": content_validation,
            "output_dir": str(instance_output_dir),
            "topic": topic,
            "keywords_found": keywords_found
        }

    except Exception as e:
        # Log error with session ID if available
        duration = (datetime.now() - start_time).total_seconds()
        logger.error(
            f"❌ Test failed for {instance_name} (session: {session_id or 'unknown'}): "
            f"{type(e).__name__}: {str(e)}"
        )

        return {
            "instance": instance_name,
            "success": False,
            "session_id": session_id,
            "duration_seconds": duration,
            "error": str(e),
            "error_type": type(e).__name__,
            "topic": instance.get("topic", "unknown")
        }


async def run_parallel_tests(
    instances: List[Dict[str, Any]],
    timeout: float
) -> List[Dict[str, Any]]:
    """Run research tests in parallel for all instances - each with its own topic"""
    logger.info(f"🚀 Starting parallel research tests for {len(instances)} instances...")
    logger.info(f"   Timeout: {timeout}s")
    logger.info(f"   Topics:")
    for instance in instances:
        logger.info(f"     - {instance['name']}: {instance['topic']}")
    logger.info("")

    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Run tests in parallel - each instance with its own topic
    tasks = [
        test_research_instance(instance, instance["topic"], timeout)
        for instance in instances
    ]

    results = await asyncio.gather(*tasks)

    return results


def print_summary(results: List[Dict[str, Any]]):
    """Print test summary"""
    success_count = sum(1 for r in results if r["success"])
    total_count = len(results)

    logger.info("")
    logger.info("=" * 70)
    logger.info(f"📊 TEST SUMMARY: {success_count}/{total_count} instances passed")
    logger.info("=" * 70)

    for result in results:
        instance = result["instance"]
        success = result["success"]
        duration = result["duration_seconds"]

        if success:
            validation = result["content_validation"]
            logger.info(f"✅ {instance}")
            logger.info(f"   Topic: {result['topic']}")
            logger.info(f"   Session ID: {result['session_id']}")
            logger.info(f"   Duration: {duration:.1f}s")
            logger.info(f"   Files: {result['files_count']}")
            logger.info(f"   Content: {validation['word_count']} words, {validation['char_count']} chars")
            logger.info(f"   Keywords matched: {', '.join(result['keywords_found'])}")
            logger.info(f"   Output: {result['output_dir']}")
        else:
            logger.error(f"❌ {instance}")
            logger.error(f"   Topic: {result['topic']}")
            logger.error(f"   Session ID: {result.get('session_id', 'unknown')}")
            logger.error(f"   Duration: {duration:.1f}s")
            logger.error(f"   Error: {result['error_type']}: {result['error']}")

        logger.info("")

    logger.info("=" * 70)

    # Exit with error if any test failed
    if success_count < total_count:
        raise SystemExit(1)


async def main():
    """Main test entry point"""
    try:
        results = await run_parallel_tests(
            WRAPPER_INSTANCES,
            TIMEOUT
        )

        print_summary(results)

    except KeyboardInterrupt:
        logger.warning("⚠️  Tests interrupted by user")
        raise SystemExit(130)
    except Exception as e:
        logger.error(f"❌ Test execution failed: {type(e).__name__}: {str(e)}")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
