/**
 * Incremental Server-Sent-Events parser (Phase 1B.2R MP-3C3C).
 *
 * Handles both the legacy `/messages/stream` wire (no `id:` lines) and the
 * MP-3C3B durable wire (`id: <run_seq>` lines) identically. Correct across
 * arbitrary network chunk boundaries:
 *
 *  - `event:` / `data:` / `id:` fields, `:comment` / heartbeat lines
 *  - CRLF and LF line/frame separators
 *  - multiple frames in one chunk; one frame split across many chunks
 *  - multi-byte UTF-8 code points split across byte chunks (TextDecoder stream)
 *  - a final buffered frame flushed at stream close, if complete
 *
 * The buffer of an incomplete frame is bounded. Partial data is never surfaced
 * as an event (callers JSON-parse `data` themselves and skip on failure).
 * Numeric-id validation beyond "non-negative safe integer" (monotonicity,
 * dedupe) is the consumer's job — this parser is stateless about ordering.
 */

export interface SSEEvent {
  /** event type; "" for a default `message` event */
  event: string;
  /** raw data payload (data: lines joined with "\n"); may be "" */
  data: string;
  /** present only when the frame carried a valid non-negative-integer `id:` */
  id?: number;
}

/** Bounded incomplete-frame buffer (1 MiB) — a hostile/broken stream cannot
 *  grow memory without bound. */
export const MAX_SSE_BUFFER = 1 << 20;

export class SSEStreamParser {
  private buf = "";
  private decoder = new TextDecoder("utf-8", { fatal: false });

  /** Feed a raw byte chunk; returns any newly-completed frames. */
  feedBytes(chunk: Uint8Array): SSEEvent[] {
    return this.feed(this.decoder.decode(chunk, { stream: true }));
  }

  /** Feed decoded text (test seam); returns any newly-completed frames. */
  feed(text: string): SSEEvent[] {
    if (text) this.buf += text;
    if (this.buf.length > MAX_SSE_BUFFER) {
      const norm = this.buf.replace(/\r\n/g, "\n");
      const lastBoundary = norm.lastIndexOf("\n\n");
      this.buf = lastBoundary >= 0 ? norm.slice(lastBoundary + 2) : norm.slice(-4096);
    }
    const norm = this.buf.replace(/\r\n/g, "\n");
    const parts = norm.split("\n\n");
    this.buf = parts.pop() ?? "";              // trailing incomplete frame stays buffered
    const out: SSEEvent[] = [];
    for (const p of parts) {
      const ev = parseFrame(p);
      if (ev) out.push(ev);
    }
    return out;
  }

  /** Stream closed: flush the decoder tail + one trailing complete frame. */
  flush(): SSEEvent[] {
    this.buf += this.decoder.decode();         // emit any pending multi-byte tail
    const tail = this.buf.replace(/\r\n/g, "\n").replace(/\n+$/, "");
    this.buf = "";
    if (!tail) return [];
    const ev = parseFrame(tail);
    return ev ? [ev] : [];
  }

  /** Current unparsed remainder (test/debug aid). */
  get remainder(): string {
    return this.buf;
  }
}

function parseFrame(raw: string): SSEEvent | null {
  let event = "";
  let idRaw: string | null = null;
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line === "") continue;
    if (line.startsWith(":")) continue;                       // comment / heartbeat
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);        // SSE: strip one leading SP
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
    else if (field === "id") idRaw = value;
    // `retry:` and unknown fields are ignored
  }
  if (event === "" && dataLines.length === 0) return null;
  const ev: SSEEvent = { event, data: dataLines.join("\n") };
  if (idRaw !== null) {
    const n = Number(idRaw.trim());
    if (Number.isInteger(n) && n >= 0 && n <= Number.MAX_SAFE_INTEGER) ev.id = n;
  }
  return ev;
}

// ── legacy compatibility shim ──────────────────────────────────────────
//
// `useStreaming.ts` historically exported `parseSSEBuffer(buffer) ->
// {events:{type,data}[], remainder}` and several tests call it directly. Keep
// that exact behaviour on top of the one parser above: parse each frame, then
// JSON-parse `data` (drop the frame on failure), map `event -> type`.

export interface LegacyParsedEvent {
  type: string;
  data: Record<string, unknown>;
}

export function parseSSEBuffer(buffer: string): {
  events: LegacyParsedEvent[];
  remainder: string;
} {
  const p = new SSEStreamParser();
  const raw = p.feed(buffer);
  const events: LegacyParsedEvent[] = [];
  for (const ev of raw) {
    try {
      const data = ev.data ? JSON.parse(ev.data) : {};
      if (data && typeof data === "object") {
        events.push({ type: ev.event, data: data as Record<string, unknown> });
      }
    } catch {
      /* invalid JSON — drop the frame, exactly as the old parser did */
    }
  }
  return { events, remainder: p.remainder };
}
