/**
 * MP-3C3C — durable-mode behaviour of the useStreaming hook, plus proof that
 * the legacy (flag-off, no `X-Run-Id`) path is unchanged.
 *
 * Auth is neutralised with an in-memory non-expired access token so the single
 * shared `fetch` mock is consumed only by the streaming call (not by an
 * internal token-refresh fetch).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useStreaming } from "../useStreaming";
import { setAccessToken, resetRefreshState } from "../auth";

// ── helpers ──────────────────────────────────────────────
function fakeJwt(): string {
  const body = btoa(JSON.stringify({ exp: 9_999_999_999 }));
  return `h.${body}.s`;
}

function sseResponse(
  chunks: string[],
  opts: { runId?: string; status?: number } = {},
): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  const headers: Record<string, string> = { "Content-Type": "text/event-stream" };
  if (opts.runId) headers["X-Run-Id"] = opts.runId;
  return new Response(stream, { status: opts.status ?? 200, headers });
}

function jsonResponse(status: number, body: unknown, runId?: string): Response {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (runId) headers["X-Run-Id"] = runId;
  return new Response(JSON.stringify(body), { status, headers });
}

const RUN_ID = "11111111-2222-4333-8444-555555555555";

beforeEach(() => {
  setAccessToken(fakeJwt());
  resetRefreshState();
  vi.spyOn(globalThis, "fetch");
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  setAccessToken(null);
});

// ── legacy path unchanged ────────────────────────────────
describe("legacy (no X-Run-Id)", () => {
  it("streams tokens and completes exactly as before", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse([
        'event: start\ndata: {"session_id":1}\n\n',
        'event: token\ndata: {"token":"Hel"}\n\n',
        'event: token\ndata: {"token":"lo"}\n\n',
        'event: complete\ndata: {"message_id":42,"citations":[]}\n\n',
      ]),
    );
    const { result } = renderHook(() => useStreaming());
    const onToken = vi.fn();
    const onComplete = vi.fn();

    await act(async () => {
      await result.current.startStream(1, "hi", { onToken, onComplete });
    });

    expect(onToken.mock.calls.map((c) => c[0])).toEqual(["Hel", "lo"]);
    expect(onComplete).toHaveBeenCalledOnce();
    expect(onComplete.mock.calls[0][0]).toMatchObject({ messageId: 42, content: "Hello" });
    expect(result.current.durableRunId).toBeNull();
  });

  it("cancelStream() aborts and yields onCancelled (no run id)", async () => {
    vi.mocked(fetch).mockImplementation(
      (_u, init) =>
        new Promise((_res, rej) => {
          const signal = (init as RequestInit).signal;
          signal?.addEventListener("abort", () =>
            rej(new DOMException("aborted", "AbortError")),
          );
        }) as Promise<Response>,
    );
    const { result } = renderHook(() => useStreaming());
    const onCancelled = vi.fn();

    await act(async () => {
      result.current.startStream(1, "hi", { onCancelled });
    });
    await act(async () => {
      result.current.cancelStream();
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(onCancelled).toHaveBeenCalledOnce();
    expect(result.current.isStreaming).toBe(false);
  });
});

// ── idempotency key ──────────────────────────────────────
describe("Idempotency-Key", () => {
  it("sends one UUID key per send and reuses it on reconnect", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        sseResponse(['id: 1\nevent: token\ndata: {"token":"a"}\n\n'], { runId: RUN_ID }),
      )
      .mockResolvedValueOnce(
        sseResponse(
          ['id: 2\nevent: token\ndata: {"token":"b"}\n\n', 'id: 3\nevent: complete\ndata: {"message_id":9}\n\n'],
          { runId: RUN_ID },
        ),
      );

    const { result } = renderHook(() => useStreaming());
    const onComplete = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", { onComplete });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    const call0 = vi.mocked(fetch).mock.calls[0];
    const call1 = vi.mocked(fetch).mock.calls[1];
    const key0 = (call0[1] as RequestInit & { headers: Record<string, string> }).headers["Idempotency-Key"];
    const key1 = (call1[1] as RequestInit & { headers: Record<string, string> }).headers["Idempotency-Key"];
    expect(key0).toMatch(/^[0-9a-f-]{36}$/i);
    expect(key1).toBe(key0); // reconnect reuses the same key
    // reconnect also carried the cursor
    expect((call1[1] as RequestInit & { headers: Record<string, string> }).headers["Last-Event-ID"]).toBe("1");
    expect(String(call1[0])).toContain("after_seq=1");
    expect(onComplete).toHaveBeenCalledOnce();
  });

  it("mints a fresh key for a second explicit send", async () => {
    vi.mocked(fetch).mockImplementation(async () =>
      sseResponse(['event: complete\ndata: {"message_id":1,"citations":[]}\n\n']),
    );
    const { result } = renderHook(() => useStreaming());
    await act(async () => {
      await result.current.startStream(1, "one", {});
    });
    await act(async () => {
      await result.current.startStream(1, "two", {});
    });
    const k0 = (vi.mocked(fetch).mock.calls[0][1] as { headers: Record<string, string> }).headers["Idempotency-Key"];
    const k1 = (vi.mocked(fetch).mock.calls[1][1] as { headers: Record<string, string> }).headers["Idempotency-Key"];
    expect(k0).not.toBe(k1);
  });
});

// ── durable run identity ─────────────────────────────────
describe("X-Run-Id capture", () => {
  it("captures a valid uuid once and exposes it", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse(['id: 1\nevent: complete\ndata: {"message_id":7}\n\n'], { runId: RUN_ID }),
    );
    const { result } = renderHook(() => useStreaming());
    const onRunId = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", { onRunId });
    });
    expect(onRunId).toHaveBeenCalledExactlyOnceWith(RUN_ID);
  });

  it("ignores a malformed X-Run-Id header", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse(['event: complete\ndata: {"message_id":7,"citations":[]}\n\n'], { runId: "not-a-uuid" }),
    );
    const { result } = renderHook(() => useStreaming());
    const onRunId = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", { onRunId });
    });
    expect(onRunId).not.toHaveBeenCalled();
    expect(result.current.durableRunId).toBeNull();
  });
});

// ── dedupe on replay ─────────────────────────────────────
describe("durable replay dedupe", () => {
  it("does not re-emit a token whose id was already accepted", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        sseResponse(
          ['id: 1\nevent: token\ndata: {"token":"a"}\n\n', 'id: 2\nevent: token\ndata: {"token":"b"}\n\n'],
          { runId: RUN_ID },
        ),
      )
      .mockResolvedValueOnce(
        sseResponse(
          [
            'id: 1\nevent: token\ndata: {"token":"a"}\n\n', // replayed — must be ignored
            'id: 2\nevent: token\ndata: {"token":"b"}\n\n', // replayed — must be ignored
            'id: 3\nevent: token\ndata: {"token":"c"}\n\n',
            'id: 4\nevent: complete\ndata: {"message_id":9}\n\n',
          ],
          { runId: RUN_ID },
        ),
      );
    const { result } = renderHook(() => useStreaming());
    const onToken = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", { onToken });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(onToken.mock.calls.map((c) => c[0])).toEqual(["a", "b", "c"]);
  });
});

// ── reconnect bounds ─────────────────────────────────────
describe("bounded reconnect", () => {
  it("gives up with a recoverable error after the attempt cap", async () => {
    vi.useFakeTimers();
    // every pass: identified run, one token, then close with no terminal event
    vi.mocked(fetch).mockImplementation(async () =>
      sseResponse(['id: 1\nevent: token\ndata: {"token":"a"}\n\n'], { runId: RUN_ID }),
    );
    const { result } = renderHook(() => useStreaming());
    const onError = vi.fn();
    const onComplete = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", { onError, onComplete });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(onComplete).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0][0].code).toBe("reconnect_exhausted");
    // 1 initial + at most RECONNECT_MAX_ATTEMPTS (6) reconnects
    expect(vi.mocked(fetch).mock.calls.length).toBeLessThanOrEqual(7);
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(1);
  });
});

// ── unsupported mode / durable errors do NOT fall back ───
describe("durable error handling (no legacy fallback)", () => {
  it("422 → VALIDATION_ERROR, terminal, single request", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(422, { detail: "image mode not supported on durable path" }, RUN_ID),
    );
    const { result } = renderHook(() => useStreaming());
    const onError = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", { onError });
    });
    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0][0].code).toBe("VALIDATION_ERROR");
    expect(vi.mocked(fetch)).toHaveBeenCalledOnce();
    expect(result.current.isStreaming).toBe(false);
  });

  it("503 → DURABLE_UNAVAILABLE without retrying", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(503, { detail: "worker down" }));
    const { result } = renderHook(() => useStreaming());
    const onError = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", { onError });
    });
    expect(onError.mock.calls[0][0].code).toBe("DURABLE_UNAVAILABLE");
    expect(vi.mocked(fetch)).toHaveBeenCalledOnce();
  });
});

// ── approval hand-off ────────────────────────────────────
describe("tool_approval_required (durable)", () => {
  it("surfaces the redacted approval and enters waiting_approval", async () => {
    vi.useFakeTimers();
    let call = 0;
    vi.mocked(fetch).mockImplementation(async () => {
      call += 1;
      return call === 1
        ? sseResponse(
            [
              'id: 1\nevent: token\ndata: {"token":"working"}\n\n',
              'id: 2\nevent: tool_approval_required\ndata: {"approval_id":"ap_123","tool_id":"fs.read"}\n\n',
            ],
            { runId: RUN_ID },
          )
        : sseResponse(['id: 3\nevent: complete\ndata: {"message_id":9}\n\n'], { runId: RUN_ID });
    });
    const { result } = renderHook(() => useStreaming());
    const onApprovalRequired = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", { onApprovalRequired });
    });
    expect(onApprovalRequired).toHaveBeenCalledExactlyOnceWith({
      approvalId: "ap_123",
      toolId: "fs.read",
    });
    expect(result.current.phase).toBe("waiting_approval");
    expect(vi.mocked(fetch)).toHaveBeenCalledOnce(); // no auto-reconnect while paused

    // user approves → hook re-opens the stream for the continuation
    await act(async () => {
      result.current.resumeAfterApproval();
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(vi.mocked(fetch).mock.calls.length).toBe(2);
  });
});

// ── unmount cleanup ──────────────────────────────────────
describe("cleanup", () => {
  it("unmount aborts the in-flight fetch and schedules no reconnect", async () => {
    vi.useFakeTimers();
    let aborted = false;
    vi.mocked(fetch).mockImplementation((_u, init) => {
      const signal = (init as RequestInit).signal;
      return new Promise<Response>((_res, rej) => {
        signal?.addEventListener("abort", () => {
          aborted = true;
          rej(new DOMException("aborted", "AbortError"));
        });
      });
    });
    const { result, unmount } = renderHook(() => useStreaming());
    await act(async () => {
      result.current.startStream(1, "hi", {});
    });
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(aborted).toBe(true);
    expect(vi.mocked(fetch)).toHaveBeenCalledOnce(); // no reconnect after unmount
  });
});

// ── MP-3C3D3B — mode serialization + reconnect immutability ─

function bodyOf(call: unknown[]): Record<string, unknown> {
  return JSON.parse((call[1] as RequestInit).body as string);
}

describe("MP-3C3D3B — mode serialization", () => {
  it("Auto (mode omitted) sends no `mode` property at all", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse(['event: complete\ndata: {"message_id":1,"citations":[]}\n\n']),
    );
    const { result } = renderHook(() => useStreaming());
    await act(async () => {
      await result.current.startStream(1, "hi", {});
    });
    const body = bodyOf(vi.mocked(fetch).mock.calls[0]);
    expect("mode" in body).toBe(false);
  });

  it.each(["chat", "deep_research", "multi_agent"] as const)(
    "explicit %s sends exactly that canonical value",
    async (mode) => {
      vi.mocked(fetch).mockResolvedValue(
        sseResponse(['event: complete\ndata: {"message_id":1,"citations":[]}\n\n']),
      );
      const { result } = renderHook(() => useStreaming());
      await act(async () => {
        await result.current.startStream(1, "hi", undefined, {}, mode);
      });
      const body = bodyOf(vi.mocked(fetch).mock.calls[0]);
      expect(body.mode).toBe(mode);
    },
  );

  it("never sends mode as \"auto\", null, or a display label", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse(['event: complete\ndata: {"message_id":1,"citations":[]}\n\n']),
    );
    const { result } = renderHook(() => useStreaming());
    await act(async () => {
      await result.current.startStream(1, "hi", {});
    });
    const raw = (vi.mocked(fetch).mock.calls[0][1] as RequestInit).body as string;
    expect(raw).not.toContain("auto");
    expect(raw).not.toContain('"mode":null');
    expect(raw).not.toContain("Deep Research");
    expect(raw).not.toContain("Multi-Agent");
  });

  it("a durable reconnect reuses the SAME explicit mode as the initial send", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        sseResponse(['id: 1\nevent: token\ndata: {"token":"a"}\n\n'], { runId: RUN_ID }),
      )
      .mockResolvedValueOnce(
        sseResponse(['id: 2\nevent: complete\ndata: {"message_id":9}\n\n'], { runId: RUN_ID }),
      );
    const { result } = renderHook(() => useStreaming());
    await act(async () => {
      await result.current.startStream(1, "hi", undefined, {}, "deep_research");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    const body0 = bodyOf(vi.mocked(fetch).mock.calls[0]);
    const body1 = bodyOf(vi.mocked(fetch).mock.calls[1]);
    expect(body0.mode).toBe("deep_research");
    expect(body1.mode).toBe("deep_research");
  });

  it("a durable reconnect of an Auto send keeps omitting mode", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        sseResponse(['id: 1\nevent: token\ndata: {"token":"a"}\n\n'], { runId: RUN_ID }),
      )
      .mockResolvedValueOnce(
        sseResponse(['id: 2\nevent: complete\ndata: {"message_id":9}\n\n'], { runId: RUN_ID }),
      );
    const { result } = renderHook(() => useStreaming());
    await act(async () => {
      await result.current.startStream(1, "hi", {});
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect("mode" in bodyOf(vi.mocked(fetch).mock.calls[0])).toBe(false);
    expect("mode" in bodyOf(vi.mocked(fetch).mock.calls[1])).toBe(false);
  });

  it("a later explicit send after a mode change gets a new Idempotency-Key and its own mode", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse(['event: complete\ndata: {"message_id":1,"citations":[]}\n\n']),
    );
    const { result } = renderHook(() => useStreaming());
    await act(async () => {
      await result.current.startStream(1, "one", undefined, {}, "chat");
    });
    await act(async () => {
      await result.current.startStream(1, "two", undefined, {}, "multi_agent");
    });
    const call0 = vi.mocked(fetch).mock.calls[0];
    const call1 = vi.mocked(fetch).mock.calls[1];
    const key0 = (call0[1] as RequestInit & { headers: Record<string, string> }).headers["Idempotency-Key"];
    const key1 = (call1[1] as RequestInit & { headers: Record<string, string> }).headers["Idempotency-Key"];
    expect(key0).not.toBe(key1);
    expect(bodyOf(call0).mode).toBe("chat");
    expect(bodyOf(call1).mode).toBe("multi_agent");
  });
});

// ── MP-3C3D4C — bounded deep-research progress dispatch ──────

describe("MP-3C3D4C — research_progress SSE dispatch", () => {
  it("dispatches a bounded progress code to onResearchProgress", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse([
        'id: 1\nevent: research_progress\ndata: {"code":"research_searching"}\n\n',
        'id: 2\nevent: research_progress\ndata: {"code":"research_fetching"}\n\n',
        'id: 3\nevent: complete\ndata: {"message_id":1,"citations":[]}\n\n',
      ], { runId: RUN_ID }),
    );
    const { result } = renderHook(() => useStreaming());
    const onResearchProgress = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", undefined, { onResearchProgress }, "deep_research");
    });
    expect(onResearchProgress.mock.calls.map((c) => c[0])).toEqual([
      "research_searching", "research_fetching",
    ]);
  });

  it("an unrecognized event name is safely ignored (no callback required)", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse([
        'event: some_future_event\ndata: {"x":1}\n\n',
        'event: complete\ndata: {"message_id":1,"citations":[]}\n\n',
      ]),
    );
    const { result } = renderHook(() => useStreaming());
    const onComplete = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", { onComplete });
    });
    expect(onComplete).toHaveBeenCalledOnce();
  });
});

// ── MP-3C3D5C — bounded multi-agent progress dispatch ────────

describe("MP-3C3D5C — agent_progress SSE dispatch", () => {
  it("dispatches bounded progress codes to onAgentProgress in order", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse([
        'id: 1\nevent: agent_progress\ndata: {"code":"agent_researching"}\n\n',
        'id: 2\nevent: agent_progress\ndata: {"code":"agent_research_complete"}\n\n',
        'id: 3\nevent: agent_progress\ndata: {"code":"agent_solving"}\n\n',
        'id: 3\nevent: agent_progress\ndata: {"code":"agent_solving"}\n\n',
        'id: 4\nevent: agent_progress\ndata: {"code":"agent_solution_complete"}\n\n',
        'id: 5\nevent: agent_progress\ndata: {"code":"agent_reviewing"}\n\n',
        'id: 6\nevent: agent_progress\ndata: {"code":"agent_finalizing"}\n\n',
        'id: 7\nevent: complete\ndata: {"message_id":1,"citations":[]}\n\n',
      ], { runId: RUN_ID }),
    );
    const { result } = renderHook(() => useStreaming());
    const onAgentProgress = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", undefined, { onAgentProgress }, "multi_agent");
    });
    expect(onAgentProgress.mock.calls.map((c) => c[0])).toEqual([
      "agent_researching", "agent_research_complete",
      "agent_solving", "agent_solution_complete",
      "agent_reviewing", "agent_finalizing",
    ]);
  });

  it("ignores unknown and malformed progress without invoking the callback", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse([
        'event: agent_progress\ndata: {"code":"agent_some_future_code"}\n\n',
        ...[null, [], {}, { code: 1 }, { code: " agent_solving" }, { code: {} }].map(
          (data) => `event: agent_progress\ndata: ${JSON.stringify(data)}\n\n`,
        ),
        'event: complete\ndata: {"message_id":1,"citations":[]}\n\n',
      ]),
    );
    const { result } = renderHook(() => useStreaming());
    const onAgentProgress = vi.fn();
    const onComplete = vi.fn();
    await act(async () => {
      await result.current.startStream(1, "hi", { onAgentProgress, onComplete });
    });
    expect(onAgentProgress).not.toHaveBeenCalled();
    expect(onComplete).toHaveBeenCalledOnce();
  });
});
