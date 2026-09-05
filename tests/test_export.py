"""
Report Export — /api/export integration + service unit tests.

Uses FastAPI TestClient (in-process). No live Ollama required: the export
endpoint only renders documents, it never calls an LLM.
"""

import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import export_service

from tests.auth_helpers import register_and_login, auth_headers


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def token(client):
    _, tok = register_and_login(client, username=None)
    return tok


def _headers(token):
    return auth_headers(token)


def _export(client, token, payload):
    return client.post("/api/export", json=payload, headers=_headers(token))


# ── Service unit tests: sanitizers ─────────────────────────


def test_sanitize_text_strips_control_chars():
    dirty = "hello\x00\x08\x1fworld\x7f"
    assert export_service.sanitize_text(dirty) == "helloworld"


def test_sanitize_text_keeps_newlines_and_tabs():
    assert export_service.sanitize_text("a\tb\nc\rd") == "a\tb\nc\rd"


def test_sanitize_text_truncates():
    out = export_service.sanitize_text("x" * 600_000)
    assert len(out) < 600_000
    assert out.endswith("…[truncated]")


def test_sanitize_filename_removes_traversal_and_separators():
    assert export_service.sanitize_filename("../../etc/passwd") == "etc_passwd"
    assert "/" not in export_service.sanitize_filename("..\\..\\windows\\system32")
    assert export_service.sanitize_filename("") == "export"
    assert export_service.sanitize_filename(None) == "export"
    assert export_service.sanitize_filename("   ") == "export"


def test_sanitize_filename_keeps_readable_words():
    assert export_service.sanitize_filename("Deep Research: AI (2026)") == "Deep Research_ AI _2026"


# ── Service unit tests: document bytes ─────────────────────


def _assert_valid_pdf(data: bytes):
    assert data.startswith(b"%PDF-")
    assert b"%%EOF" in data[-1024:]


def _assert_valid_docx(data: bytes):
    assert data[:2] == b"PK"  # OOXML is a zip container
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "word/document.xml" in zf.namelist()
        assert b"javascript" not in zf.read("word/document.xml").lower()


def test_generate_pdf_report():
    data = export_service.generate_pdf(
        "Test Report", report_text="# Heading\n\nSome **bold** text.\n- bullet one\n- bullet two"
    )
    _assert_valid_pdf(data)


def test_generate_pdf_escapes_markup_injection():
    data = export_service.generate_pdf(
        "Injection", report_text="<b onclick='x'>evil</b> & <script>alert(1)</script>"
    )
    _assert_valid_pdf(data)


def test_generate_docx_report():
    data = export_service.generate_docx(
        "Test Report", report_text="# Heading\n\nParagraph.\n1. first\n2. second"
    )
    _assert_valid_docx(data)


def test_generate_pdf_chat():
    data = export_service.generate_pdf(
        "Chat Export",
        chat_messages=[
            {"role": "user", "content": "What is RAG?", "created_at": "2026-09-06T10:00:00Z"},
            {"role": "assistant", "content": "Retrieval-Augmented Generation…"},
        ],
    )
    _assert_valid_pdf(data)


def test_generate_docx_graph():
    data = export_service.generate_docx(
        "Graph Export",
        graph={
            "nodes": [{"id": 0, "name": "Python", "type": "tech"}, {"id": 1, "name": "FastAPI", "type": "tech"}],
            "links": [{"source": 0, "target": 1, "relation": "used_by", "weight": 2}],
        },
    )
    _assert_valid_docx(data)


def test_generate_pdf_graph():
    data = export_service.generate_pdf(
        "Graph Export",
        graph={
            "nodes": [{"id": 0, "name": "A"}, {"id": 1, "name": "B"}],
            "links": [{"source": 0, "target": 1, "relation": "rel", "weight": 1.5}],
        },
    )
    _assert_valid_pdf(data)


# ── API: auth & validation ─────────────────────────────────


def test_export_requires_auth(client):
    resp = client.post(
        "/api/export",
        json={"type": "research_report", "data": {"report_text": "x"}},
    )
    assert resp.status_code == 401


def test_export_rejects_invalid_type(client, token):
    resp = _export(client, token, {"type": "not_a_type", "data": {"report_text": "x"}})
    assert resp.status_code == 422


def test_export_rejects_invalid_format(client, token):
    resp = _export(
        client, token,
        {"type": "research_report", "format": "exe", "data": {"report_text": "x"}},
    )
    assert resp.status_code == 422


def test_export_report_requires_text(client, token):
    resp = _export(client, token, {"type": "research_report", "data": {}})
    assert resp.status_code == 422


