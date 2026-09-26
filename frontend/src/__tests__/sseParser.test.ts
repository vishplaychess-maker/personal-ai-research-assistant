/**
 * MP-3C3C — dual-compatible incremental SSE parser.
 *
 * Covers the cases the durable + legacy wires actually exercise: multi-frame
 * chunks, split frames, CRLF vs LF, `id:` capture + validation, split UTF-8
 * code points, `:comment` heartbeats, unknown events, bounded buffer, and the
 * legacy `parseSSEBuffer` shim staying byte-identical for id-less frames.
 */
import { describe, it, expect } from "vitest";
import {
  SSEStreamParser,
  parseSSEBuffer,
  MAX_SSE_BUFFER,
} from "../lib/sseParser";

const enc = new TextEncoder();

describe("SSEStreamParser — framing", () => {
  it("parses multiple frames in one feed", () => {
    const p = new SSEStreamParser();
    const out = p.feed(
      "event: token\ndata: {\"token\":\"a\"}\n\n" +
        "event: token\ndata: {\"token\":\"b\"}\n\n",
    );
    expect(out.map((e) => e.event)).toEqual(["token", "token"]);
    expect(out.map((e) => JSON.parse(e.data).token)).toEqual(["a", "b"]);
    expect(p.remainder).toBe("");
  });

  it("holds an incomplete trailing frame until the rest arrives", () => {
    const p = new SSEStreamParser();
    expect(p.feed("event: token\ndata: {\"tok")).toEqual([]);
    const out = p.feed("en\":\"hi\"}\n\n");
    expect(out).toHaveLength(1);
    expect(JSON.parse(out[0].data).token).toBe("hi");
  });

  it("treats CRLF frame + line separators the same as LF", () => {
    const p = new SSEStreamParser();
    const out = p.feed("event: token\r\ndata: {\"token\":\"x\"}\r\n\r\n");
    expect(out).toHaveLength(1);
    expect(out[0].event).toBe("token");
    expect(JSON.parse(out[0].data).token).toBe("x");
  });

  it("captures and validates a numeric id: line", () => {
    const p = new SSEStreamParser();
    const [ev] = p.feed("id: 7\nevent: token\ndata: {\"token\":\"x\"}\n\n");
    expect(ev.id).toBe(7);
  });

  it("drops a non-integer / negative id but keeps the frame", () => {
    const p = new SSEStreamParser();
    const [a] = p.feed("id: abc\nevent: token\ndata: {}\n\n");
    const [b] = p.feed("id: -3\nevent: token\ndata: {}\n\n");
    expect(a.id).toBeUndefined();
    expect(b.id).toBeUndefined();
    expect(a.event).toBe("token");
  });

  it("ignores `:` comment / heartbeat lines", () => {
    const p = new SSEStreamParser();
    const out = p.feed(": keep-alive\n\nevent: token\ndata: {\"token\":\"x\"}\n\n");
    expect(out).toHaveLength(1);
    expect(out[0].event).toBe("token");
  });

  it("surfaces unknown event types verbatim (consumer decides)", () => {
    const p = new SSEStreamParser();
    const [ev] = p.feed("event: something_new\ndata: {\"k\":1}\n\n");
    expect(ev.event).toBe("something_new");
  });

  it("flush() emits a final complete frame with no trailing blank line", () => {
    const p = new SSEStreamParser();
    expect(p.feed("event: complete\ndata: {\"message_id\":5}")).toEqual([]);
    const out = p.flush();
    expect(out).toHaveLength(1);
    expect(JSON.parse(out[0].data).message_id).toBe(5);
  });

  it("reassembles a UTF-8 code point split across byte chunks", () => {
    const p = new SSEStreamParser();
    // "€" = e2 82 ac ; feed the frame with the 3 bytes split 1 + 2
    const full = enc.encode("event: token\ndata: {\"token\":\"€\"}\n\n");
    const cut = full.indexOf(0xe2);
    expect(p.feedBytes(full.slice(0, cut + 1))).toEqual([]);
    const out = p.feedBytes(full.slice(cut + 1));
    expect(out).toHaveLength(1);
    expect(JSON.parse(out[0].data).token).toBe("€");
  });

  it("bounds the incomplete-frame buffer under a hostile no-boundary stream", () => {
    const p = new SSEStreamParser();
    p.feed("x".repeat(MAX_SSE_BUFFER + 5000));
    expect(p.remainder.length).toBeLessThanOrEqual(MAX_SSE_BUFFER);
  });
});

describe("parseSSEBuffer — legacy shim unchanged", () => {
  it("maps event->type and JSON-parses data for id-less frames", () => {
    const { events, remainder } = parseSSEBuffer(
      "event: start\ndata: {\"session_id\":1}\n\n" +
        "event: token\ndata: {\"token\":\"hi\"}\n\n",
    );
    expect(events).toEqual([
      { type: "start", data: { session_id: 1 } },
      { type: "token", data: { token: "hi" } },
    ]);
    expect(remainder).toBe("");
  });

  it("drops a frame with invalid JSON, exactly like the old parser", () => {
    const { events } = parseSSEBuffer("event: token\ndata: {bad}\n\n");
    expect(events).toEqual([]);
  });

  it("returns the incomplete trailing frame as remainder", () => {
    const { events, remainder } = parseSSEBuffer(
      "event: start\ndata: {\"session_id\":1}\n\nevent: tok",
    );
    expect(events).toHaveLength(1);
    expect(remainder).toContain("event: tok");
  });
});
