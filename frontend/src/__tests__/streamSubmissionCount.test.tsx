import { StrictMode, useRef } from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStreaming } from "../useStreaming";

/** D6B-R3 Part 1: classify EVERY fetch issued by one explicit user-like send.
 *  Records pathname, method, ordinal, a hash of the Idempotency-Key (never the
 *  raw value) and the returned X-Run-Id, so an auth refresh can never be
 *  mistaken for a stream submission. */

const RUN_ID = "11111111-2222-4333-8444-555555555555";

const FRAMES = [
  `event: start\ndata: {"run_id":"${RUN_ID}"}\n\n` +
    `id: 3\nevent: run_status\ndata: {"status":"running"}\n\n`,
  `id: 4\nevent: token\ndata: {"token":"నమస్కారం🌩️D6BSMOKE "}\n\n`,
  `id: 5\nevent: token\ndata: {"token":"రెండవ భాగం END"}\n\n`,
  `id: 6\nevent: complete\ndata: {"message_id":2,"citations":[],"sources_used":false,"memories_used":false}\n\n`,
];

const EXPECTED = "నమస్కారం🌩️D6BSMOKE రెండవ భాగం END";

interface Rec { ordinal: number; method: string; path: string; idemHash: string; runId: string; }
const seen: Rec[] = [];

function hash(input: string): string {
  let h = 2166136261;
  for (let i = 0; i < input.length; i += 1) { h ^= input.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(16).padStart(8, "0");
}

function streamResponse(): Response {
  const enc = new TextEncoder();
  let i = 0;
  return new Response(new ReadableStream<Uint8Array>({
    pull(c) { if (i >= FRAMES.length) { c.close(); return; } c.enqueue(enc.encode(FRAMES[i])); i += 1; }
  }), { status: 200, headers: { "Content-Type": "text/event-stream", "X-Run-Id": RUN_ID } });
}

function Harness() {
  const { streamedContent, startStream } = useStreaming();
  const sent = useRef(false);
  return (
    <div>
      <div data-testid="draft">{streamedContent}</div>
      <button data-testid="go" onClick={() => { if (sent.current) return; sent.current = true; void startStream(1, "hello", {} as never); }}>go</button>
    </div>
  );
}

describe("D6B-R3 fetch classification", () => {
  beforeEach(() => {
    seen.length = 0;
    let n = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      n += 1;
      const url = typeof input === "string" ? input : String((input as { url?: string })?.url ?? input);
      const path = url.replace(/^https?:\/\/[^/]+/, "");
      const hdrs = (init?.headers ?? {}) as Record<string, string>;
      const idem = hdrs["Idempotency-Key"] ?? "";
      const isStream = path.indexOf("/messages/stream") !== -1;
      seen.push({ ordinal: n, method: init?.method ?? "GET", path,
                  idemHash: idem ? hash(idem) : "-", runId: isStream ? RUN_ID : "-" });
      if (isStream) return streamResponse();
      return new Response(JSON.stringify({ detail: "no session" }), { status: 401, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("one explicit send issues exactly one /messages/stream POST", async () => {
    render(<StrictMode><Harness /></StrictMode>);
    fireEvent.click(screen.getByTestId("go"));
    await waitFor(() => { expect(screen.getByTestId("draft").textContent).toBe(EXPECTED); });

    const streams = seen.filter((s) => s.path.indexOf("/messages/stream") !== -1);
    // eslint-disable-next-line no-console
    console.log("REQUEST TABLE " + JSON.stringify(seen));
    expect({ streamPosts: streams.length, distinctIdem: new Set(streams.map((s) => s.idemHash)).size })
      .toEqual({ streamPosts: 1, distinctIdem: 1 });
  });
});
