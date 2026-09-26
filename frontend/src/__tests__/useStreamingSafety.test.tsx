import { StrictMode, useRef } from "react";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStreaming } from "../useStreaming";

/** D6B-R4 Parts 1: StrictMode stale-async safety matrix (A-D) against the REAL
 *  hook and the REAL SSE parser. Every start is an explicit user-like click,
 *  never an unguarded useEffect, because StrictMode intentionally runs effects
 *  twice. */

const RUN_ID = "11111111-2222-4333-8444-555555555555";
const FR = [
  `event: start\ndata: {"run_id":"${RUN_ID}"}\n\n` +
    `id: 3\nevent: run_status\ndata: {"status":"running"}\n\n`,
  `id: 4\nevent: token\ndata: {"token":"AAAA"}\n\n`,
  `id: 5\nevent: token\ndata: {"token":"BBBB"}\n\n`,
  `id: 6\nevent: complete\ndata: {"message_id":2,"citations":[],"sources_used":false,"memories_used":false}\n\n`,
];
const FULL = "AAAABBBB";

type Ctl = { push: (s: string) => void; close: () => void };

function harnessStream(): { resp: Response; ctl: Ctl } {
  const enc = new TextEncoder();
  let c: ReadableStreamDefaultController<Uint8Array> | null = null;
  const stream = new ReadableStream<Uint8Array>({ start(controller) { c = controller; } });
  const resp = new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream", "X-Run-Id": RUN_ID } });
  return { resp, ctl: { push: (s) => c?.enqueue(enc.encode(s)), close: () => c?.close() } };
}

function completedResponse(): Response {
  const enc = new TextEncoder();
  let i = 0;
  return new Response(new ReadableStream<Uint8Array>({
    pull(c) { if (i >= FR.length) { c.close(); return; } c.enqueue(enc.encode(FR[i])); i += 1; }
  }), { status: 200, headers: { "Content-Type": "text/event-stream", "X-Run-Id": RUN_ID } });
}

let calls: { path: string; idem: string }[] = [];

function Harness({ cb }: { cb: Record<string, unknown> }) {
  const { streamedContent, isStreaming, startStream } = useStreaming();
  const sent = useRef(false);
  return (
    <div>
      <div data-testid="draft">{streamedContent}</div>
      <div data-testid="streaming">{String(isStreaming)}</div>
      <button data-testid="go" onClick={() => { if (sent.current) return; sent.current = true; void startStream(1, "hello", cb as never); }}>go</button>
    </div>
  );
}

function record(input: unknown, init?: RequestInit): string {
  const url = typeof input === "string" ? input : String((input as { url?: string })?.url ?? input);
  const path = url.replace(/^https?:\/\/[^/]+/, "");
  const h = (init?.headers ?? {}) as Record<string, string>;
  if (path.indexOf("/messages/stream") !== -1) calls.push({ path, idem: h["Idempotency-Key"] ?? "" });
  return path;
}

describe("D6B-R4 StrictMode stale-async safety matrix", () => {
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("A: unmount during an active stream aborts once and never completes", async () => {
    const { resp, ctl } = harnessStream();
    vi.stubGlobal("fetch", vi.fn(async (i: unknown, init?: RequestInit) => {
      const path = record(i, init);
      if (path.indexOf("/messages/stream") !== -1) return resp;
      return new Response("{}", { status: 401 });
    }));
    const abortSpy = vi.spyOn(AbortController.prototype, "abort");
    const onComplete = vi.fn(); const onCancelled = vi.fn();
    const view = render(<Harness cb={{ onComplete, onCancelled }} />);
    fireEvent.click(screen.getByTestId("go"));
    await waitFor(() => { expect(calls.length).toBe(1); });
    await act(async () => { ctl.push(FR[1]); });
    await waitFor(() => { expect(screen.getByTestId("draft").textContent).not.toBe(""); });
    view.unmount();
    await act(async () => { ctl.push(FR[2]); ctl.close(); });
    expect(abortSpy).toHaveBeenCalledTimes(1);
    expect(onComplete).not.toHaveBeenCalled();
    expect(calls.length).toBe(1);
  });

  it("B: a late stale resolution cannot mutate the reactivated hook", async () => {
    const { resp, ctl } = harnessStream();
    let release: ((r: Response) => void) | null = null;
    vi.stubGlobal("fetch", vi.fn(async (i: unknown, init?: RequestInit) => {
      const path = record(i, init);
      if (path.indexOf("/messages/stream") !== -1) {
        return await new Promise<Response>((res) => { release = res; });   // deliberately ignores abort
      }
      return new Response("{}", { status: 401 });
    }));
    const onComplete = vi.fn(); const onCancelled = vi.fn();
    const view = render(<Harness cb={{ onComplete, onCancelled }} />);
    fireEvent.click(screen.getByTestId("go"));
    await waitFor(() => { expect(calls.length).toBe(1); });
    view.unmount();
    await act(async () => { release!(resp); ctl.push(FR[1]); ctl.push(FR[3]); ctl.close(); });
    expect(onComplete).not.toHaveBeenCalled();
    expect(onCancelled.mock.calls.length).toBeLessThanOrEqual(1);
    expect(calls.length).toBe(1);                       // no extra submission
  });

  it("C: a fresh mount after unmount works and uses a different idempotency key", async () => {
    vi.stubGlobal("fetch", vi.fn(async (i: unknown, init?: RequestInit) => {
      const path = record(i, init);
      if (path.indexOf("/messages/stream") !== -1) return completedResponse();
      return new Response("{}", { status: 401 });
    }));
    const first = render(<Harness cb={{}} />);
    fireEvent.click(screen.getByTestId("go"));
    await waitFor(() => { expect(screen.getByTestId("draft").textContent).toBe(FULL); });
    first.unmount();

    const onComplete = vi.fn();
    render(<Harness cb={{ onComplete }} />);
    fireEvent.click(screen.getByTestId("go"));
    await waitFor(() => { expect(onComplete).toHaveBeenCalledTimes(1); });
    expect(calls.length).toBe(2);
    expect(calls[0].idem).not.toBe("");
    expect(calls[0].idem).not.toBe(calls[1].idem);
  });

  it("D: StrictMode setup/cleanup/setup still submits exactly once and completes once", async () => {
    vi.stubGlobal("fetch", vi.fn(async (i: unknown, init?: RequestInit) => {
      const path = record(i, init);
      if (path.indexOf("/messages/stream") !== -1) return completedResponse();
      return new Response("{}", { status: 401 });
    }));
    const onComplete = vi.fn();
    render(<StrictMode><Harness cb={{ onComplete }} /></StrictMode>);
    fireEvent.click(screen.getByTestId("go"));
    await waitFor(() => { expect(screen.getByTestId("draft").textContent).not.toBe(""); });
    await waitFor(() => { expect(onComplete).toHaveBeenCalledTimes(1); });
    expect(calls.length).toBe(1);
    expect((onComplete.mock.calls[0][0] as { content: string }).content).toBe(FULL);
    await waitFor(() => { expect(screen.getByTestId("streaming").textContent).toBe("false"); });
  });
});
