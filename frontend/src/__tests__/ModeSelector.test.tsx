/**
 * Phase 1B.2R MP-3C3D3B — ModeSelector focused tests.
 *
 * Covers: default Auto, capability-gated disabling, keyboard accessibility,
 * no-submit, freeze-while-streaming, fetch-failure fail-safe, and
 * capability-refresh invalidation reset. Serialization/reconnect/idempotency
 * invariants (what gets SENT) are covered separately in useStreaming.test.ts
 * and durableStreaming.test.ts additions — this file is UI-only.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, cleanup, waitFor } from "@testing-library/react";
import { ModeSelector } from "../ModeSelector";
import type { ChatModeCapability } from "../types";

function settingsResponse(chat_modes: ChatModeCapability[]) {
  return new Response(
    JSON.stringify({ llm_provider: "ollama", api_key: "", model: "", chat_modes }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

const ALL_AVAILABLE: ChatModeCapability[] = [
  { mode: "chat", available: true },
  { mode: "deep_research", available: true },
  { mode: "multi_agent", available: true },
];

function mockFetchWith(chat_modes: ChatModeCapability[] | "error") {
  vi.mocked(fetch).mockImplementation((input: unknown) => {
    const url = String(input);
    if (url.includes("/api/auth/refresh")) {
      return Promise.resolve(
        new Response(JSON.stringify({ detail: "No refresh cookie" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }
    if (url.includes("/api/settings")) {
      if (chat_modes === "error") {
        return Promise.resolve(
          new Response(JSON.stringify({ detail: "boom" }), { status: 500 }),
        );
      }
      return Promise.resolve(settingsResponse(chat_modes));
    }
    return Promise.resolve(new Response("{}", { status: 200 }));
  });
}

describe("ModeSelector", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("defaults to displaying Auto", () => {
    mockFetchWith(ALL_AVAILABLE);
    render(<ModeSelector mode="auto" onModeChange={() => {}} />);
    expect(screen.getByText("Auto")).toBeTruthy();
  });

  it("the trigger is a real button (never submits an enclosing form)", () => {
    mockFetchWith(ALL_AVAILABLE);
    render(<ModeSelector mode="auto" onModeChange={() => {}} />);
    const trigger = screen.getByRole("button", { name: /Response mode/i });
    expect(trigger.tagName).toBe("BUTTON");
    expect(trigger.getAttribute("type")).toBe("button");
  });

  it("opens the menu on click and lists all four choices", async () => {
    mockFetchWith(ALL_AVAILABLE);
    render(<ModeSelector mode="auto" onModeChange={() => {}} />);
    const trigger = screen.getByRole("button", { name: /Response mode/i });
    await act(async () => {
      fireEvent.pointerDown(trigger);
      fireEvent.click(trigger);
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(screen.getByRole("menuitemradio", { name: /^Auto$/ })).toBeTruthy();
    expect(screen.getByRole("menuitemradio", { name: /Chat/ })).toBeTruthy();
    expect(screen.getByRole("menuitemradio", { name: /Deep Research/ })).toBeTruthy();
    expect(screen.getByRole("menuitemradio", { name: /Multi-Agent/ })).toBeTruthy();
  });

  it("opens the menu via keyboard (Enter on the focused trigger)", async () => {
    mockFetchWith(ALL_AVAILABLE);
    render(<ModeSelector mode="auto" onModeChange={() => {}} />);
    const trigger = screen.getByRole("button", { name: /Response mode/i });
    trigger.focus();
    await act(async () => {
      fireEvent.keyDown(trigger, { key: "Enter" });
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(screen.getByRole("menuitemradio", { name: /Chat/ })).toBeTruthy();
  });

  it("selecting an available mode calls onModeChange with the canonical value", async () => {
    mockFetchWith(ALL_AVAILABLE);
    const onModeChange = vi.fn();
    render(<ModeSelector mode="auto" onModeChange={onModeChange} />);
    const trigger = screen.getByRole("button", { name: /Response mode/i });
    await act(async () => {
      fireEvent.pointerDown(trigger);
      fireEvent.click(trigger);
      await new Promise((r) => setTimeout(r, 20));
    });
    const chatItem = screen.getByRole("menuitemradio", { name: /Chat/ });
    fireEvent.click(chatItem);
    expect(onModeChange).toHaveBeenCalledWith("chat");
  });

  it("an unavailable mode is visible, disabled, shows a reason, and cannot be selected", async () => {
    mockFetchWith([
      { mode: "chat", available: true },
      { mode: "deep_research", available: false, reason: "Not enabled on this deployment." },
      { mode: "multi_agent", available: true },
    ]);
    const onModeChange = vi.fn();
    render(<ModeSelector mode="auto" onModeChange={onModeChange} />);
    const trigger = screen.getByRole("button", { name: /Response mode/i });
    await act(async () => {
      fireEvent.pointerDown(trigger);
      fireEvent.click(trigger);
      await new Promise((r) => setTimeout(r, 20));
    });
    const drItem = screen.getByRole("menuitemradio", { name: /Deep Research/ });
    expect(drItem.getAttribute("aria-disabled") ?? drItem.getAttribute("data-disabled")).toBeTruthy();
    expect(screen.getByText("Not enabled on this deployment.")).toBeTruthy();
    fireEvent.click(drItem);
    expect(onModeChange).not.toHaveBeenCalled();
  });

  it("a capability-fetch failure never speculatively enables the advanced modes", async () => {
    mockFetchWith("error");
    render(<ModeSelector mode="auto" onModeChange={() => {}} />);
    const trigger = screen.getByRole("button", { name: /Response mode/i });
    await act(async () => {
      fireEvent.pointerDown(trigger);
      fireEvent.click(trigger);
      await new Promise((r) => setTimeout(r, 20));
    });
    await waitFor(() => {
      const drItem = screen.getByRole("menuitemradio", { name: /Deep Research/ });
      expect(drItem.getAttribute("aria-disabled") ?? drItem.getAttribute("data-disabled")).toBeTruthy();
    });
    // Chat must still be usable per the D3A/D1 contract even on a failed fetch.
    const chatItem = screen.getByRole("menuitemradio", { name: /Chat/ });
    expect(chatItem.getAttribute("aria-disabled") ?? chatItem.getAttribute("data-disabled")).toBeFalsy();
  });

  it("is frozen (disabled) while a message is streaming", () => {
    mockFetchWith(ALL_AVAILABLE);
    render(<ModeSelector mode="auto" onModeChange={() => {}} disabled />);
    const trigger = screen.getByRole("button", { name: /Response mode/i });
    expect(trigger).toBeDisabled();
  });

  it("resets a now-unavailable explicit selection to Auto on capability refresh, with a visible explanation", async () => {
    mockFetchWith([
      { mode: "chat", available: true },
      { mode: "deep_research", available: false, reason: "Not enabled on this deployment." },
      { mode: "multi_agent", available: true },
    ]);
    const onModeChange = vi.fn();
    render(<ModeSelector mode="deep_research" onModeChange={onModeChange} />);
    await waitFor(() => {
      expect(onModeChange).toHaveBeenCalledWith("auto");
    });
    expect(screen.getByText(/no longer available/i)).toBeTruthy();
  });
});
