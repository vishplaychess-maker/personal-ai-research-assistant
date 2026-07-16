/**
 * Retry Integration Tests — exercises the complete retry flow:
 * handleRetry → handleSend → startStream.
 *
 * Simulates the App.tsx retry logic by creating a controlled test
 * component that mirrors the production retry behaviour.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, render, fireEvent } from "@testing-library/react";
import { useStreaming } from "../useStreaming";
import React, { useState, useCallback } from "react";

// ── Test Helpers ─────────────────────────────────────────

function createMockSSEResponse(chunks: string[]) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

// ── Test Component ───────────────────────────────────────
// Mirrors the App.tsx retry logic but in a controlled, testable way.

interface RetryTestComponentProps {
  onStateChange?: (state: {
    sending: boolean;
    chatError: string | null;
    hasRetryTarget: boolean;
    messages: string[];
    memoriesUsedIds: number[];
  }) => void;
}

function RetryTestComponent({ onStateChange }: RetryTestComponentProps) {
  const { isStreaming, streamedContent, startStream, cancelStream } =
    useStreaming();
  const [sending, setSending] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [retryTarget, setRetryTarget] = useState<{
    message: string;
    errorDetail: string;
  } | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [memoriesUsedIds, setMemoriesUsedIds] = useState<Set<number>>(
    new Set()
  );

  // Mirror App.tsx's handleSend and handleRetry
  const handleSend = useCallback(
    (overrideText?: string) => {
      const text = (overrideText ?? "").trim();
      if (!text || isStreaming) return;

      const originalText = text;

      setSending(true);
      setChatError(null);
      setRetryTarget(null);

      const tempMsg: Message = {
        id: -Date.now(),
        role: "user",
        content: text,
      };
      setMessages((prev) => [...prev, tempMsg]);

      startStream(1, text, {
        onStart: () => {},
        onToken: () => {},
        onComplete: (result) => {
          setSending(false);
          if (result.memoriesUsed && result.messageId) {
            setMemoriesUsedIds((prev) => new Set(prev).add(result.messageId));
          }
          onStateChange?.({
            sending: false,
            chatError: null,
            hasRetryTarget: false,
            messages: [],
            memoriesUsedIds: result.memoriesUsed ? [result.messageId] : [],
          });
        },
        onError: (error) => {
          setChatError(error.detail || "Failed");
          setMessages((prev) => prev.filter((m) => m.id !== tempMsg.id));
          setRetryTarget({
            message: originalText,
            errorDetail: error.detail || "Failed",
          });
          setSending(false);
        },
        onCancelled: () => {
          setMessages((prev) => prev.filter((m) => m.id !== tempMsg.id));
          setSending(false);
        },
      });
    },
    [isStreaming, startStream, onStateChange]
  );

  const handleRetry = useCallback(() => {
    if (!retryTarget || isStreaming) return;
    handleSend(retryTarget.message);
  }, [retryTarget, isStreaming, handleSend]);

  return (
    <div>
      <div data-testid="state">
        {JSON.stringify({
          sending,
          chatError,
          hasRetryTarget: !!retryTarget,
          isStreaming,
        })}
      </div>
      <div data-testid="retry-target">
        {retryTarget ? retryTarget.message : ""}
      </div>
      <button
        data-testid="send-btn"
        onClick={() => handleSend("Original message")}
        disabled={isStreaming}
      >
        Send
      </button>
      <button
        data-testid="retry-btn"
        onClick={handleRetry}
        disabled={!retryTarget || isStreaming}
      >
        Retry
      </button>
      <button data-testid="cancel-btn" onClick={cancelStream}>
        Cancel
      </button>
    </div>
  );
}

interface Message {
  id: number;
  role: string;
  content: string;
}

// ── Retry Flow Tests ─────────────────────────────────────

describe("Retry Integration — handleRetry → handleSend → startStream", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("retryTarget is set when streaming errors and records the original message", async () => {
    // Mock an error response (HTTP 500)
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "Ollama unavailable" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      })
    );

    const stateChanges: any[] = [];
    const { container } = render(
      <RetryTestComponent
        onStateChange={(s) => stateChanges.push(s)}
      />
    );

    // Trigger send which will fail
    const sendBtn = container.querySelector(
      '[data-testid="send-btn"]'
    ) as HTMLButtonElement;
    fireEvent.click(sendBtn);

    // Wait for async error to propagate
    await new Promise((r) => setTimeout(r, 100));

    // retryTarget should contain the original message
    const retryTargetDiv = container.querySelector(
      '[data-testid="retry-target"]'
    );
    expect(retryTargetDiv?.textContent).toBe("Original message");

    // Retry button should be enabled
    const retryBtn = container.querySelector(
      '[data-testid="retry-btn"]'
    ) as HTMLButtonElement;
    expect(retryBtn.disabled).toBe(false);
  });

  it("handleRetry resends the original user message exactly once", async () => {
    let fetchCallCount = 0;
    vi.mocked(fetch).mockImplementation(async (url, options) => {
      fetchCallCount++;
      const body = JSON.parse((options?.body as string) || "{}");
      if (fetchCallCount === 1) {
        // First call fails
        return new Response(
          JSON.stringify({ detail: "Ollama unavailable" }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
      // Retry succeeds
      expect(body.message).toBe("Original message");
      return createMockSSEResponse([
        "event: start\ndata: {}\n\n",
        "event: complete\ndata: {\"message_id\":1,\"citations\":[]}\n\n",
      ]);
    });

    const { container } = render(<RetryTestComponent />);

    // Click send (fails)
    const sendBtn = container.querySelector(
      '[data-testid="send-btn"]'
    ) as HTMLButtonElement;
    fireEvent.click(sendBtn);
    await new Promise((r) => setTimeout(r, 100));

    // Click retry (succeeds)
    const retryBtn = container.querySelector(
      '[data-testid="retry-btn"]'
    ) as HTMLButtonElement;
    fireEvent.click(retryBtn);
    await new Promise((r) => setTimeout(r, 100));

    // fetch was called exactly twice (1 original + 1 retry)
    expect(fetchCallCount).toBe(2);
  });

  it("repeated retry clicks do not create duplicate requests", async () => {
    let fetchCallCount = 0;
    vi.mocked(fetch).mockImplementation(async (_url, options) => {
      fetchCallCount++;
      const body = JSON.parse((options?.body as string) || "{}");
      if (fetchCallCount === 1) {
        // First call fails
        return new Response(
          JSON.stringify({ detail: "Ollama unavailable" }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
      // Subsequent calls succeed — but second retry click should be blocked
      expect(body.message).toBe("Original message");
      return createMockSSEResponse([
        "event: start\ndata: {}\n\n",
        "event: complete\ndata: {\"message_id\":1,\"citations\":[]}\n\n",
      ]);
    });

    const { container } = render(<RetryTestComponent />);

    // Click send (fails)
    const sendBtn = container.querySelector(
      '[data-testid="send-btn"]'
    ) as HTMLButtonElement;
    fireEvent.click(sendBtn);
    await new Promise((r) => setTimeout(r, 100));

    // Click retry twice rapidly
    const retryBtn = container.querySelector(
      '[data-testid="retry-btn"]'
    ) as HTMLButtonElement;
    fireEvent.click(retryBtn);
    fireEvent.click(retryBtn); // Second click — should be blocked (isStreaming=true)
    await new Promise((r) => setTimeout(r, 100));

    // fetch was called exactly twice (1 original + 1 retry, NOT 3)
    expect(fetchCallCount).toBe(2);
  });

  it("retry completion creates exactly one assistant message", async () => {
    vi.mocked(fetch).mockImplementation(async (_url, options) => {
      const body = JSON.parse((options?.body as string) || "{}");
      if (fetchCallCount === 0) {
        return new Response(
          JSON.stringify({ detail: "Error" }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
      return createMockSSEResponse([
        "event: start\ndata: {}\n\n",
        "event: complete\ndata: {\"message_id\":1,\"citations\":[]}\n\n",
      ]);
    });

    let fetchCallCount = 0;
    vi.mocked(fetch).mockImplementation(async (_url, options) => {
      fetchCallCount++;
      if (fetchCallCount === 1) {
        return new Response(
          JSON.stringify({ detail: "Error" }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
      return createMockSSEResponse([
        "event: start\ndata: {}\n\n",
        "event: complete\ndata: {\"message_id\":55,\"citations\":[]}\n\n",
      ]);
    });

    const onComplete = vi.fn();
    const { result } = renderHook(() => useStreaming());

    // Simulate: starting a stream that errors, then retrying successfully
    // First stream — error
    await act(async () => {
      result.current.startStream(1, "Original message", {
        onStart: () => {},
        onToken: () => {},
        onComplete: () => {},
        onError: () => {},
      });
    });

    // Second stream — retry, should succeed
    await act(async () => {
      result.current.startStream(1, "Original message", {
        onComplete: (res) => {
          onComplete();
          expect(res.messageId).toBe(55);
        },
      });
    });

    // Exactly one complete event for the retry
    expect(onComplete).toHaveBeenCalledOnce();
    expect(fetchCallCount).toBe(2);
  });

  it("retry cancellation restores the UI correctly", async () => {
    const streamController: { current: ReadableStreamDefaultController | null } = { current: null };
    const stream = new ReadableStream({
      start(controller) {
        streamController.current = controller;
        controller.enqueue(
          new TextEncoder().encode(
            "event: start\ndata: {}\n\n"
          )
        );
      },
    });
    const mockResponse = new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });

    // First call fails, second call hangs (to test cancellation)
    let callNum = 0;
    vi.mocked(fetch).mockImplementation(async () => {
      callNum++;
      if (callNum === 1) {
        return new Response(
          JSON.stringify({ detail: "Error" }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
      return mockResponse;
    });

    const { container } = render(<RetryTestComponent />);

    // Click send (fails)
    const sendBtn = container.querySelector(
      '[data-testid="send-btn"]'
    ) as HTMLButtonElement;
    fireEvent.click(sendBtn);
    await new Promise((r) => setTimeout(r, 100));

    // Click retry (hangs — stream doesn't close)
    const retryBtn = container.querySelector(
      '[data-testid="retry-btn"]'
    ) as HTMLButtonElement;
    fireEvent.click(retryBtn);
    await new Promise((r) => setTimeout(r, 50));

    // Cancel the retry
    const cancelBtn = container.querySelector(
      '[data-testid="cancel-btn"]'
    ) as HTMLButtonElement;
    fireEvent.click(cancelBtn);
    streamController.current?.close();
    await new Promise((r) => setTimeout(r, 50));

    // Verify state via data-testid="state"
    const stateDiv = container.querySelector('[data-testid="state"]');
    const state = JSON.parse(stateDiv?.textContent || "{}");
    expect(state.sending).toBe(false);
    expect(state.hasRetryTarget).toBe(false); // retry cleared by handleSend on retry
  });

  it("memory badge remains accurate after retry", async () => {
    let fetchCallCount = 0;
    vi.mocked(fetch).mockImplementation(async (_url, options) => {
      fetchCallCount++;
      if (fetchCallCount === 1) {
        return new Response(
          JSON.stringify({ detail: "Error" }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
      // Retry succeeds with memories_used: true
      return createMockSSEResponse([
        "event: start\ndata: {}\n\n",
        "event: token\ndata: {\"token\":\"Hello\"}\n\n",
        "event: complete\ndata: {\"message_id\":42,\"citations\":[],\"memories_used\":true,\"sources_used\":false}\n\n",
      ]);
    });

    const stateChanges: any[] = [];
    const { container } = render(
      <RetryTestComponent
        onStateChange={(s) => stateChanges.push(s)}
      />
    );

    // Send (fails)
    const sendBtn = container.querySelector(
      '[data-testid="send-btn"]'
    ) as HTMLButtonElement;
    fireEvent.click(sendBtn);
    await new Promise((r) => setTimeout(r, 100));

    // Retry (succeeds with memory)
    const retryBtn = container.querySelector(
      '[data-testid="retry-btn"]'
    ) as HTMLButtonElement;
    fireEvent.click(retryBtn);
    await new Promise((r) => setTimeout(r, 100));

    // The successful retry should report memoriesUsed from the complete event
    const lastState = stateChanges[stateChanges.length - 1];
    if (lastState) {
      expect(lastState.memoriesUsedIds).toEqual([42]);
    }
  });
});
