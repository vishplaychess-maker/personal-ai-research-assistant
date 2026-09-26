import { StrictMode, useRef } from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStreaming } from "../useStreaming";

/** D6B-R2 regression: the REAL useStreaming hook, inside React.StrictMode,
 *  must consume and render a durable /messages/stream response.
 *
 *  Root cause it locks down: the hook's unmount cleanup sets
 *  `unmountedRef.current = true`, but the effect body never resets it. React
 *  StrictMode (development) mounts, unmounts and re-mounts every component, so
 *  after that simulated unmount `unmountedRef.current` stayed true forever and
 *  `runPass` returned early — before `response.body.getReader()` — leaving the
 *  composer stuck in the streaming state and the draft permanently empty.
 */

const RUN_ID = "11111111-2222-4333-8444-555555555555";

// The exact observed durable wire, split to cross chunk boundaries.
const FRAMES = [
  `event: start\ndata: {"run_id":"${RUN_ID}"}\n\n` +
    `id: 3\nevent: run_status\ndata: {"status":"running"}\n\n`,
  `id: 4\nevent: token\ndata: {"token":"నమస్కారం🌩️D6BSMOKE "}\n\n`,
  `id: 5\nevent: token\ndata: {"token":"రెండవ భాగం END"}\n\n`,
  `id: 6\nevent: complete\ndata: {"message_id":2,"citations":[],"sources_used":false,"memories_used":false}\n\n`,
];

const EXPECTED = "నమస్కారం🌩️D6BSMOKE రెండవ భాగం END";

function Harness({ onComplete }: { onComplete: (r: unknown) => void }) {
  const { isStreaming, streamedContent, startStream } = useStreaming();
  const sent = useRef(false);
  return (
    <div>
      <div data-testid="draft">{streamedContent}</div>
      <div data-testid="streaming">{String(isStreaming)}</div>
      <button
        data-testid="go"
        onClick={() => {
          if (sent.current) return;
          sent.current = true;
          void startStream(1, "hello", { onComplete } as never);
        }}
      >
        go
      </button>
    </div>
  );
}

function makeResponse(): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i >= FRAMES.length) { controller.close(); return; }
      controller.enqueue(encoder.encode(FRAMES[i]));
      i += 1;
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream", "X-Run-Id": RUN_ID },
  });
}

describe("useStreaming under React.StrictMode (D6B-R2)", () => {
  beforeEach(() => {
    // Distinguish the auth refresh from the stream submission. Answering EVERY
    // URL with the stream Response was a harness artifact that made the refresh
    // request look like a second stream POST (D6B-R3/R4).
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      const url = typeof input === "string" ? input : String((input as { url?: string })?.url ?? input);
      if (url.indexOf("/messages/stream") !== -1) return makeResponse();
      return new Response("{}", { status: 401 });
    }));
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("renders the streamed draft progressively and completes once", async () => {
    const onComplete = vi.fn();
    render(
      <StrictMode>
        <Harness onComplete={onComplete} />
      </StrictMode>,
    );

    fireEvent.click(screen.getByTestId("go"));

    // Progressive: the draft must become non-empty BEFORE the terminal frame.
    await waitFor(() => {
      expect(screen.getByTestId("draft").textContent).not.toBe("");
    });

    // Completion contract: `finish()` intentionally clears the transient draft
    // (the real App renders the persisted Message instead), so the authoritative
    // assertion is the content handed to onComplete.
    await waitFor(() => {
      expect(onComplete).toHaveBeenCalled();
    });
    // Exactly ONE /messages/stream submission (the other fetch is the auth
    // refresh that precedes it) and exactly one terminal handling.
    const all = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map((c) => String(c[0]));
    // eslint-disable-next-line no-console
    const streamCalls = all.filter((u) => u.indexOf("/messages/stream") !== -1).length;
    expect({ streamCalls, completes: onComplete.mock.calls.length }).toEqual({ streamCalls: 1, completes: 1 });
    expect((onComplete.mock.calls[0][0] as { content: string }).content).toBe(EXPECTED);
    expect(screen.getByTestId("streaming").textContent).toBe("false");
    // No reconnect: exactly one stream submission. (A raw global fetch count would
    // also include the auth refresh and is NOT a valid duplicate check.)
    expect(streamCalls).toBe(1);
  });
});
