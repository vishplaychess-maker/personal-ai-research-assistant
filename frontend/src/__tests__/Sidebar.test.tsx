/**
 * Tests for the Sidebar component.
 *
 * Phase 5C — Search and Frontend Refactoring
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { Sidebar } from "../Sidebar";
import type { Session, HealthStatus } from "../types";

describe("Sidebar", () => {
  const mockSessions: Session[] = [
    {
      id: 1,
      title: "Test Session 1",
      user_id: 1,
      model: null,
      system_prompt: null,
      created_at: "2026-07-17T10:00:00Z",
      updated_at: "2026-07-17T10:30:00Z",
    },
    {
      id: 2,
      title: "Research on AI",
      user_id: 1,
      model: "llama3.2:1b",
      system_prompt: null,
      created_at: "2026-07-16T08:00:00Z",
      updated_at: "2026-07-16T12:00:00Z",
    },
  ];

  const mockHealth: HealthStatus = {
    backend: "ok",
    chromadb: "ok",
    ollama: "ok",
  };

  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function renderSidebar(props: Partial<Parameters<typeof Sidebar>[0]> = {}) {
    return render(
      <Sidebar
        sessions={props.sessions ?? mockSessions}
        activeSessionId={props.activeSessionId ?? null}
        loadingSessions={props.loadingSessions ?? false}
        health={props.health ?? mockHealth}
        onSelectSession={props.onSelectSession ?? vi.fn()}
        onCreateSession={props.onCreateSession ?? vi.fn()}
        onDeleteSession={props.onDeleteSession ?? vi.fn()}
        onRenameSession={props.onRenameSession ?? vi.fn()}
      />
    );
  }

  // ── Session List ───────────────────────────────────────

  it("renders the session list with titles", () => {
    renderSidebar();
    expect(screen.getByText("Test Session 1")).toBeTruthy();
    expect(screen.getByText("Research on AI")).toBeTruthy();
  });

  it("renders the sidebar header", () => {
    renderSidebar();
    expect(screen.getByText("Research Sessions")).toBeTruthy();
    expect(screen.getByText("✚ New")).toBeTruthy();
  });

  it("shows loading state when sessions are loading", () => {
    renderSidebar({ loadingSessions: true, sessions: [] });
    expect(screen.getByText("Loading…")).toBeTruthy();
  });

  it("shows empty state when there are no sessions", () => {
    renderSidebar({ sessions: [] });
    expect(screen.getByText("No sessions yet")).toBeTruthy();
  });

  it("calls onCreateSession when New button is clicked", () => {
    const onCreateSession = vi.fn();
    renderSidebar({ onCreateSession });
    fireEvent.click(screen.getByText("✚ New"));
    expect(onCreateSession).toHaveBeenCalledOnce();
  });

  it("calls onSelectSession when a session is clicked", () => {
    const onSelectSession = vi.fn();
    renderSidebar({ onSelectSession });
    fireEvent.click(screen.getByText("Test Session 1"));
    expect(onSelectSession).toHaveBeenCalledWith(1);
  });

  it("highlights the active session", () => {
    const { container } = renderSidebar({ activeSessionId: 1 });
    const items = container.querySelectorAll(".sidebar-item");
    expect(items.length).toBeGreaterThan(0);
    // First sidebar item should be the active one (session id 1)
    expect(items[0].classList.contains("active")).toBe(true);
  });

  it("calls onDeleteSession when delete button is clicked", () => {
    const onDeleteSession = vi.fn();
    renderSidebar({ onDeleteSession });
    const deleteBtns = screen.getAllByTitle("Delete");
    expect(deleteBtns.length).toBeGreaterThan(0);
    fireEvent.click(deleteBtns[0]);
    expect(onDeleteSession).toHaveBeenCalledWith(1);
  });

  it("shows rename input when rename button is clicked", () => {
    renderSidebar();
    const renameBtns = screen.getAllByTitle("Rename");
    fireEvent.click(renameBtns[0]);
    // Should show an input field
    const renameInput = document.querySelector(".rename-input");
    expect(renameInput).toBeTruthy();
  });

  it("calls onRenameSession when rename is submitted", async () => {
    const onRenameSession = vi.fn();
    renderSidebar({ onRenameSession });
    const renameBtns = screen.getAllByTitle("Rename");
    await act(async () => {
      fireEvent.click(renameBtns[0]);
      await new Promise((r) => setTimeout(r, 20));
    });
    const renameInput = document.querySelector(".rename-input") as HTMLInputElement;
    expect(renameInput).toBeTruthy();
    // Change the value
    await act(async () => {
      fireEvent.change(renameInput, { target: { value: "Renamed Session" } });
    });
    // Press Enter to submit
    await act(async () => {
      fireEvent.keyDown(renameInput, { key: "Enter" });
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(onRenameSession).toHaveBeenCalledWith(1, "Renamed Session");
  });

  it("shows health indicators", () => {
    renderSidebar();
    expect(screen.getByText("API")).toBeTruthy();
    expect(screen.getByText("LLM")).toBeTruthy();
    expect(screen.getByText("All OK")).toBeTruthy();
  });

  it("shows health count when not all services are ok", () => {
    const partialHealth: HealthStatus = {
      backend: "ok",
      chromadb: "error",
      ollama: "ok",
    };
    renderSidebar({ health: partialHealth });
    expect(screen.getByText("2/3")).toBeTruthy();
  });

  // ── Search ─────────────────────────────────────────────

  it("renders the search input", () => {
    renderSidebar();
    expect(screen.getByPlaceholderText("Search conversations…")).toBeTruthy();
  });

  it("shows clear button when search query is typed", async () => {
    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations…");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      await new Promise((r) => setTimeout(r, 20));
    });
    // Clear button should be visible
    const clearBtn = document.querySelector(".sidebar-search-clear");
    expect(clearBtn).toBeTruthy();
  });

  it("clears search when clear button is clicked", async () => {
    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations…");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      await new Promise((r) => setTimeout(r, 20));
    });
    const clearBtn = document.querySelector(".sidebar-search-clear") as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(clearBtn);
      await new Promise((r) => setTimeout(r, 20));
    });
    // Search should be cleared
    expect(screen.getByPlaceholderText("Search conversations…")).toBeTruthy();
  });

  it("shows empty state when search returns no results", async () => {
    // Mock fetch to return empty results
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );

    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations…");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "xyznonexistent" } });
      await new Promise((r) => setTimeout(r, 400));
    });

    expect(screen.getByText("No results found")).toBeTruthy();
  });

  it("shows search results when search returns matches", async () => {
    const mockResults = [
      {
        session_id: 1,
        session_title: "Test Session",
        message_id: 42,
        role: "user",
        content: "Tell me about AI",
        snippet: "Tell me about AI",
        created_at: "2026-07-17T10:00:00Z",
      },
    ];

    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify(mockResults), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );

    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations…");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      await new Promise((r) => setTimeout(r, 400));
    });

    expect(screen.getByText("Test Session")).toBeTruthy();
    expect(screen.getByText("Tell me about AI")).toBeTruthy();
  });

  it("navigates to session when search result is clicked", async () => {
    const mockResults = [
      {
        session_id: 2,
        session_title: "AI Research",
        message_id: 10,
        role: "assistant",
        content: "AI is fascinating",
        snippet: "AI is fascinating",
        created_at: "2026-07-17T10:00:00Z",
      },
    ];

    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify(mockResults), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );

    const onSelectSession = vi.fn();
    renderSidebar({ onSelectSession });

    const searchInput = screen.getByPlaceholderText("Search conversations…");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      await new Promise((r) => setTimeout(r, 400));
    });

    // Click the search result
    const resultTitle = screen.getByText("AI Research");
    await act(async () => {
      fireEvent.click(resultTitle);
      await new Promise((r) => setTimeout(r, 20));
    });

    expect(onSelectSession).toHaveBeenCalledWith(2);
  });

  it("shows searching state while search is in progress", async () => {
    // Create a fetch that never resolves (simulating network delay)
    vi.mocked(fetch).mockImplementation(() => new Promise(() => {}));

    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations…");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      await new Promise((r) => setTimeout(r, 20));
    });

    // Wait for debounce to trigger
    await act(async () => {
      await new Promise((r) => setTimeout(r, 350));
    });

    // Should show searching indicator
    const spinner = document.querySelector(".sidebar-search-spinner");
    expect(spinner).toBeTruthy();
  });

  it("clears search on Escape key", async () => {
    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations…");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      await new Promise((r) => setTimeout(r, 20));
    });
    // Press Escape
    await act(async () => {
      fireEvent.keyDown(searchInput, { key: "Escape" });
      await new Promise((r) => setTimeout(r, 20));
    });
    // Search input should be cleared
    expect((searchInput as HTMLInputElement).value).toBe("");
  });
});
