"""
Unit Tests for FileDiscoveryService

Tests file discovery functionality including:
- SDK message parsing for Write tool calls
- Directory scanning for new files
- File metadata creation
- Error handling and LAW 1 compliance

Version: 1.0
Created: 2025-10-27
"""

import pytest
from pathlib import Path
from datetime import datetime, timedelta
import tempfile
import hashlib
from unittest.mock import Mock

from src.file_discovery import (
    FileDiscoveryService,
    FileMetadata,
    SDKMessageParsingError,
    DirectoryScanError,
    FileMetadataError,
    ChecksumCalculationError
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_wrapper_root(tmp_path):
    """Create temporary wrapper root directory."""
    claudedocs_dir = tmp_path / "claudedocs"
    claudedocs_dir.mkdir()
    return tmp_path


@pytest.fixture
def file_discovery_service(temp_wrapper_root):
    """Create FileDiscoveryService instance."""
    return FileDiscoveryService(temp_wrapper_root)


@pytest.fixture
def sample_file(temp_wrapper_root):
    """Create sample file for testing."""
    file_path = temp_wrapper_root / "claudedocs" / "test_research.md"
    file_path.write_text("# Test Research\n\nSample content")
    return file_path


# ============================================================================
# Test: Initialization
# ============================================================================

def test_init_valid_wrapper_root(temp_wrapper_root):
    """FileDiscoveryService should initialize with valid wrapper root."""
    service = FileDiscoveryService(temp_wrapper_root)

    assert service.wrapper_root == temp_wrapper_root
    assert service.claudedocs_dir == temp_wrapper_root / "claudedocs"


def test_init_invalid_wrapper_root_not_exists():
    """FileDiscoveryService should raise ValueError if wrapper root doesn't exist."""
    invalid_path = Path("/nonexistent/path/to/wrapper")

    with pytest.raises(ValueError, match="Wrapper root does not exist"):
        FileDiscoveryService(invalid_path)


def test_init_invalid_wrapper_root_not_directory(tmp_path):
    """FileDiscoveryService should raise ValueError if wrapper root is not a directory."""
    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory")

    with pytest.raises(ValueError, match="Wrapper root is not a directory"):
        FileDiscoveryService(file_path)


# ============================================================================
# Test: File Metadata Creation
# ============================================================================

def test_create_file_metadata_success(file_discovery_service, sample_file):
    """_create_file_metadata should create valid FileMetadata."""
    metadata = file_discovery_service._create_file_metadata(sample_file)

    assert isinstance(metadata, FileMetadata)
    assert metadata.path == str(sample_file.absolute())
    assert metadata.relative_path == "claudedocs/test_research.md"
    assert metadata.size_bytes > 0
    assert metadata.mime_type == "text/markdown"
    assert metadata.checksum.startswith("sha256:")

    # Validate ISO timestamp
    datetime.fromisoformat(metadata.created_at)


def test_create_file_metadata_file_not_exists(file_discovery_service, temp_wrapper_root):
    """_create_file_metadata should raise FileMetadataError if file doesn't exist."""
    nonexistent = temp_wrapper_root / "nonexistent.md"

    with pytest.raises(FileMetadataError, match="File does not exist"):
        file_discovery_service._create_file_metadata(nonexistent)


def test_checksum_calculation_success(file_discovery_service, sample_file):
    """_calculate_checksum should calculate SHA256 checksum."""
    checksum = file_discovery_service._calculate_checksum(sample_file)

    assert checksum.startswith("sha256:")

    # Verify checksum is correct
    sha256 = hashlib.sha256()
    with open(sample_file, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)

    expected = f"sha256:{sha256.hexdigest()}"
    assert checksum == expected


def test_checksum_calculation_file_not_readable(file_discovery_service, temp_wrapper_root):
    """_calculate_checksum should raise ChecksumCalculationError if file can't be read."""
    nonexistent = temp_wrapper_root / "nonexistent.md"

    with pytest.raises(ChecksumCalculationError, match="Failed to read file for checksum"):
        file_discovery_service._calculate_checksum(nonexistent)


# ============================================================================
# Test: SDK Message Parsing
# ============================================================================

def test_discover_from_sdk_messages_success(file_discovery_service, sample_file):
    """discover_files_from_sdk_messages should find files from Write tool calls."""
    session_start = datetime.now() - timedelta(minutes=1)

    # Mock SDK message with Write tool call
    mock_block = Mock()
    mock_block.name = "Write"
    mock_block.input = {"file_path": str(sample_file)}

    mock_message = Mock()
    mock_message.content = [mock_block]

    sdk_messages = [mock_message]

    files = file_discovery_service.discover_files_from_sdk_messages(
        sdk_messages=sdk_messages,
        session_start=session_start
    )

    assert len(files) == 1
    assert files[0].relative_path == "claudedocs/test_research.md"


def test_discover_from_sdk_messages_empty_list(file_discovery_service):
    """discover_files_from_sdk_messages should return empty list for empty messages."""
    session_start = datetime.now()

    files = file_discovery_service.discover_files_from_sdk_messages(
        sdk_messages=[],
        session_start=session_start
    )

    assert files == []


def test_discover_from_sdk_messages_none_input(file_discovery_service):
    """discover_files_from_sdk_messages should raise ValueError for None input."""
    with pytest.raises(ValueError, match="sdk_messages cannot be None"):
        file_discovery_service.discover_files_from_sdk_messages(
            sdk_messages=None,
            session_start=datetime.now()
        )


def test_discover_from_sdk_messages_file_predates_session(file_discovery_service, sample_file):
    """discover_files_from_sdk_messages should skip files created before session start."""
    # Session started AFTER file was created
    session_start = datetime.now() + timedelta(minutes=1)

    mock_block = Mock()
    mock_block.name = "Write"
    mock_block.input = {"file_path": str(sample_file)}

    mock_message = Mock()
    mock_message.content = [mock_block]

    files = file_discovery_service.discover_files_from_sdk_messages(
        sdk_messages=[mock_message],
        session_start=session_start
    )

    # File should be skipped (created before session)
    assert len(files) == 0


# ============================================================================
# Test: Directory Scanning
# ============================================================================

def test_discover_from_directory_scan_success(file_discovery_service, sample_file):
    """discover_files_from_directory_scan should find new files."""
    session_start = datetime.now() - timedelta(minutes=1)
    claudedocs_dir = file_discovery_service.wrapper_root / "claudedocs"

    files = file_discovery_service.discover_files_from_directory_scan(
        directories=[claudedocs_dir],
        session_start=session_start,
        file_patterns=["*.md"]
    )

    assert len(files) == 1
    assert files[0].relative_path == "claudedocs/test_research.md"


def test_discover_from_directory_scan_no_match(file_discovery_service, sample_file):
    """discover_files_from_directory_scan should return empty for no matching files."""
    session_start = datetime.now() - timedelta(minutes=1)
    claudedocs_dir = file_discovery_service.wrapper_root / "claudedocs"

    # Search for .json files (none exist)
    files = file_discovery_service.discover_files_from_directory_scan(
        directories=[claudedocs_dir],
        session_start=session_start,
        file_patterns=["*.json"]
    )

    assert len(files) == 0


def test_discover_from_directory_scan_empty_directories(file_discovery_service):
    """discover_files_from_directory_scan should raise ValueError for empty directories."""
    with pytest.raises(ValueError, match="directories list cannot be empty"):
        file_discovery_service.discover_files_from_directory_scan(
            directories=[],
            session_start=datetime.now()
        )


def test_discover_from_directory_scan_all_fail(file_discovery_service):
    """discover_files_from_directory_scan should raise DirectoryScanError if all dirs fail."""
    nonexistent = Path("/nonexistent/dir")

    with pytest.raises(DirectoryScanError, match="All .* directories failed to scan"):
        file_discovery_service.discover_files_from_directory_scan(
            directories=[nonexistent],
            session_start=datetime.now()
        )


# ============================================================================
# Test: FileMetadata to_dict
# ============================================================================

def test_file_metadata_to_dict(file_discovery_service, sample_file):
    """FileMetadata.to_dict() should convert to dict."""
    metadata = file_discovery_service._create_file_metadata(sample_file)
    result = metadata.to_dict()

    assert isinstance(result, dict)
    assert result["path"] == str(sample_file.absolute())
    assert result["relative_path"] == "claudedocs/test_research.md"
    assert result["size_bytes"] > 0
    assert result["mime_type"] == "text/markdown"
    assert result["checksum"].startswith("sha256:")
    assert "created_at" in result


# ============================================================================
# Manual Run Instructions
# ============================================================================

"""
===========================================
MANUAL RUN INSTRUCTIONS
===========================================

1. Activate virtual environment:
   cd /Users/lorenz/ECO/projects/eco-openai-wrapper
   source .venv/bin/activate

2. Run all file discovery tests:
   pytest tests/unit/test_file_discovery.py -v

3. Run specific test:
   pytest tests/unit/test_file_discovery.py::test_discover_from_sdk_messages_success -v

4. Run with coverage:
   pytest tests/unit/test_file_discovery.py --cov=file_discovery --cov-report=term-missing

===========================================
"""
