/**
 * Tests for the SystemPromptEditor component.
 *
 * Phase 5B — Model and Prompt Controls
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { SystemPromptEditor } from "../SystemPromptEditor";

describe("SystemPromptEditor", () => {
  const mockSessionId = 1;

  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function createMockPromptResponse(
    prompt: string,
    usingDefault: boolean = true
  ) {
    return new Response(
      JSON.stringify({
        system_prompt: prompt,
        using_default: usingDefault,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  }

  it("loads and displays the current system prompt", async () => {
    const promptText = "You are an expert Python programmer.";
    // Phase 7C: the load path calls refreshAccessToken() first (an implicit
    // /api/auth/refresh fetch). Return a FRESH Response per call so bodies
    // are never shared, and reject the refresh (no refresh cookie in tests).
    vi.mocked(fetch).mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) {
        return Promise.resolve(
          new Response(JSON.stringify({ detail: "No refresh cookie" }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          })
        );
      }
      return Promise.resolve(createMockPromptResponse(promptText, false));
    });

    render(
      <SystemPromptEditor sessionId={mockSessionId} onClose={() => {}} />
    );

    // Wait for loading to complete
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(screen.getByText("⚙ System Prompt")).toBeTruthy();
    expect(screen.getByText("Custom")).toBeTruthy();
    const textarea = screen.getByDisplayValue(promptText);
    expect(textarea).toBeTruthy();
  });

  it("shows 'Default' badge when using default prompt", async () => {
    vi.mocked(fetch).mockResolvedValue(
      createMockPromptResponse("Default system prompt", true)
    );

    render(
      <SystemPromptEditor sessionId={mockSessionId} onClose={() => {}} />
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(screen.getByText("Default")).toBeTruthy();
  });

  it("shows loading state initially", () => {
    // Don't resolve fetch — keep loading
    vi.mocked(fetch).mockImplementation(() => new Promise(() => {}));

    render(
      <SystemPromptEditor sessionId={mockSessionId} onClose={() => {}} />
    );

    expect(screen.getByText("Loading system prompt…")).toBeTruthy();
  });

  it("saves prompt on Save button click", async () => {
    const initialPrompt = "Original prompt";
    vi.mocked(fetch).mockResolvedValueOnce(
      createMockPromptResponse(initialPrompt, false)
    );

    const onClose = vi.fn();

    render(
      <SystemPromptEditor sessionId={mockSessionId} onClose={onClose} />
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    // Change the prompt text
    const textarea = screen.getByDisplayValue(initialPrompt);
    await act(async () => {
      fireEvent.change(textarea, {
        target: { value: "New custom prompt" },
      });
    });

    // Mock the save response
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          system_prompt: "New custom prompt",
          using_default: false,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    // Click Save
    await act(async () => {
      fireEvent.click(screen.getByText("Save"));
      await new Promise((r) => setTimeout(r, 50));
    });

    // Should call onClose after save
    expect(onClose).toHaveBeenCalledOnce();

    // Verify fetch was called exactly 2 times (load + save)
    const allCalls = vi.mocked(fetch).mock.calls;
    expect(allCalls.length).toBe(2);

    // First call should be a GET-like load, second should be the PATCH save
    const saveCall = allCalls[1];
    expect(saveCall[0]).toBe(`/api/sessions/${mockSessionId}/system-prompt`);
    // Second argument should have method PATCH and body with new prompt
    const options = saveCall[1] as RequestInit;
    expect(options.method).toBe("PATCH");
    expect(options.body).toBe(
      JSON.stringify({ system_prompt: "New custom prompt" })
    );
  });

  it("resets prompt to default on Reset button click", async () => {
    const defaultPrompt = "Default system prompt";
    vi.mocked(fetch).mockResolvedValueOnce(
      createMockPromptResponse("Custom prompt", false)
    );

    const onClose = vi.fn();

    render(
      <SystemPromptEditor sessionId={mockSessionId} onClose={onClose} />
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    // Mock the reset response
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          system_prompt: defaultPrompt,
          using_default: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    // Click Reset
    const resetBtn = screen.getByText("Reset to Default");
    await act(async () => {
      fireEvent.click(resetBtn);
      await new Promise((r) => setTimeout(r, 50));
    });

    // Verify the PATCH was called with null body
    const allCalls = vi.mocked(fetch).mock.calls;
    expect(allCalls.length).toBe(2);

    const resetCall = allCalls[1];
    expect(resetCall[0]).toBe(`/api/sessions/${mockSessionId}/system-prompt`);
    const options = resetCall[1] as RequestInit;
    expect(options.method).toBe("PATCH");
    expect(options.body).toBe(
      JSON.stringify({ system_prompt: null })
    );
  });

  it("closes when overlay is clicked (not modal)", async () => {
    vi.mocked(fetch).mockResolvedValue(
      createMockPromptResponse("Test prompt", false)
    );

    const onClose = vi.fn();

    render(
      <SystemPromptEditor sessionId={mockSessionId} onClose={onClose} />
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    // Click the overlay (not the modal content)
    const overlay = screen.getByText("⚙ System Prompt").closest(
      ".sp-editor-overlay"
    );
    expect(overlay).toBeTruthy();

    await act(async () => {
      fireEvent.click(overlay!);
      await new Promise((r) => setTimeout(r, 20));
    });

    expect(onClose).toHaveBeenCalledOnce();
  });

  it("shows error message on save failure", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      createMockPromptResponse("Original", false)
    );

    render(
      <SystemPromptEditor sessionId={mockSessionId} onClose={() => {}} />
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    // Change text to enable save
    const textarea = screen.getByDisplayValue("Original");
    await act(async () => {
      fireEvent.change(textarea, { target: { value: "New prompt" } });
    });

    // Mock save failure
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Server error" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      })
    );

    await act(async () => {
      fireEvent.click(screen.getByText("Save"));
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(screen.getByText("Failed to save system prompt")).toBeTruthy();
  });

  it("shows character count", async () => {
    vi.mocked(fetch).mockResolvedValue(
      createMockPromptResponse("Short prompt", false)
    );

    render(
      <SystemPromptEditor sessionId={mockSessionId} onClose={() => {}} />
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(screen.getByText(/12\/2000/)).toBeTruthy();
  });
});
