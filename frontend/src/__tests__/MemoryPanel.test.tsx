/**
 * Tests for the MemoryPanel component.
 *
 * Phase 5C — Search and Frontend Refactoring
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryPanel } from "../MemoryPanel";
import type { Memory } from "../types";

describe("MemoryPanel", () => {
  const mockMemories: Memory[] = [
    {
      id: 1,
      user_id: 1,
      session_id: null,
      content: "User prefers concise answers",
      category: "preference",
      created_at: "2026-07-17T10:00:00Z",
      last_used_at: "2026-07-17T10:30:00Z",
    },
    {
      id: 2,
      user_id: 1,
      session_id: 1,
      content: "Researching machine learning",
      category: "research_interest",
      created_at: "2026-07-16T08:00:00Z",
      last_used_at: "2026-07-16T12:00:00Z",
    },
  ];

  const baseProps = {
    memories: mockMemories,
    memoryEnabled: true,
    memorySettingLoaded: true,
    memoryError: null as string | null,
    togglePending: false,
    addingMemory: false,
    newMemoryContent: "",
    newMemoryCategory: "fact",
    editingMemoryId: null as number | null,
    editMemoryContent: "",
    editMemoryCategory: "fact",
    showClearConfirm: false,
    onToggleMemory: vi.fn(),
    onSetAddingMemory: vi.fn() as (v: boolean) => void,
    onSetNewMemoryContent: vi.fn() as (v: string) => void,
    onSetNewMemoryCategory: vi.fn() as (v: string) => void,
    onAddMemory: vi.fn(),
    onEditMemory: vi.fn() as (id: number) => void,
    onSetEditingMemoryId: vi.fn() as (id: number | null) => void,
    onSetEditMemoryContent: vi.fn() as (v: string) => void,
    onSetEditMemoryCategory: vi.fn() as (v: string) => void,
    onDeleteMemory: vi.fn() as (id: number) => void,
    onClearAllMemories: vi.fn(),
    onSetShowClearConfirm: vi.fn() as (v: boolean) => void,
  };

  // ── Panel Header ───────────────────────────────────────

  it("renders the panel with title and memory count", () => {
    render(<MemoryPanel {...baseProps} />);
    expect(screen.getByText("🧠 Long-Term Memory")).toBeTruthy();
    expect(screen.getByText(/2 saved/)).toBeTruthy();
  });

  it("shows memory enabled status", () => {
    render(<MemoryPanel {...baseProps} />);
    expect(screen.getByText("🟢 Memory enabled")).toBeTruthy();
  });

  it("shows memory disabled status", () => {
    render(<MemoryPanel {...baseProps} memoryEnabled={false} />);
    expect(screen.getByText("🔴 Memory disabled")).toBeTruthy();
  });

  it("shows syncing status when not loaded", () => {
    render(<MemoryPanel {...baseProps} memorySettingLoaded={false} />);
    expect(screen.getByText("⏳ Syncing…")).toBeTruthy();
  });

  // ── Memory List ────────────────────────────────────────

  it("renders all memories with content", () => {
    render(<MemoryPanel {...baseProps} />);
    expect(screen.getByText("User prefers concise answers")).toBeTruthy();
    expect(screen.getByText("Researching machine learning")).toBeTruthy();
  });

  it("renders category labels", () => {
    render(<MemoryPanel {...baseProps} />);
    expect(screen.getByText("Preference")).toBeTruthy();
    expect(screen.getByText("Interest")).toBeTruthy();
  });

  it("shows empty state when there are no memories", () => {
    render(<MemoryPanel {...baseProps} memories={[]} />);
    expect(screen.getByText(/No saved memories yet/)).toBeTruthy();
  });

  // ── Add Memory ─────────────────────────────────────────

  it("shows add form when addingMemory is true", () => {
    render(<MemoryPanel {...baseProps} addingMemory={true} />);
    expect(screen.getByPlaceholderText("What should I remember?")).toBeTruthy();
    expect(screen.getByText("Save")).toBeTruthy();
    expect(screen.getByText("Cancel")).toBeTruthy();
  });

  it("shows Add Memory button when not adding", () => {
    render(<MemoryPanel {...baseProps} />);
    expect(screen.getByText("✚ Add Memory")).toBeTruthy();
  });

  it("calls onAddMemory when Save is clicked", () => {
    const onAddMemory = vi.fn();
    render(<MemoryPanel {...baseProps} addingMemory={true} newMemoryContent="Test" onAddMemory={onAddMemory} />);
    fireEvent.click(screen.getByText("Save"));
    expect(onAddMemory).toHaveBeenCalledOnce();
  });

  it("calls onSetAddingMemory(false) when Cancel is clicked", () => {
    const onSetAddingMemory = vi.fn();
    render(<MemoryPanel {...baseProps} addingMemory={true} onSetAddingMemory={onSetAddingMemory} />);
    fireEvent.click(screen.getByText("Cancel"));
    expect(onSetAddingMemory).toHaveBeenCalledWith(false);
  });

  // ── Edit Memory ────────────────────────────────────────

  it("shows edit form when editingMemoryId is set", () => {
    render(
      <MemoryPanel
        {...baseProps}
        editingMemoryId={1}
        editMemoryContent="Edited content"
        editMemoryCategory="fact"
      />
    );
    expect(screen.getByDisplayValue("Edited content")).toBeTruthy();
  });

  it("calls onDeleteMemory when delete button is clicked", () => {
    const onDeleteMemory = vi.fn();
    render(<MemoryPanel {...baseProps} onDeleteMemory={onDeleteMemory} />);
    const deleteBtns = screen.getAllByTitle("Delete");
    expect(deleteBtns.length).toBeGreaterThan(0);
    fireEvent.click(deleteBtns[0]);
    expect(onDeleteMemory).toHaveBeenCalledWith(1);
  });

  // ── Clear All ──────────────────────────────────────────

  it("shows clear confirm dialog when showClearConfirm is true", () => {
    render(<MemoryPanel {...baseProps} showClearConfirm={true} />);
    expect(screen.getByText("Clear all memories?")).toBeTruthy();
    expect(screen.getByText("Yes")).toBeTruthy();
    expect(screen.getByText("No")).toBeTruthy();
  });

  it("calls onClearAllMemories when Yes is clicked", () => {
    const onClearAllMemories = vi.fn();
    render(<MemoryPanel {...baseProps} showClearConfirm={true} onClearAllMemories={onClearAllMemories} />);
    fireEvent.click(screen.getByText("Yes"));
    expect(onClearAllMemories).toHaveBeenCalledOnce();
  });

  it("hides clear confirm when No is clicked", () => {
    const onSetShowClearConfirm = vi.fn();
    render(<MemoryPanel {...baseProps} showClearConfirm={true} onSetShowClearConfirm={onSetShowClearConfirm} />);
    fireEvent.click(screen.getByText("No"));
    expect(onSetShowClearConfirm).toHaveBeenCalledWith(false);
  });

  // ── Error ──────────────────────────────────────────────

  it("displays memory error when present", () => {
    render(<MemoryPanel {...baseProps} memoryError="Failed to save memory" />);
    expect(screen.getByText("Failed to save memory")).toBeTruthy();
  });
});
