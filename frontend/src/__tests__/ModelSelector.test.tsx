/**
 * Tests for the ModelSelector component.
 *
 * Phase 5B — Model and Prompt Controls
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, cleanup } from "@testing-library/react";
import { ModelSelector } from "../ModelSelector";

describe("ModelSelector", () => {
  const mockSessionId = 1;

  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const MOCK_MODELS = [
    { name: "llama3.2:3b", size: "2.0 GB", modified_at: "2024-01-01" },
    { name: "llama3.2:1b", size: "1.0 GB", modified_at: "2024-01-01" },
    { name: "mistral:7b", size: "4.0 GB", modified_at: "2024-01-01" },
    { name: "nomic-embed-text:latest", size: "0.3 GB", modified_at: "2024-01-01" },
  ];

  function createMockModelsResponse() {
    return new Response(
      JSON.stringify({ models: MOCK_MODELS, error: null }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  }

  /**
   * Phase 7C: the component now fetches through api.ts, which sends an
   * implicit /api/auth/refresh preflight before the first real request.
   * Order-based mockResolvedValueOnce mocking breaks under that flow, so
   * these URL-aware mocks return a FRESH Response per call (Response bodies
   * must not be shared across fetch calls).
   */
  function mockModelsAndPatch(patchBody: { id: number; model: string | null }) {
    vi.mocked(fetch).mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) {
        // No refresh cookie in the test browser — auth stays unset.
        return Promise.resolve(
          new Response(JSON.stringify({ detail: "No refresh cookie" }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          })
        );
      }
      if (url.includes("/api/sessions/") && url.endsWith("/model")) {
        return Promise.resolve(
          new Response(JSON.stringify(patchBody), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        );
      }
      return Promise.resolve(createMockModelsResponse());
    });
  }

  it("renders the trigger button with default label", () => {
    render(
      <ModelSelector
        sessionId={mockSessionId}
        currentModel={null}
        onModelChange={() => {}}
      />
    );

    expect(screen.getByText("Default model")).toBeTruthy();
  });

  it("shows the current model name when set", () => {
    render(
      <ModelSelector
        sessionId={mockSessionId}
        currentModel="llama3.2:1b"
        onModelChange={() => {}}
      />
    );

    expect(screen.getByText("llama3.2:1b")).toBeTruthy();
  });

  it("fetches models on dropdown open", async () => {
    vi.mocked(fetch).mockResolvedValue(createMockModelsResponse());

    render(
      <ModelSelector
        sessionId={mockSessionId}
        currentModel={null}
        onModelChange={() => {}}
      />
    );

    // Click trigger to open dropdown
    const trigger = screen.getByText("Default model").closest("button");
    expect(trigger).toBeTruthy();

    await act(async () => {
      fireEvent.click(trigger!);
      await new Promise((r) => setTimeout(r, 50));
    });

    // Should show the dropdown with model list
    expect(screen.getByText("Select Model")).toBeTruthy();
    expect(screen.getByText("llama3.2:3b")).toBeTruthy();
    expect(screen.getByText("llama3.2:1b")).toBeTruthy();
    expect(screen.getByText("mistral:7b")).toBeTruthy();
    expect(screen.queryByText("nomic-embed-text:latest")).toBeNull();
    expect(screen.getAllByText("Default model")).toHaveLength(2);
    expect(screen.queryByText(/Default \(llama/i)).toBeNull();
  });

  it("shows error message when model fetch fails", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({ models: [], error: "Ollama is not running" }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    render(
      <ModelSelector
        sessionId={mockSessionId}
        currentModel={null}
        onModelChange={() => {}}
      />
    );

    const trigger = screen.getByText("Default model").closest("button");
    await act(async () => {
      fireEvent.click(trigger!);
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(screen.getByText("Ollama is not running")).toBeTruthy();
  });

  it("calls onModelChange when a model is selected", async () => {
    mockModelsAndPatch({ id: 1, model: "llama3.2:1b" });
    const onModelChange = vi.fn();

    render(
      <ModelSelector
        sessionId={mockSessionId}
        currentModel={null}
        onModelChange={onModelChange}
      />
    );

    const trigger = screen.getByText("Default model").closest("button");
    await act(async () => {
      fireEvent.click(trigger!);
      await new Promise((r) => setTimeout(r, 50));
    });

    // Click a model option
    await act(async () => {
      fireEvent.click(screen.getByText("llama3.2:1b"));
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(onModelChange).toHaveBeenCalledWith("llama3.2:1b");
  });

  it("selects default option and calls onModelChange with null", async () => {
    mockModelsAndPatch({ id: 1, model: null });
    const onModelChange = vi.fn();

    render(
      <ModelSelector
        sessionId={mockSessionId}
        currentModel="llama3.2:1b"
        onModelChange={onModelChange}
      />
    );

    const trigger = screen.getByText("llama3.2:1b").closest("button");
    await act(async () => {
      fireEvent.click(trigger!);
      await new Promise((r) => setTimeout(r, 50));
    });

    // Click the configuration-backed Default option.
    await act(async () => {
      fireEvent.click(screen.getByText("Use server default").closest("button")!);
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(onModelChange).toHaveBeenCalledWith(null);
  });

  it("shows loading state while fetching models", async () => {
    // Create a promise that never resolves to simulate loading
    vi.mocked(fetch).mockImplementation(() => new Promise(() => {}));

    render(
      <ModelSelector
        sessionId={mockSessionId}
        currentModel={null}
        onModelChange={() => {}}
      />
    );

    const trigger = screen.getByText("Default model").closest("button");
    await act(async () => {
      fireEvent.click(trigger!);
      await new Promise((r) => setTimeout(r, 20));
    });

    expect(screen.getByText("Loading models…")).toBeTruthy();
  });

  it("closes dropdown when clicking outside", async () => {
    vi.mocked(fetch).mockResolvedValue(createMockModelsResponse());

    render(
      <div>
        <div data-testid="outside">Outside</div>
        <ModelSelector
          sessionId={mockSessionId}
          currentModel={null}
          onModelChange={() => {}}
        />
      </div>
    );

    const trigger = screen.getByText("Default model").closest("button");
    await act(async () => {
      fireEvent.click(trigger!);
      await new Promise((r) => setTimeout(r, 50));
    });

    // Dropdown should be visible
    expect(screen.getByText("Select Model")).toBeTruthy();

    // Click outside
    await act(async () => {
      fireEvent.mouseDown(screen.getByTestId("outside"));
      await new Promise((r) => setTimeout(r, 20));
    });

    // Dropdown should be closed
    expect(screen.queryByText("Select Model")).toBeNull();
  });
});
