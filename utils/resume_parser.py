"""
Resume text extraction — Parses PDF resumes for profile analysis.
Caches extracted text to avoid re-parsing every time.
"""

import hashlib
from pathlib import Path

from core.private_home import PRIVATE_DIRECTORY_MODE, PrivateHome


CACHE_DIR = PrivateHome.discover().paths.cache / "resume-text"


def extract_resume_text(resume_path: str) -> str:
    """
    Extract text from a PDF resume.
    Returns cached text if the file hasn't changed.
    """
    path = Path(resume_path)
    if not path.exists():
        return ""

    # Cache key based on file content hash
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    home = PrivateHome.discover()
    paths = home.ensure()
    cache_dir = paths.cache / "resume-text"
    cache_dir.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    cache_dir.chmod(PRIVATE_DIRECTORY_MODE)
    cache_file = cache_dir / f"resume_{file_hash}.txt"

    if cache_file.exists():
        return cache_file.read_text()

    try:
        import pdfplumber
    except ImportError:
        print("  ⚠ pdfplumber not installed. Run: pip install pdfplumber")
        return ""

    text = ""
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        print(f"  ⚠ Failed to parse resume: {e}")
        return ""

    # Cache the result
    home.write_text(cache_file, text)
    return text
