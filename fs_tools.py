"""
fs_tools.py
-----------
Core file-system tools for the LLM-Powered FileSystem Assistant.

Each tool is a plain Python function that returns a JSON-serializable
dict / list. This makes them easy to expose to an LLM via
function calling / tool use in Part B of the project.

Supported read formats: .txt, .md, .pdf, .docx
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# --- Optional heavy deps: imported lazily so the module still loads
#     even if the user hasn't installed them yet. -----------------

try:
    from pypdf import PdfReader  # type: ignore
    _HAS_PYPDF = True
except ImportError:  # pragma: no cover
    _HAS_PYPDF = False

try:
    import docx  # python-docx  # type: ignore
    _HAS_DOCX = True
except ImportError:  # pragma: no cover
    _HAS_DOCX = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _error(message: str, **extra) -> dict:
    """Standard error envelope returned by every tool."""
    payload = {"success": False, "error": message}
    payload.update(extra)
    return payload


def _file_metadata(path: Path) -> dict:
    """Return name/size/modified/extension metadata for a file path."""
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "extension": path.suffix.lower(),
    }


def _read_txt(path: Path) -> str:
    # utf-8 first, fall back to latin-1 so we never crash on odd bytes
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1", errors="replace")


def _read_pdf(path: Path) -> str:
    if not _HAS_PYPDF:
        raise RuntimeError(
            "pypdf is not installed. Run: pip install pypdf"
        )
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # pragma: no cover - defensive
            pages.append(f"[error extracting page: {exc}]")
    return "\n".join(pages)


def _read_docx(path: Path) -> str:
    if not _HAS_DOCX:
        raise RuntimeError(
            "python-docx is not installed. Run: pip install python-docx"
        )
    document = docx.Document(str(path))
    return "\n".join(p.text for p in document.paragraphs)


# ---------------------------------------------------------------------------
# Tool 1: read_file
# ---------------------------------------------------------------------------

def read_file(filepath: str) -> dict:
    """
    Read a resume / text file (.txt, .md, .pdf, .docx) and return its
    text content plus metadata.

    Returns
    -------
    dict with keys:
        success  : bool
        content  : str          (present on success)
        metadata : dict         (name, size_bytes, modified, extension, char_count)
        error    : str | None   (present on failure)
    """
    if not filepath:
        return _error("filepath is required")

    path = Path(filepath)
    if not path.exists():
        return _error(f"File not found: {filepath}")
    if not path.is_file():
        return _error(f"Not a file: {filepath}")

    ext = path.suffix.lower()
    try:
        if ext in {".txt", ".md", ""}:
            content = _read_txt(path)
        elif ext == ".pdf":
            content = _read_pdf(path)
        elif ext == ".docx":
            content = _read_docx(path)
        else:
            return _error(
                f"Unsupported file type: {ext or '(no extension)'}",
                supported=[".txt", ".md", ".pdf", ".docx"],
            )
    except RuntimeError as exc:
        # Missing optional dependency
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Failed to read file: {exc}")

    metadata = _file_metadata(path)
    metadata["char_count"] = len(content)
    metadata["line_count"] = content.count("\n") + 1 if content else 0

    return {
        "success": True,
        "content": content,
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Tool 2: list_files
# ---------------------------------------------------------------------------

def list_files(directory: str, extension: Optional[str] = None) -> list:
    """
    List files in `directory`, optionally filtering by extension.

    Parameters
    ----------
    directory : str
        Directory to scan (non-recursive).
    extension : str, optional
        Extension filter, e.g. ".pdf" or "pdf". Case-insensitive.

    Returns
    -------
    list of dict. On error a single-element list containing an error
    envelope is returned so the shape is still JSON-friendly.
    """
    if not directory:
        return [_error("directory is required")]

    path = Path(directory)
    if not path.exists():
        return [_error(f"Directory not found: {directory}")]
    if not path.is_dir():
        return [_error(f"Not a directory: {directory}")]

    # Normalize extension filter: accept "pdf", ".PDF", ".pdf", etc.
    ext_filter: Optional[str] = None
    if extension:
        ext_filter = extension.lower()
        if not ext_filter.startswith("."):
            ext_filter = "." + ext_filter

    results: list[dict] = []
    for entry in sorted(path.iterdir()):
        if not entry.is_file():
            continue
        if ext_filter and entry.suffix.lower() != ext_filter:
            continue
        try:
            results.append(_file_metadata(entry))
        except OSError as exc:
            results.append(_error(f"Could not stat {entry.name}: {exc}",
                                  name=entry.name))
    return results


# ---------------------------------------------------------------------------
# Tool 3: write_file
# ---------------------------------------------------------------------------

def write_file(filepath: str, content: str) -> dict:
    """
    Write `content` to `filepath`, creating parent directories as needed.

    Returns
    -------
    dict with keys:
        success  : bool
        path     : str   (absolute path on success)
        bytes_written : int
        error    : str   (on failure)
    """
    if not filepath:
        return _error("filepath is required")
    if content is None:
        return _error("content is required (use empty string for empty file)")

    path = Path(filepath)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Refuse to overwrite a directory with the same name
        if path.exists() and path.is_dir():
            return _error(f"Path is a directory, cannot write file: {filepath}")
        path.write_text(content, encoding="utf-8")
    except Exception as exc:
        return _error(f"Failed to write file: {exc}")

    return {
        "success": True,
        "path": str(path.resolve()),
        "bytes_written": len(content.encode("utf-8")),
    }


# ---------------------------------------------------------------------------
# Tool 4: search_in_file
# ---------------------------------------------------------------------------

def search_in_file(filepath: str, keyword: str, context_chars: int = 60) -> dict:
    """
    Case-insensitive search for `keyword` inside a file. Works on any
    format supported by `read_file` (txt/md/pdf/docx).

    Parameters
    ----------
    filepath : str
    keyword  : str
    context_chars : int
        Number of characters of surrounding context to include with
        each match (default 60 chars on either side).

    Returns
    -------
    dict with keys:
        success       : bool
        keyword       : str
        match_count   : int
        matches       : list of {line, position, context, match}
        error         : str  (on failure)
    """
    if not keyword:
        return _error("keyword is required")

    read_result = read_file(filepath)
    if not read_result.get("success"):
        # Propagate the read error as-is
        return read_result

    content: str = read_result["content"]
    needle = keyword.lower()
    haystack = content.lower()

    matches: list[dict] = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            break

        ctx_start = max(0, idx - context_chars)
        ctx_end = min(len(content), idx + len(keyword) + context_chars)
        # Line number = number of newlines before the match + 1
        line_no = content.count("\n", 0, idx) + 1

        matches.append({
            "line": line_no,
            "position": idx,
            "match": content[idx: idx + len(keyword)],
            "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
        })
        start = idx + len(keyword)  # advance past this match

    return {
        "success": True,
        "keyword": keyword,
        "filepath": filepath,
        "match_count": len(matches),
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# Manual smoke-test entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python fs_tools.py read   <filepath>")
        print("  python fs_tools.py list   <directory> [extension]")
        print("  python fs_tools.py write  <filepath> <content>")
        print("  python fs_tools.py search <filepath> <keyword>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "read":
        out = read_file(sys.argv[2])
    elif cmd == "list":
        ext = sys.argv[3] if len(sys.argv) > 3 else None
        out = list_files(sys.argv[2], ext)
    elif cmd == "write":
        out = write_file(sys.argv[2], sys.argv[3])
    elif cmd == "search":
        out = search_in_file(sys.argv[2], sys.argv[3])
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

    print(json.dumps(out, indent=2, default=str))
