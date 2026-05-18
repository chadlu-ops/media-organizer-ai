import pytest
import hashlib
from pathlib import Path
from media_organizer import md5_hash

def test_md5_hash_empty_file(tmp_path: Path):
    """Test md5_hash with an empty file."""
    file_path = tmp_path / "empty.txt"
    file_path.touch()

    expected_hash = hashlib.md5(b"").hexdigest()
    assert md5_hash(file_path) == expected_hash

def test_md5_hash_small_file(tmp_path: Path):
    """Test md5_hash with a small file."""
    file_path = tmp_path / "small.txt"
    content = b"hello world"
    file_path.write_bytes(content)

    expected_hash = hashlib.md5(content).hexdigest()
    assert md5_hash(file_path) == expected_hash

def test_md5_hash_large_file(tmp_path: Path):
    """Test md5_hash with a file larger than HASH_CHUNK_SIZE (8192 bytes) to ensure chunking works."""
    file_path = tmp_path / "large.txt"
    # Create content larger than 8192 bytes, e.g., 10000 bytes
    content = b"A" * 10000
    file_path.write_bytes(content)

    expected_hash = hashlib.md5(content).hexdigest()
    assert md5_hash(file_path) == expected_hash

def test_md5_hash_file_not_found(tmp_path: Path):
    """Test that md5_hash raises FileNotFoundError for non-existent files."""
    file_path = tmp_path / "non_existent.txt"

    with pytest.raises(FileNotFoundError):
        md5_hash(file_path)
