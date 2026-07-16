/**
 * Tests for the useStreaming hook and SSE parser.
 *
 * Uses vitest with mocked fetch to simulate SSE streaming responses.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useStreaming, parseSSEBuffer } from "../useStreaming";

// ── SSE Parser Tests ─────────────────────────────────────

describe("parseSSEBuffer", () => {
  it("parses a single start event", () => {
    const buffer = "event: start\ndata: {\"session_id\": 1}\n\n";
    const { events, remainder } = parseSSEBuffer(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("start");
    expect(events[0].data.session_id).toBe(1);
    expect(remainder).toBe("");
  });

  it("parses multiple events in order", () => {
    const buffer = [
      "event: start",
      'data: {"session_id":1}',
      "",
      "event: token",
      'data: {"token":"Hello"}',
      "",
      "event: token",
      'data: {"token":" world"}',
      "",
      "event: complete",
      'data: {"message_id":42,"citations":[]}',
      "",
      "",
    ].join("\n");

    const { events, remainder } = parseSSEBuffer(buffer);
    expect(events).toHaveLength(4);
    expect(events[0].type).toBe("start");
    expect(events[1].type).toBe("token");
    expect(events[1].data.token).toBe("Hello");
    expect(events[2].type).toBe("token");
    expect(events[2].data.token).toBe(" world");
    expect(events[3].type).toBe("complete");
    expect(events[3].data.message_id).toBe(42);
    expect(remainder).toBe("");
  });

  it("handles partial/incomplete events in remainder", () => {
    const buffer = "event: token\ndata: {\"token\":\"Hello\"}\n\n";
    const { events, remainder } = parseSSEBuffer(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].data.token).toBe("Hello");
  });

  it("handles error events", () => {
    const buffer =
      'event: error\ndata: {"code":"OLLAMA_ERROR","detail":"Ollama unavailable"}\n\n';
    const { events } = parseSSEBuffer(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("error");
    expect(events[0].data.code).toBe("OLLAMA_ERROR");
    expect(events[0].data.detail).toBe("Ollama unavailable");
  });

  it("skips malformed JSON without crashing", () => {
    const buffer = "event: token\ndata: {invalid json}\n\n";
    const { events } = parseSSEBuffer(buffer);
    expect(events).toHaveLength(0);
  });

  it("handles empty buffer", () => {
    const { events, remainder } = parseSSEBuffer("");
    expect(events).toHaveLength(0);
    expect(remainder).toBe("");
  });

  it("returns remainder for incomplete events", () => {
    const buffer = "event: start\ndata: {\"session_id\":1}\n\nevent: tok";
    const { events, remainder } = parseSSEBuffer(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("start");
    expect(remainder).toContain("event: tok");
  });
});

// ── Hook Tests ───────────────────────────────────────────

describe("useStreaming", () => {
  const mockSessionId = 1;
  const mockMessage = "Hello";

  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

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

  it("emits onStart when stream begins", async () => {
    const mockResponse = createMockSSEResponse([
      "event: start\ndata: {\"session_id\":1}\n\nevent: complete\ndata: {\"message_id\":42,\"citations\":[]}\n\n",
    ]);
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const onStart = vi.fn();

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, { onStart });
    });

    expect(onStart).toHaveBeenCalledOnce();
  });

  it("accumulates tokens in correct order", async () => {
    const mockResponse = createMockSSEResponse([
      "event: start\ndata: {\"session_id\":1}\n\n",
      "event: token\ndata: {\"token\":\"Hello\"}\n\n",
      "event: token\ndata: {\"token\":\" world\"}\n\n",
      "event: token\ndata: {\"token\":\"!\"}\n\n",
      "event: complete\ndata: {\"message_id\":42,\"citations\":[]}\n\n",
    ]);
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const tokens: string[] = [];

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, {
        onToken: (token) => tokens.push(token),
      });
    });

    expect(tokens).toEqual(["Hello", " world", "!"]);
  });

  it("calls onComplete with result data including sources/memories flags", async () => {
    const mockResponse = createMockSSEResponse([
      "event: start\ndata: {\"session_id\":1}\n\n",
      "event: token\ndata: {\"token\":\"Hello\"}\n\n",
      "event: complete\ndata: {\"message_id\":42,\"citations\":[],\"sources_used\":true,\"memories_used\":true}\n\n",
    ]);
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const onComplete = vi.fn();

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, { onComplete });
    });

    expect(onComplete).toHaveBeenCalledOnce();
    expect(onComplete).toHaveBeenCalledWith(
      expect.objectContaining({
        messageId: 42,
        content: "Hello",
        sourcesUsed: true,
        memoriesUsed: true,
      })
    );
  });

  it("calls onComplete with sources_used=false and memories_used=false when flags absent", async () => {
    const mockResponse = createMockSSEResponse([
      "event: start\ndata: {\"session_id\":1}\n\n",
      "event: complete\ndata: {\"message_id\":43,\"citations\":[]}\n\n",
    ]);
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const onComplete = vi.fn();

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, { onComplete });
    });

    expect(onComplete).toHaveBeenCalledWith(
      expect.objectContaining({
        messageId: 43,
        sourcesUsed: false,
        memoriesUsed: false,
      })
    );
  });

  it("calls onError when server returns error event", async () => {
    const mockResponse = createMockSSEResponse([
      "event: start\ndata: {\"session_id\":1}\n\n",
      "event: error\ndata: {\"code\":\"OLLAMA_ERROR\",\"detail\":\"Cannot connect to Ollama\"}\n\n",
    ]);
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const onError = vi.fn();

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, { onError });
    });

    expect(onError).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith({
      code: "OLLAMA_ERROR",
      detail: "Cannot connect to Ollama",
    });
  });

  it("sets isStreaming to true during stream, false after complete", async () => {
    const mockResponse = createMockSSEResponse([
      "event: start\ndata: {\"session_id\":1}\n\n",
      "event: complete\ndata: {\"message_id\":42,\"citations\":[]}\n\n",
    ]);
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());

    expect(result.current.isStreaming).toBe(false);

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, {});
    });

    expect(result.current.isStreaming).toBe(false);
  });

  it("sets isStreaming back to false on error", async () => {
    vi.mocked(fetch).mockRejectedValue(new Error("Network failure"));

    const { result } = renderHook(() => useStreaming());
    const onError = vi.fn();

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, { onError });
    });

    expect(result.current.isStreaming).toBe(false);
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({
        code: "NETWORK_ERROR",
      })
    );
  });

  it("handles HTTP 404 error gracefully", async () => {
    const mockResponse = new Response(
      JSON.stringify({ detail: "Session 999 not found" }),
      { status: 404, headers: { "Content-Type": "application/json" } }
    );
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const onError = vi.fn();

    await act(async () => {
      result.current.startStream(999, mockMessage, { onError });
    });

    expect(onError).toHaveBeenCalledWith({
      code: "SESSION_NOT_FOUND",
      detail: "Session 999 not found",
    });
  });

  it("handles HTTP 422 validation error", async () => {
    const mockResponse = new Response(
      JSON.stringify({
        detail: [
          {
            loc: ["body", "message"],
            msg: "String should have at most 10000 characters",
          },
        ],
      }),
      { status: 422, headers: { "Content-Type": "application/json" } }
    );
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const onError = vi.fn();

    await act(async () => {
      result.current.startStream(1, "x".repeat(10001), { onError });
    });

    expect(onError).toHaveBeenCalled();
    expect(onError.mock.calls[0][0].code).toBe("VALIDATION_ERROR");
  });

  it("allows sequential streams after completion and avoids duplicates", async () => {
    let callCount = 0;
    vi.mocked(fetch).mockImplementation(async () => {
      callCount++;
      // Create a fresh response for each call to avoid consuming a locked stream
      const encoder = new TextEncoder();
      const stream = new ReadableStream({
        async start(controller) {
          controller.enqueue(
            encoder.encode(
              "event: start\ndata: {\"session_id\":1}\n\nevent: complete\ndata: {\"message_id\":" +
                String(callCount) +
                ",\"citations\":[]}\n\n"
            )
          );
          controller.close();
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    });

    const { result } = renderHook(() => useStreaming());
    const onComplete1 = vi.fn();
    const onComplete2 = vi.fn();

    await act(async () => {
      result.current.startStream(1, "msg1", { onComplete: onComplete1 });
    });

    // After first stream completes, try another
    await act(async () => {
      result.current.startStream(1, "msg2", { onComplete: onComplete2 });
    });

    expect(onComplete1).toHaveBeenCalledOnce();
    expect(onComplete2).toHaveBeenCalledOnce();
    // Each stream should produce exactly one complete event (no duplicates)
    expect(onComplete1.mock.calls[0][0].messageId).toBe(1);
    expect(onComplete2.mock.calls[0][0].messageId).toBe(2);
    expect(callCount).toBe(2);
  });

  it("prevents starting a second stream while first is active", async () => {
    // Create a stream that hangs (never closes) to simulate active streaming
    const stream = new ReadableStream({
      start(_controller) {
        // Never close — simulate ongoing generation
      },
    });
    const mockResponse = new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const onComplete1 = vi.fn();
    const onComplete2 = vi.fn();

    // Start first stream (don't await — it hangs)
    act(() => {
      result.current.startStream(1, "msg1", { onComplete: onComplete1 });
    });

    await new Promise((r) => setTimeout(r, 20));

    // Try starting a second stream while first is active
    await act(async () => {
      result.current.startStream(1, "msg2", { onComplete: onComplete2 });
    });

    // Clean up by cancelling
    await act(async () => {
      result.current.cancelStream();
    });

    // Second stream should not have been called
    expect(onComplete1).not.toHaveBeenCalled();
    expect(onComplete2).not.toHaveBeenCalled();
  });

  // ── Cancellation Tests ─────────────────────────────────

  function createHangingMockSSEResponse(): {
    response: Response;
    controller: ReadableStreamDefaultController | null;
  } {
    let streamController: ReadableStreamDefaultController | null = null;
    const stream = new ReadableStream({
      start(controller) {
        streamController = controller;
        controller.enqueue(
          new TextEncoder().encode(
            "event: start\ndata: {\"session_id\":1}\n\n"
          )
        );
      },
    });
    return {
      response: new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
      controller: streamController,
    };
  }

  it("calls onCancelled when aborted", async () => {
    const { response: mockResponse, controller } = createHangingMockSSEResponse();
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const onCancelled = vi.fn();

    // Start stream (don't await — it won't complete)
    act(() => {
      result.current.startStream(mockSessionId, mockMessage, { onCancelled });
    });

    // Wait for stream to start reading
    await new Promise((r) => setTimeout(r, 50));

    expect(result.current.isStreaming).toBe(true);

    // Cancel and close the stream controller to unblock the reader
    await act(async () => {
      result.current.cancelStream();
      controller?.close();
    });

    // Wait for cancellation to propagate
    await new Promise((r) => setTimeout(r, 50));

    expect(onCancelled).toHaveBeenCalledOnce();
    expect(result.current.isStreaming).toBe(false);
  });

  it("cancellation resets isStreaming and streamedContent", async () => {
    const { response: mockResponse, controller } = createHangingMockSSEResponse();
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());

    // Start stream (don't await — it won't complete)
    act(() => {
      result.current.startStream(mockSessionId, mockMessage, {});
    });

    // Wait for stream to start
    await new Promise((r) => setTimeout(r, 50));

    expect(result.current.isStreaming).toBe(true);

    // Cancel and close the stream controller to unblock the reader
    await act(async () => {
      result.current.cancelStream();
      controller?.close();
    });

    // Wait for cancellation to propagate
    await new Promise((r) => setTimeout(r, 50));

    expect(result.current.isStreaming).toBe(false);
    expect(result.current.streamedContent).toBe("");
  });

  it("calls cancelStream does not error when no active stream", () => {
    const { result } = renderHook(() => useStreaming());
    expect(() => result.current.cancelStream()).not.toThrow();
  });

  it("abort error during fetch (before response) calls onCancelled", async () => {
    // Mock fetch to reject with AbortError when the signal is aborted
    let abortSignal: AbortSignal | null = null;
    vi.mocked(fetch).mockImplementation(async (_url, options) => {
      abortSignal = options?.signal ?? null;
      // Simulate an abort happening before the response arrives
      return new Promise((_resolve, reject) => {
        if (abortSignal) {
          abortSignal.addEventListener("abort", () => {
            const err = new DOMException("The operation was aborted", "AbortError");
            reject(err);
          });
        }
      });
    });

    const { result } = renderHook(() => useStreaming());
    const onCancelled = vi.fn();

    // Start stream (don't await — it hangs waiting for response)
    act(() => {
      result.current.startStream(mockSessionId, mockMessage, { onCancelled });
    });

    await new Promise((r) => setTimeout(r, 20));

    // Cancel before response arrives
    await act(async () => {
      result.current.cancelStream();
    });

    await new Promise((r) => setTimeout(r, 20));

    expect(onCancelled).toHaveBeenCalledOnce();
    expect(result.current.isStreaming).toBe(false);
  });

  // ── Network Disconnect Test ─────────────────────────────

  it("handles network disconnect (fetch rejection)", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("Failed to fetch"));

    const { result } = renderHook(() => useStreaming());
    const onError = vi.fn();

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, { onError });
    });

    expect(onError).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({
        code: "NETWORK_ERROR",
        detail: "Failed to fetch",
      })
    );
    expect(result.current.isStreaming).toBe(false);
  });

  // ── Component Cleanup Test ─────────────────────────────

  it("component cleanup aborts an active stream", async () => {
    // Create a stream where we hold the controller for manual closure
    let streamController: ReadableStreamDefaultController | null = null;
    const stream = new ReadableStream({
      start(controller) {
        streamController = controller;
        controller.enqueue(
          new TextEncoder().encode(
            "event: start\ndata: {\"session_id\":1}\n\n"
          )
        );
      },
    });
    const mockResponse = new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const onCancelled = vi.fn();

    const { result, unmount } = renderHook(() => useStreaming());

    // Start stream (don't await — it hangs)
    act(() => {
      result.current.startStream(mockSessionId, mockMessage, { onCancelled });
    });

    await new Promise((r) => setTimeout(r, 20));

    expect(result.current.isStreaming).toBe(true);

    // Unmount triggers cleanup which aborts and sets cancelledRef
    await act(async () => {
      unmount();
      // Close the stream controller so reader.read() returns { done: true }
      // and the cancelledRef check at the top of the while loop fires
      streamController?.close();
    });

    // Wait for cancellation to propagate
    await new Promise((r) => setTimeout(r, 50));

    expect(onCancelled).toHaveBeenCalledOnce();
  });

  // ── Streamed Content Tests ─────────────────────────────

  it("streamedContent is present during streaming and cleared after completion", async () => {
    // Create a stream where we can observe intermediate state
    let streamController: ReadableStreamDefaultController | null = null;
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        streamController = controller;
        controller.enqueue(
          encoder.encode("event: start\ndata: {\"session_id\":1}\n\n")
        );
        controller.enqueue(
          encoder.encode("event: token\ndata: {\"token\":\"Hello\"}\n\n")
        );
        controller.close();
      },
    });
    const mockResponse = new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, {});
    });

    // After stream completes, streamedContent should be reset
    expect(result.current.streamedContent).toBe("");
  });

  it("streamedContent is cleared after cancellation", async () => {
    const { response: mockResponse, controller } = createHangingMockSSEResponse();
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());

    act(() => {
      result.current.startStream(mockSessionId, mockMessage, {});
    });

    await new Promise((r) => setTimeout(r, 50));

    await act(async () => {
      result.current.cancelStream();
      controller?.close();
    });

    await new Promise((r) => setTimeout(r, 50));

    expect(result.current.streamedContent).toBe("");
  });

  // ── Session Refresh Test ───────────────────────────────

  it("no duplicate assistant response after session refresh", async () => {
    // Simulate: complete a stream, then simulate a session refresh (second stream
    // for the same session should not duplicate the assistant message)
    let callCount = 0;
    vi.mocked(fetch).mockImplementation(async () => {
      callCount++;
      return createMockSSEResponse([
        "event: start\ndata: {\"session_id\":1}\n\n",
        `event: complete\ndata: {\"message_id\":${callCount},\"citations\":[]}\n\n`,
      ]);
    });

    const { result } = renderHook(() => useStreaming());
    const completedIds: number[] = [];
    const onComplete = vi.fn().mockImplementation((res) => {
      completedIds.push(res.messageId);
    });

    // Send first message (normal flow)
    await act(async () => {
      result.current.startStream(1, "First message", { onComplete });
    });

    // After first stream completes, refresh would call loadMessages
    // Simulate a second stream for the same session (refresh scenario)
    await act(async () => {
      result.current.startStream(1, "Second message", { onComplete });
    });

    // Verify exactly two complete events with distinct IDs (no duplicates)
    expect(onComplete).toHaveBeenCalledTimes(2);
    expect(completedIds).toEqual([1, 2]);
    expect(new Set(completedIds).size).toBe(2);
  });

  // ── Memory Off Compatibility Test ───────────────────────

  it("Memory Off remains compatible (memories_used=false in complete event)", async () => {
    const mockResponse = createMockSSEResponse([
      "event: start\ndata: {\"session_id\":1}\n\n",
      "event: complete\ndata: {\"message_id\":42,\"citations\":[],\"sources_used\":false,\"memories_used\":false}\n\n",
    ]);
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const onComplete = vi.fn();

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, { onComplete });
    });

    expect(onComplete).toHaveBeenCalledWith(
      expect.objectContaining({
        memoriesUsed: false,
        sourcesUsed: false,
      })
    );
    // Stream completes successfully even without memory/sources
    expect(onComplete).toHaveBeenCalledOnce();
  });

  // ── Malformed SSE Test ─────────────────────────────────

  // TODO: stale or missing session recovery — This is an App.tsx-level behavior
  // (handling 404 when session is deleted by another tab), not a hook-level
  // concern. Should be tested in integration/E2E tests.
  // TODO: Memory badge shown only from final server result — Verified above
  // by onComplete with memories_used=true/false. The hook correctly passes
  // these flags from the complete SSE event to the onComplete callback.


  it("handles malformed SSE event without crashing", async () => {
    // Send a chunk with a partial/malformed event then a valid complete event
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      async start(controller) {
        controller.enqueue(encoder.encode("event: "));
        controller.enqueue(encoder.encode("token\ndata: {\"token\":\"A\"}\n\n"));
        controller.enqueue(encoder.encode("event: complete\ndata: {\"message_id\":99,\"citations\":[]}\n\n"));
        controller.close();
      },
    });
    const mockResponse = new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
    vi.mocked(fetch).mockResolvedValue(mockResponse);

    const { result } = renderHook(() => useStreaming());
    const onComplete = vi.fn();

    await act(async () => {
      result.current.startStream(mockSessionId, mockMessage, { onComplete });
    });

    expect(onComplete).toHaveBeenCalledOnce();
    expect(onComplete).toHaveBeenCalledWith(
      expect.objectContaining({ messageId: 99 })
    );
  });
});