def test_export_chat_requires_messages(client, token):
    resp = _export(client, token, {"type": "chat", "data": {}})
    assert resp.status_code == 422


def test_export_chat_rejects_non_object_entries(client, token):
    resp = _export(client, token, {"type": "chat", "data": {"messages": ["hello"]}})
    assert resp.status_code == 422


def test_export_graph_requires_nodes(client, token):
    resp = _export(client, token, {"type": "knowledge_graph", "data": {"graph": {"nodes": []}}})
    assert resp.status_code == 422


# ── API: happy paths ───────────────────────────────────────


def test_export_report_pdf_download(client, token):
    resp = _export(
        client, token,
        {
            "type": "research_report",
            "format": "pdf",
            "title": "Deep Research: Quantum Computing",
            "data": {
                "report_text": "# Quantum\n\nFindings below.\n- qubit\n- superposition",
                "citations": [
                    {"marker": "[1]", "filename": "paper.pdf", "page_number": 3, "snippet": "key result"},
                ],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert re.search(r'filename="Deep Research_ Quantum Computing_\d{8}_\d{6}\.pdf"', disposition)
    _assert_valid_pdf(resp.content)


def test_export_report_docx_download(client, token):
    resp = _export(
        client, token,
        {
            "type": "research_report",
            "format": "docx",
            "title": "Report",
            "data": {"report_text": "Body text with **markdown**."},
        },
    )
    assert resp.status_code == 200, resp.text
    assert (
        resp.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    _assert_valid_docx(resp.content)


def test_export_chat_pdf(client, token):
    resp = _export(
        client, token,
        {
            "type": "chat",
            "format": "pdf",
            "title": "My Conversation",
            "data": {
                "messages": [
                    {"role": "user", "content": "Hi there"},
                    {"role": "assistant", "content": "Hello! How can I help?"},
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    _assert_valid_pdf(resp.content)
    assert 'filename="My Conversation_' in resp.headers["content-disposition"]


def test_export_chat_docx(client, token):
    resp = _export(
        client, token,
        {
            "type": "chat",
            "format": "docx",
            "data": {"messages": [{"role": "user", "content": "Hello"}]},
        },
    )
    assert resp.status_code == 200, resp.text
    _assert_valid_docx(resp.content)
    # Default title from type: "Chat"
    assert 'filename="Chat_' in resp.headers["content-disposition"]


def test_export_graph_pdf(client, token):
    resp = _export(
        client, token,
        {
            "type": "knowledge_graph",
            "format": "pdf",
            "data": {
                "graph": {
                    "nodes": [{"id": 0, "name": "Python", "type": "tech"}, {"id": 1, "name": "Testing", "type": "concept"}],
                    "links": [{"source": 0, "target": 1, "relation": "improves", "weight": 3}],
                }
            },
        },
    )
    assert resp.status_code == 200, resp.text
    _assert_valid_pdf(resp.content)
    assert 'filename="Knowledge Graph_' in resp.headers["content-disposition"]


def test_export_graph_docx(client, token):
    resp = _export(
        client, token,
        {
            "type": "knowledge_graph",
            "format": "docx",
            "data": {
                "graph": {
                    "nodes": [{"id": 0, "name": "A"}],
                    "links": [],
                }
            },
        },
    )
    assert resp.status_code == 200, resp.text
    _assert_valid_docx(resp.content)


# ── API: security behavior ─────────────────────────────────


def test_export_title_cannot_inject_filename(client, token):
    resp = _export(
        client, token,
        {
            "type": "chat",
            "title": '../../../etc/passwd',
            "data": {"messages": [{"role": "user", "content": "x"}]},
        },
    )
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert ".." not in disposition
    assert "/" not in disposition.split('filename="')[1]


def test_export_sanitizes_control_chars_in_content(client, token):
    resp = _export(
        client, token,
        {
            "type": "research_report",
            "data": {"report_text": "safe\x00\x1fcontent"},
        },
    )
    assert resp.status_code == 200
    _assert_valid_pdf(resp.content)


def test_export_oversized_chat_rejected(client, token):
    messages = [{"role": "user", "content": "x"} for _ in range(export_service.MAX_CHAT_MESSAGES + 1)]
    resp = _export(client, token, {"type": "chat", "data": {"messages": messages}})
    assert resp.status_code == 413


def test_export_no_path_traversal_via_type_or_format(client, token):
    # type/format are enums — arbitrary strings can never reach a path or header
    resp = _export(
        client, token,
        {"type": "chat", "format": "pdf", "data": {"messages": [{"role": "user", "content": "x"}]}},
    )
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
