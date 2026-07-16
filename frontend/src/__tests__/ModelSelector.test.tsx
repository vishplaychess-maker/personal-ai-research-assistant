/**
 * Tests for the ModelSelector component.
 *
 * Phase 5B — Model and Prompt Controls
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { ModelSelector } from "../ModelSelector";

describe("ModelSelector", () => {
  const mockSessionId = 1;

  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function createMockModelsResponse() {
    return new Response(
      JSON.stringify({
        models: [
          { name: "llama3.2:3b", size: "2.0 GB", modified_at: "2024-01-01" },
          { name: "llama3.2:1b", size: "1.0 GB", modified_at: "2024-01-01" },
          { name: "mistral:7b", size: "4.0 GB", modified_at: "2024-01-01" },
        ],
        error: null,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
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
    vi.mocked(fetch).mockResolvedValue(createMockModelsResponse());
    const onModelChange = vi.fn();

    // Mock the PATCH request
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ id: 1, model: "llama3.2:1b" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );

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
    vi.mocked(fetch).mockResolvedValue(createMockModelsResponse());
    const onModelChange = vi.fn();

    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ id: 1, model: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );

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

    // Click the "Default" option
    await act(async () => {
      fireEvent.click(screen.getByText("Default (llama3.2:3b)"));
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
