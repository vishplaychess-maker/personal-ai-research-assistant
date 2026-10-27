/**
 * Tests for the Sidebar component.
 *
 * Phase 5C — Search and Frontend Refactoring
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Sidebar } from "../Sidebar";
import { ThemeProvider } from "../components/ThemeProvider";
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
    vi.useRealTimers(); // restoreAllMocks does NOT restore fake timers
  });

  function renderSidebar(props: Partial<Parameters<typeof Sidebar>[0]> = {}) {
    // Test-only harness repair: production `main.tsx` always mounts
    // `ThemeProvider` around the app and `Sidebar` calls `useTheme()`, so a
    // bare render throws before any assertion can run. Same wrapper App.test
    // and ModePropagation.test use. No production change.
    return render(
      <ThemeProvider>
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
      </ThemeProvider>
    );
  }

  // ── Session List ───────────────────────────────────────

  it("renders the session list with titles", () => {
    renderSidebar();
    expect(screen.getByText("Test Session 1")).toBeTruthy();
    expect(screen.getByText("Research on AI")).toBeTruthy();
  });

  // Replaces the removed "Research Sessions" header assertion. Contract now
  // covered: the sidebar landmark (<aside>), the product brand, and the primary
  // new-session control (an icon button whose accessible name is its title).
  it("renders the sidebar landmark, brand and new-session control", () => {
    renderSidebar();
    expect(screen.getByRole("complementary")).toBeTruthy();
    expect(screen.getByText("Thunder AI")).toBeTruthy();
    expect(screen.getByTitle("New session")).toBeTruthy();
  });

  it("shows loading state when sessions are loading", () => {
    renderSidebar({ loadingSessions: true, sessions: [] });
    expect(screen.getByText("Loading…")).toBeTruthy();
  });

  it("shows empty state when there are no sessions", () => {
    renderSidebar({ sessions: [] });
    expect(screen.getByText("No conversations yet")).toBeTruthy();
  });

  it("calls onCreateSession when New button is clicked", () => {
    const onCreateSession = vi.fn();
    renderSidebar({ onCreateSession });
    fireEvent.click(screen.getByTitle("New session"));
    expect(onCreateSession).toHaveBeenCalledOnce();
  });

  it("calls onSelectSession when a session is clicked", () => {
    const onSelectSession = vi.fn();
    renderSidebar({ onSelectSession });
    fireEvent.click(screen.getByText("Test Session 1"));
    expect(onSelectSession).toHaveBeenCalledWith(1);
  });

  it("highlights the active session", () => {
    renderSidebar({ activeSessionId: 1 });
    // Current active indication is the title emphasis colour: the active
    // session title is `text-foreground`, an inactive one `text-secondary`.
    expect(screen.getByText("Test Session 1").className).toContain("text-foreground");
    expect(screen.getByText("Research on AI").className).toContain("text-secondary");
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
    // The rename field is the textbox showing the session's current title.
    expect(screen.getByDisplayValue("Test Session 1")).toBeTruthy();
  });

  it("calls onRenameSession when rename is submitted", async () => {
    const onRenameSession = vi.fn();
    renderSidebar({ onRenameSession });
    const renameBtns = screen.getAllByTitle("Rename");
    await act(async () => {
      fireEvent.click(renameBtns[0]);
      await new Promise((r) => setTimeout(r, 20));
    });
    const renameInput = screen.getByDisplayValue("Test Session 1") as HTMLInputElement;
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

  it("shows health indicators", async () => {
    const user = userEvent.setup();
    renderSidebar();
    await user.click(screen.getByRole("button", { name: "More actions" }));
    const menu = await screen.findByRole("menu");
    expect(menu).toHaveTextContent("API");
    expect(menu).toHaveTextContent("LLM");
    expect(menu).toHaveTextContent("DB");
    expect(menu).toHaveTextContent("3/3");
  });

  it("shows health count when not all services are ok", async () => {
    const partialHealth: HealthStatus = {
      backend: "ok",
      chromadb: "error",
      ollama: "ok",
    };
    const user = userEvent.setup();
    renderSidebar({ health: partialHealth });
    await user.click(screen.getByRole("button", { name: "More actions" }));
    const menu = await screen.findByRole("menu");
    expect(menu).toHaveTextContent("2/3");
  });

  // ── Search ─────────────────────────────────────────────

  it("renders the search input", () => {
    renderSidebar();
    expect(screen.getByPlaceholderText("Search conversations")).toBeTruthy();
  });

  it("shows clear button when search query is typed", () => {
    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations");
    fireEvent.change(searchInput, { target: { value: "AI" } });
    expect(screen.getByTitle("Clear search")).toBeTruthy();
  });

  it("clears search when clear button is clicked", () => {
    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations");
    fireEvent.change(searchInput, { target: { value: "AI" } });
    fireEvent.click(screen.getByTitle("Clear search"));
    expect((searchInput as HTMLInputElement).value).toBe("");
  });

  it("shows empty state when search returns no results", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );

    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "xyznonexistent" } });
      vi.advanceTimersByTime(300); // Skip debounce
      await Promise.resolve();
    });

    expect(screen.getByText("No results found")).toBeTruthy();
  });

  it("shows search results when search returns matches", async () => {
    vi.useFakeTimers();
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
    const searchInput = screen.getByPlaceholderText("Search conversations");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      vi.advanceTimersByTime(300); // Skip debounce
      await Promise.resolve();
    });

    expect(screen.getByText("Test Session")).toBeTruthy();
    expect(screen.getByText("Tell me about AI")).toBeTruthy();
  });

  it("navigates to session when search result is clicked", async () => {
    vi.useFakeTimers();
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

    const searchInput = screen.getByPlaceholderText("Search conversations");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      vi.advanceTimersByTime(300); // Skip debounce
      await Promise.resolve();
    });

    // Click the search result
    const resultTitle = screen.getByText("AI Research");
    await act(async () => {
      fireEvent.click(resultTitle);
      await Promise.resolve();
    });

    expect(onSelectSession).toHaveBeenCalledWith(2);
  });

  it("shows searching state while search is in progress", async () => {
    vi.useFakeTimers();
    // Create a fetch that never resolves (simulating network delay)
    vi.mocked(fetch).mockImplementation(() => new Promise(() => {}));

    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      vi.advanceTimersByTime(300); // Skip debounce
      await Promise.resolve();
    });

    // Current in-progress indicator is the visible "Searching…" status text.
    expect(screen.getByText("Searching…")).toBeTruthy();
  });

  it("shows search error on HTTP 500", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "Server error" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      })
    );

    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      vi.advanceTimersByTime(300);
      await Promise.resolve();
    });

    expect(screen.getByText("HTTP 500")).toBeTruthy();
  });

  it("shows search error on network failure", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch).mockRejectedValue(new Error("Network error"));

    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "AI" } });
      vi.advanceTimersByTime(300);
      await Promise.resolve();
    });

    expect(screen.getByText("Network error")).toBeTruthy();
  });

  it("clears search on Escape key", () => {
    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations");
    fireEvent.change(searchInput, { target: { value: "AI" } });
    // Press Escape
    fireEvent.keyDown(searchInput, { key: "Escape" });
    expect((searchInput as HTMLInputElement).value).toBe("");
  });

  it("removes spinner when search completes", async () => {
    vi.useFakeTimers();
    // Start with a pending fetch to let the spinner appear
    let resolveFetch: (r: Response) => void;
    const fetchPromise = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    vi.mocked(fetch).mockImplementation(() => fetchPromise);

    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "test" } });
      vi.advanceTimersByTime(300);
      await Promise.resolve();
    });

    // In-progress indicator while the fetch is pending
    expect(screen.getByText("Searching…")).toBeTruthy();

    // Now resolve the fetch
    await act(async () => {
      resolveFetch!(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
      await vi.runAllTimersAsync();
      await Promise.resolve();
    });

    // ... and replaced by the empty-results state after completion
    expect(screen.queryByText("Searching…")).toBeNull();
    expect(screen.getByText("No results found")).toBeTruthy();
  });

  it("passes encodeURIComponent for special characters", async () => {
    vi.useFakeTimers();
    let capturedUrl = "";
    vi.mocked(fetch).mockImplementation((url: RequestInfo | URL) => {
      capturedUrl = url.toString();
      return Promise.resolve(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    });

    renderSidebar();
    const searchInput = screen.getByPlaceholderText("Search conversations");
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: "c++ & c#" } });
      vi.advanceTimersByTime(300);
      await Promise.resolve();
    });

    expect(capturedUrl).toContain(encodeURIComponent("c++ & c#"));
    expect(capturedUrl).not.toContain("c++ & c#"); // raw chars not in URL
  });
});
