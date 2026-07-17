/**
 * Tests for the CitationPopup component.
 *
 * Phase 5C — Search and Frontend Refactoring
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CitationPopup } from "../CitationPopup";
import type { Citation } from "../types";

describe("CitationPopup", () => {
  const mockCitation: Citation = {
    marker: "[1]",
    document_id: 42,
    filename: "research-paper.pdf",
    page_number: 5,
    chunk_id: 100,
    snippet: "This is a key finding from the research paper.",
  };

  it("renders the citation popup with marker and filename", () => {
    render(<CitationPopup citation={mockCitation} onClose={() => {}} />);
    expect(screen.getByText("[1]")).toBeTruthy();
    expect(screen.getByText("research-paper.pdf")).toBeTruthy();
  });

  it("renders the citation snippet", () => {
    render(<CitationPopup citation={mockCitation} onClose={() => {}} />);
    expect(screen.getByText(/"This is a key finding from the research paper."/)).toBeTruthy();
  });

  it("shows page number when provided", () => {
    render(<CitationPopup citation={mockCitation} onClose={() => {}} />);
    expect(screen.getByText("Page 5")).toBeTruthy();
  });

  it("does not show page number when null", () => {
    const citationNoPage: Citation = { ...mockCitation, page_number: null };
    render(<CitationPopup citation={citationNoPage} onClose={() => {}} />);
    expect(screen.queryByText(/Page/)).toBeNull();
  });

  it("calls onClose when close button is clicked", () => {
    const onClose = vi.fn();
    render(<CitationPopup citation={mockCitation} onClose={onClose} />);
    fireEvent.click(screen.getByText("✕"));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("calls onClose when overlay is clicked (backdrop)", () => {
    const onClose = vi.fn();
    const { container } = render(
      <CitationPopup citation={mockCitation} onClose={onClose} />
    );
    const overlay = container.querySelector(".citation-overlay");
    expect(overlay).toBeTruthy();
    fireEvent.click(overlay!);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does not call onClose when popup content is clicked (stopPropagation)", () => {
    const onClose = vi.fn();
    render(<CitationPopup citation={mockCitation} onClose={onClose} />);
    const popup = document.querySelector(".citation-popup");
    expect(popup).toBeTruthy();
    fireEvent.click(popup!);
    expect(onClose).not.toHaveBeenCalled();
  });
});
