/**
 * Tests for the DocumentPanel component.
 *
 * Phase 5C — Search and Frontend Refactoring
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { DocumentPanel } from "../DocumentPanel";
import type { Document } from "../types";

describe("DocumentPanel", () => {
  const mockDocuments: Document[] = [
    {
      id: 1,
      session_id: 1,
      filename: "research.pdf",
      content_type: "application/pdf",
      file_size: 102400,
      status: "ready",
      chunk_count: 5,
      error_message: null,
      created_at: "2026-07-17T10:00:00Z",
    },
    {
      id: 2,
      session_id: 1,
      filename: "notes.txt",
      content_type: "text/plain",
      file_size: 2048,
      status: "processing",
      chunk_count: 0,
      error_message: null,
      created_at: "2026-07-17T11:00:00Z",
    },
    {
      id: 3,
      session_id: 1,
      filename: "broken.pdf",
      content_type: "application/pdf",
      file_size: 500,
      status: "failed",
      chunk_count: 0,
      error_message: "Invalid PDF format",
      created_at: "2026-07-17T09:00:00Z",
    },
  ];

  const baseProps = {
    documents: mockDocuments,
    uploading: false,
    uploadError: null as string | null,
    readyDocs: 1,
    onFileSelect: vi.fn() as (e: React.ChangeEvent<HTMLInputElement>) => void,
    onDeleteDocument: vi.fn() as (id: number) => void,
  };

  // ── Panel Header ───────────────────────────────────────

  it("renders the panel with title and document counts", () => {
    render(<DocumentPanel {...baseProps} />);
    expect(screen.getByText("📄 Documents")).toBeTruthy();
    expect(screen.getByText(/1 ready/)).toBeTruthy();
    expect(screen.getByText(/1 processing/)).toBeTruthy();
  });

  // ── Upload ─────────────────────────────────────────────

  it("renders the upload button", () => {
    render(<DocumentPanel {...baseProps} />);
    expect(screen.getByText("📤 Upload PDF/TXT")).toBeTruthy();
  });

  it("shows uploading state", () => {
    render(<DocumentPanel {...baseProps} uploading={true} />);
    expect(screen.getByText("⏳ Uploading…")).toBeTruthy();
  });

  it("shows upload hint", () => {
    render(<DocumentPanel {...baseProps} />);
    expect(screen.getByText("Max 20 MB")).toBeTruthy();
  });

  // ── Document List ──────────────────────────────────────

  it("renders all documents with filenames", () => {
    render(<DocumentPanel {...baseProps} />);
    expect(screen.getByText("research.pdf")).toBeTruthy();
    expect(screen.getByText("notes.txt")).toBeTruthy();
    expect(screen.getByText("broken.pdf")).toBeTruthy();
  });

  it("shows empty state when there are no documents", () => {
    render(<DocumentPanel {...baseProps} documents={[]} />);
    expect(screen.getByText("No documents uploaded yet")).toBeTruthy();
  });

  it("shows status for ready documents", () => {
    render(<DocumentPanel {...baseProps} />);
    // Text includes file size appended (" · 100.0 KB"), so use partial match
    expect(screen.getByText((content) => content.includes("✅ 5 chunks"))).toBeTruthy();
  });

  it("shows status for processing documents", () => {
    render(<DocumentPanel {...baseProps} />);
    expect(screen.getByText((content) => content.includes("⏳ Processing…"))).toBeTruthy();
  });

  it("shows status for failed documents with error message", () => {
    render(<DocumentPanel {...baseProps} />);
    expect(screen.getByText((content) => content.includes("❌ Failed: Invalid PDF format"))).toBeTruthy();
  });

  it("shows file size for documents", () => {
    render(<DocumentPanel {...baseProps} />);
    expect(screen.getByText((content) => content.includes("100.0 KB"))).toBeTruthy();
  });

  // ── Delete ─────────────────────────────────────────────

  it("calls onDeleteDocument when delete button is clicked", () => {
    const onDeleteDocument = vi.fn();
    render(<DocumentPanel {...baseProps} onDeleteDocument={onDeleteDocument} />);
    const deleteBtns = screen.getAllByTitle("Delete document");
    expect(deleteBtns.length).toBeGreaterThan(0);
    fireEvent.click(deleteBtns[0]);
    expect(onDeleteDocument).toHaveBeenCalledWith(1);
  });

  // ── Upload Error ───────────────────────────────────────

  it("displays upload error when present", () => {
    render(<DocumentPanel {...baseProps} uploadError="File too large" />);
    expect(screen.getByText("File too large")).toBeTruthy();
  });
});
