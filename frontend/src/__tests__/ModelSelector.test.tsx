/**
 * Tests for the ModelSelector component.
 *
 * Modernized to the CURRENT production contract: the picker fetches
 * `API.listAllProviderModels()` (GET /api/providers/models) which returns a
 * ProviderModelGroup[] (grouped by provider), renders a portal menu with
 * role="option" buttons, and shows server-provided error text.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, cleanup } from "@testing-library/react";
import { ModelSelector } from "../ModelSelector";
import type { ProviderModelGroup } from "../types";

describe("ModelSelector", () => {
  const mockSessionId = 1;

  // GET /api/providers/models -> ProviderModelGroup[]
  const PROVIDER_GROUPS: ProviderModelGroup[] = [
    {
      provider: "ollama",
      provider_label: "Ollama",
      provider_id: 1,
      models: [
        { name: "llama3.2:3b", size: "2.0 GB", modified_at: "2024-01-01", is_free: true },
        { name: "llama3.2:1b", size: "1.0 GB", modified_at: "2024-01-01", is_free: true },
        { name: "mistral:7b", size: "4.0 GB", modified_at: "2024-01-01", is_free: true },
        { name: "nomic-embed-text:latest", size: "0.3 GB", modified_at: "2024-01-01", is_free: true },
      ],
    },
  ];

  /** URL-aware fetch mock: /api/auth/refresh, session-model PATCH, provider PUT,
   *  and the provider-group list. Fresh Response per call (bodies are single-use). */
  function mockFetch(opts: { groups?: ProviderModelGroup[]; modelsError?: string | null } = {}) {
    const groups = opts.groups ?? PROVIDER_GROUPS;
    vi.mocked(fetch).mockImplementation((input: unknown) => {
      const url = String(input);
      // Resolve on a macrotask so the response lands AFTER the component has
      // measured its trigger (the portal is gated on that measurement).
      const json = (body: unknown, status = 200) =>
        new Promise<Response>((resolve) =>
          setTimeout(
            () =>
              resolve(
                new Response(JSON.stringify(body), {
                  status,
                  headers: { "Content-Type": "application/json" },
                })
              ),
            10
          )
        );
      if (url.includes("/api/auth/refresh")) {
        return json({ detail: "No refresh cookie" }, 401);
      }
      if (url.endsWith("/model")) {
        return json({ id: mockSessionId, model: null });
      }
      if (/\/api\/providers\/\d+$/.test(url)) {
        return json({ id: 1, is_active: true });
      }
      if (url.includes("/api/providers/models")) {
        if (opts.modelsError) return json({ detail: opts.modelsError }, 500);
        return json(groups);
      }
      return json(groups);
    });
  }

  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
    // Deterministic, non-zero trigger rect so the portal menu positions
    // reproducibly in jsdom (jsdom otherwise reports a zero-size rect).
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 40, left: 0, right: 320, width: 320, height: 40,
      x: 0, y: 0, toJSON: () => ({}),
    } as DOMRect);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  function triggerButton(): HTMLElement {
    const btn = screen.getByRole("button", { name: /default model|llama3\.2:1b/i });
    return btn;
  }

  async function openMenu() {
    // Two separate act() blocks: the first flushes the [open] effect that
    // measures the trigger; the second lets the async model fetch resolve and
    // re-render, which is when the portal content mounts.
    await act(async () => {
      fireEvent.click(triggerButton());
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 80));
    });
  }

  it("renders the trigger button with default label", () => {
    mockFetch();
    render(<ModelSelector sessionId={mockSessionId} currentModel={null} onModelChange={() => {}} />);
    expect(screen.getByText("Default model")).toBeTruthy();
  });

  it("shows the current model name when set", () => {
    mockFetch();
    render(<ModelSelector sessionId={mockSessionId} currentModel="llama3.2:1b" onModelChange={() => {}} />);
    expect(screen.getByText("llama3.2:1b")).toBeTruthy();
  });

  it("fetches provider models on dropdown open", async () => {
    mockFetch();
    render(<ModelSelector sessionId={mockSessionId} currentModel={null} onModelChange={() => {}} />);
    await openMenu();

    // menu header + provider group label
    expect(screen.getByText("Select Model")).toBeTruthy();
    expect(screen.getByText("Ollama")).toBeTruthy();
    // every non-embed model is offered as an accessible option
    expect(screen.getByRole("option", { name: /llama3\.2:3b/i })).toBeTruthy();
    expect(screen.getByRole("option", { name: /llama3\.2:1b/i })).toBeTruthy();
    expect(screen.getByRole("option", { name: /mistral:7b/i })).toBeTruthy();
    // embedding models are filtered out
    expect(screen.queryByText(/nomic-embed-text/i)).toBeNull();
    // trigger + the configuration-backed default option
    expect(screen.getAllByText("Default model")).toHaveLength(2);
    expect(screen.queryByText(/Default \(llama/i)).toBeNull();
  });

  it("shows the empty state when no providers are configured", async () => {
    mockFetch({ groups: [] });
    render(<ModelSelector sessionId={mockSessionId} currentModel={null} onModelChange={() => {}} />);
    await openMenu();
    expect(screen.getByText("No providers configured. Add one in Settings.")).toBeTruthy();
  });

  it("shows the server error message when the model fetch fails", async () => {
    mockFetch({ modelsError: "Ollama is not running" });
    render(<ModelSelector sessionId={mockSessionId} currentModel={null} onModelChange={() => {}} />);
    await openMenu();
    expect(screen.getByText("Ollama is not running")).toBeTruthy();
  });

  it("calls onModelChange when a model is selected", async () => {
    mockFetch();
    const onModelChange = vi.fn();
    render(<ModelSelector sessionId={mockSessionId} currentModel={null} onModelChange={onModelChange} />);
    await openMenu();

    await act(async () => {
      fireEvent.click(screen.getByRole("option", { name: /llama3\.2:1b/i }));
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(onModelChange).toHaveBeenCalledWith("llama3.2:1b");
  });

  it("selects the default option and calls onModelChange with null", async () => {
    mockFetch();
    const onModelChange = vi.fn();
    render(<ModelSelector sessionId={mockSessionId} currentModel="llama3.2:1b" onModelChange={onModelChange} />);
    await openMenu();

    await act(async () => {
      fireEvent.click(screen.getByRole("option", { name: /default model/i }));
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(onModelChange).toHaveBeenCalledWith(null);
  });

  it("shows loading state while fetching models", async () => {
    vi.mocked(fetch).mockImplementation(() => new Promise(() => {}));
    render(<ModelSelector sessionId={mockSessionId} currentModel={null} onModelChange={() => {}} />);
    await openMenu();
    expect(screen.getByText("Loading models…")).toBeTruthy();
  });

  it("closes dropdown when clicking outside", async () => {
    mockFetch();
    render(
      <div>
        <div data-testid="outside">Outside</div>
        <ModelSelector sessionId={mockSessionId} currentModel={null} onModelChange={() => {}} />
      </div>
    );
    await openMenu();
    expect(screen.getByText("Select Model")).toBeTruthy();

    await act(async () => {
      fireEvent.mouseDown(screen.getByTestId("outside"));
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(screen.queryByText("Select Model")).toBeNull();
  });
});
