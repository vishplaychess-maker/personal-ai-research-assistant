"""
Document text extraction and chunking service.

Supports:
  - TXT files (plain text)
  - PDF files (pypdf with page numbers)
"""

import zipfile
from pathlib import Path
from typing import List, Dict, Optional


# ── Configuration ──────────────────────────────────────────

CHUNK_SIZE = 800       # characters per chunk
CHUNK_OVERLAP = 120    # overlapping characters between chunks


# ── Text extraction ────────────────────────────────────────


def extract_text_from_txt(file_path: str) -> List[Dict]:
    """Extract text from a plain text file."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not text.strip():
        raise ValueError("File is empty")
    return [{"text": text, "page_number": None}]


def extract_text_from_pdf(file_path: str) -> List[Dict]:
    """Extract text per-page from a PDF file using pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf is not installed. Add it to requirements.txt.")

    reader = PdfReader(file_path)
    if len(reader.pages) == 0:
        raise ValueError("PDF file has no pages")

    pages: List[Dict] = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text and text.strip():
            pages.append({"text": text.strip(), "page_number": i + 1})

    if not pages:
        raise ValueError("Could not extract any text from PDF")

    return pages


def _looks_like_pdf(file_path: str, content_type: str) -> bool:
    """Identify a PDF from its MIME type, extension, or magic bytes.

    Browsers and Electron drag-drop do not always send ``application/pdf``;
    some send ``application/octet-stream`` or an empty type. Falling back to
    plain-text extraction for those uploads indexed binary PDF bytes as
    "ready" text, so the model received garbage instead of the document.
    """
    if content_type == "application/pdf":
        return True
    if Path(file_path).suffix.lower() == ".pdf":
        return True
    try:
        with open(file_path, "rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def _looks_like_docx(file_path: str, content_type: str) -> bool:
    if content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return True
    if Path(file_path).suffix.lower() == ".docx":
        return True
    try:
        return zipfile.is_zipfile(file_path) and Path(file_path).suffix.lower() == ".docx"
    except OSError:
        return False


def extract_text(file_path: str, content_type: str) -> List[Dict]:
    """
    Extract text from a file.

    Args:
        file_path: Absolute path to the uploaded file.
        content_type: MIME type (text/plain, application/pdf, or code files).

    Returns:
        List of dicts with 'text' and 'page_number' keys.

    Raises:
        ValueError: If extraction fails or content_type unsupported.
    """
    if _looks_like_pdf(file_path, content_type):
        return extract_text_from_pdf(file_path)
    elif _looks_like_docx(file_path, content_type):
        try:
            from docx import Document as DocxDocument
            doc = DocxDocument(file_path)
            text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
            if not text.strip():
                raise ValueError("DOCX is empty")
            return [{"text": text, "page_number": None}]
        except ImportError as exc:
            raise RuntimeError(
                "python-docx is not installed. Add it to requirements.txt."
            ) from exc
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not extract text from DOCX: {type(exc).__name__}") from exc
    elif content_type == "text/plain" or content_type.startswith("text/") or content_type in ("application/json", "application/x-yaml", "application/xml", "application/x-yaml", "application/xml", "application/x-toml") or content_type in ("text/markdown", "application/msword"):
        return extract_text_from_txt(file_path)
    else:
        # Fallback: try as plain text for code/docs files (.md, .py, etc.)
        try:
            return extract_text_from_txt(file_path)
        except Exception:
            raise ValueError(f"Unsupported content type: {content_type}")


# ── Chunking ────────────────────────────────────────────────


def chunk_text(
    pages: List[Dict],
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> List[Dict]:
    """
    Split extracted pages into overlapping chunks.

    Args:
        pages: List of dicts with 'text' and 'page_number'.
        chunk_size: Maximum characters per chunk.
        overlap: Overlapping characters between consecutive chunks.

    Returns:
        List of dicts with 'text', 'page_number', and 'chunk_index'.
    """
    chunks: List[Dict] = []
    chunk_index = 0

    for page in pages:
        text = page["text"]
        page_number = page.get("page_number")

        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))

            # Try to break at a sentence or word boundary
            if end < len(text):
                # Look for sentence end backwards
                for sep in (". ", "! ", "? ", "\n\n", "\n"):
                    idx = text.rfind(sep, start + max(1, chunk_size - 100), end)
                    if idx > start:
                        end = idx + len(sep)
                        break
                else:
                    # Look for word boundary
                    idx = text.rfind(" ", start + max(1, chunk_size - 50), end)
                    if idx > start:
                        end = idx

            chunk_content = text[start:end].strip()
            if chunk_content:
                chunks.append({
                    "text": chunk_content,
                    "page_number": page_number,
                    "chunk_index": chunk_index,
                })
                chunk_index += 1

            start = end - overlap if end < len(text) else len(text)

    if not chunks:
        raise ValueError("No chunks could be created from the extracted text")

    return chunks
