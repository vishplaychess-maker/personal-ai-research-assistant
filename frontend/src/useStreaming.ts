/**
 * useStreaming — React hook for SSE-based token streaming.
 *
 * Connects to POST /api/sessions/{id}/messages/stream and parses SSE events.
 *
 * Dual mode (Phase 1B.2R MP-3C3C):
 *  - LEGACY  (backend cutover flag OFF): the response carries no `X-Run-Id`.
 *    Behaviour is exactly as before — `start`/`token`/`plan`/`agent_status`/
 *    `complete`/`error`/`cancelled`, `cancelStream()` aborts and yields
 *    `onCancelled`, a dropped connection ends the stream.
 *  - DURABLE (flag ON): the response carries `X-Run-Id`. The hook captures the
 *    server-owned run id, tracks the SSE `id:` cursor, dedupes replayed events,
 *    reconnects (re-POST with the SAME Idempotency-Key + `Last-Event-ID`) with
 *    bounded exponential backoff + jitter, exposes `cancelDurableRun()` (POST
 *    /api/runs/{id}/cancel — the browser disconnect does NOT cancel a durable
 *    run), and surfaces a redacted `tool_approval_required` via
 *    `onApprovalRequired`.
 */

import { useState, useRef, useCallback, useEffect } from "react";
import { getCsrfToken, getAuthHeadersAsync } from "./auth";
import { apiUrl } from "./apiBase";
import { SSEStreamParser } from "./lib/sseParser";
import { API } from "./api";
import type { BackendChatMode } from "./types";

// re-export the legacy pure parser for existing callers/tests
export { parseSSEBuffer } from "./lib/sseParser";

// ── Types ─────────────────────────────────────────────────

export interface StreamResult {
  messageId: number;
  content: string;
  citations: Array<Record<string, unknown>>;
  sourcesUsed: boolean;
  memoriesUsed: boolean;
}

export interface StreamError {
  code: string;
  detail: string;
}

export type DurablePhase =
  | "streaming"
  | "waiting_approval"
  | "reconnecting"
  | "resuming"
  | "cancelling"
  | "done";

export interface StreamCallbacks {
  onStart?: () => void;
  onToken?: (token: string) => void;
  onPlan?: (steps: Array<Record<string, unknown>>) => void;
  onAgentStatus?: (data: { agent: string; message: string }) => void;
  onComplete?: (result: StreamResult) => void;
  onError?: (error: StreamError) => void;
  onCancelled?: () => void;
  /** legacy dormant tool-approval shape */
  onToolApproval?: (data: { tool: string; args: unknown; server: string }) => void;
  /** MP-3C3C durable additions (all optional) */
  onRunId?: (runId: string) => void;
  onApprovalRequired?: (data: { approvalId: string; toolId?: string }) => void;
  onPhase?: (phase: DurablePhase) => void;
  /** MP-3C3D4C — bounded durable deep-research progress code (server-owned
   *  closed set: "research_searching"|"research_fetching"|
   *  "research_sources_ready"|"research_synthesizing"). Never a raw query,
   *  URL, title, or error. Omit to ignore — an unrecognized/older build
   *  already safely ignores this SSE event by name. */
  onResearchProgress?: (code: string) => void;
  /** MP-3C3D5C — bounded durable multi-agent progress code (server-owned
   *  closed set: "agent_researching"|"agent_research_complete"|
   *  "agent_solving"|"agent_solution_complete"|"agent_reviewing"|
   *  "agent_finalizing"). Never a raw role name, artifact text, or
   *  citation. Omit to ignore — an unrecognized/older build already safely
   *  ignores this SSE event by name. Unknown/malformed codes are ignored. */
  onAgentProgress?: (code: string) => void;
}

// ── reconnect bounds (durable only) ───────────────────────

const RECONNECT_MAX_ATTEMPTS = 6;
const RECONNECT_MAX_ELAPSED_MS = 60_000;
const RECONNECT_BASE_MS = 500;
const RECONNECT_CAP_MS = 8_000;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const AGENT_PROGRESS_CODES = new Set([
  "agent_researching", "agent_research_complete", "agent_solving",
  "agent_solution_complete", "agent_reviewing", "agent_finalizing",
]);

function newIdempotencyKey(): string {
  const c: Crypto | undefined = typeof crypto !== "undefined" ? crypto : undefined;
  try {
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
  } catch { /* fall through */ }
  if (!c || typeof c.getRandomValues !== "function") {
    // fail safely: never fall back to Math.random for an idempotency key
    throw new Error("Secure random number generation is unavailable in this environment");
  }
  // RFC-4122 v4 fallback
  const b = new Uint8Array(16);
  c.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function backoffMs(attempt: number): number {
  const raw = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_CAP_MS);
  const jitter = raw * 0.3 * (Math.random() * 2 - 1);
  return Math.max(0, Math.round(raw + jitter));
}

// ── Hook ──────────────────────────────────────────────────

export function useStreaming() {
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamedContent, setStreamedContent] = useState("");
  const [durableRunId, setDurableRunId] = useState<string | null>(null);
  const [phase, setPhase] = useState<DurablePhase | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const contentRef = useRef("");
  const cancelledRef = useRef(false);
  const unmountedRef = useRef(false);

  // durable state (kept in refs — reconnect/cancel callbacks read the latest)
  const runIdRef = useRef<string | null>(null);
  const cursorRef = useRef(0);                 // last ACCEPTED numeric event id
  const idemKeyRef = useRef<string | null>(null);
  const submissionRef = useRef<{ sessionId: number; message: string; imageUrl?: string; mode?: BackendChatMode } | null>(null);
  const terminalRef = useRef(false);
  const attemptsRef = useRef(0);
  const startedAtRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const readerActiveRef = useRef(false);
  const awaitingApprovalRef = useRef(false);   // paused on a tool_approval_required gate
  const cbRef = useRef<StreamCallbacks>({});
  const startPendingRef = useRef(false);       // guards synchronous double-submit

  useEffect(() => {
    // MP-3C3D6B-R2: React StrictMode (development) mounts, unmounts and
    // re-mounts every component, so this cleanup runs once against a component
    // that is still live. Without resetting the flag here the hook stayed
    // permanently "unmounted" and `runPass` returned before
    // `response.body.getReader()`, so the durable stream was never consumed and
    // the composer stayed stuck in its streaming state.
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
      cancelledRef.current = true;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
    };
  }, []);

  // ── one fetch+read pass (initial or a reconnect) ──────────

  const runPass = useCallback(async (isReconnect: boolean): Promise<void> => {
    const cb = cbRef.current;
    const sub = submissionRef.current;
    const key = idemKeyRef.current;
    if (!sub || !key) return;

    const controller = new AbortController();
    abortRef.current = controller;

    let response: Response;
    try {
      const headers: Record<string, string> = { "Content-Type": "application/json", "Idempotency-Key": key };
      const auth = await getAuthHeadersAsync();
      if (auth.Authorization) headers.Authorization = auth.Authorization;
      const csrf = getCsrfToken();
      if (csrf) headers["X-CSRF-Token"] = csrf;
      if (isReconnect && cursorRef.current > 0) {
        headers["Last-Event-ID"] = String(cursorRef.current);
      }
      const url = apiUrl(`/api/sessions/${sub.sessionId}/messages/stream`)
        + (isReconnect && cursorRef.current > 0 ? `?after_seq=${cursorRef.current}` : "");
      // MP-3C3D3B: `sub.mode` is a snapshot taken once in `startStream` and
      // never re-read from live selector state — every reconnect/resume of
      // THIS logical send reuses the identical body. `undefined` (Auto) is
      // dropped by JSON.stringify, never sent as `null` or `"auto"`.
      response = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify({ message: sub.message, image_url: sub.imageUrl, mode: sub.mode }),
        credentials: "include",
        signal: controller.signal,
      });
    } catch (err) {
      abortRef.current = null;
      if (cancelledRef.current || unmountedRef.current
        || (err instanceof DOMException && err.name === "AbortError")) {
        if (!runIdRef.current && !terminalRef.current) { cb.onCancelled?.(); }
        finish();
        return;
      }
      if (runIdRef.current && !terminalRef.current) { scheduleReconnect(); return; }
      cb.onError?.({ code: "NETWORK_ERROR", detail: err instanceof Error ? err.message : "Network request failed" });
      finish();
      return;
    }

    if (cancelledRef.current || unmountedRef.current) {
      if (!runIdRef.current) cb.onCancelled?.();
      finish();
      return;
    }

    // durable run identity — validate before trusting it
    const rid = response.headers.get("X-Run-Id");
    if (rid && UUID_RE.test(rid.trim())) {
      const v = rid.trim();
      if (!runIdRef.current) {
        runIdRef.current = v;
        setDurableRunId(v);
        cb.onRunId?.(v);
      }
    }

    if (!response.ok) {
      abortRef.current = null;
      const body = await response.json().catch(() => ({ detail: `Request failed with status ${response.status}` }));
      const code = response.status === 404 ? "SESSION_NOT_FOUND"
        : response.status === 422 ? "VALIDATION_ERROR"
        : response.status === 429 ? "RATE_LIMITED"
        : response.status === 503 ? "DURABLE_UNAVAILABLE"
        : "HTTP_ERROR";
      // a non-2xx on a reconnect of an already-identified run: stop, surface honestly
      terminalRef.current = true;
      cb.onError?.({ code, detail: body.detail || `Request failed with status ${response.status}` });
      finish();
      return;
    }

    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      reader = response.body?.getReader();
    } catch {
      reader = undefined;                     // body already disturbed/locked
    }
    if (!reader) {
      // fail closed: on a durable run let the bounded reconnect try again,
      // otherwise surface an honest error. Never leave the pass hanging.
      if (runIdRef.current && !terminalRef.current && !cancelledRef.current) {
        scheduleReconnect();
        return;
      }
      cb.onError?.({ code: "READ_ERROR", detail: "Failed to read response stream" });
      finish();
      return;
    }

    readerActiveRef.current = true;
    const parser = new SSEStreamParser();
    if (isReconnect) { setPhaseSafe("streaming"); }

    try {
      while (true) {
        if (cancelledRef.current || unmountedRef.current) {
          try { await reader.cancel(); } catch { /* noop */ }
          readerActiveRef.current = false;
          if (!runIdRef.current && !terminalRef.current) cb.onCancelled?.();
          finish();
          return;
        }
        const { done, value } = await reader.read();
        if (cancelledRef.current || unmountedRef.current) {
          readerActiveRef.current = false;
          if (!runIdRef.current && !terminalRef.current) cb.onCancelled?.();
          finish();
          return;
        }
        if (done) break;

        const frames = parser.feedBytes(value);
        for (const f of frames) {
          const stop = dispatch(f);
          if (stop) { readerActiveRef.current = false; return; }
        }
      }
      // stream close — flush any trailing complete frame
      for (const f of parser.flush()) {
        const stop = dispatch(f);
        if (stop) { readerActiveRef.current = false; return; }
      }
      readerActiveRef.current = false;

      if (terminalRef.current) { finish(); return; }

      // paused on an approval gate: the server has closed the stream and is
      // waiting for the user's decision — do NOT auto-reconnect (that would
      // burn the bounded budget during human think-time). resumeAfterApproval()
      // re-opens the stream once the user Approves/Denies.
      if (awaitingApprovalRef.current && runIdRef.current) { return; }

      if (runIdRef.current && !cancelledRef.current && !unmountedRef.current) {
        scheduleReconnect();           // durable: the run continues server-side
        return;
      }
      // legacy: stream ended without a terminal event
      if (cancelledRef.current) { cb.onCancelled?.(); }
      else if (contentRef.current) {
        cb.onComplete?.({ messageId: 0, content: contentRef.current, citations: [], sourcesUsed: false, memoriesUsed: false });
      }
      finish();
    } catch (err) {
      readerActiveRef.current = false;
      if (cancelledRef.current || unmountedRef.current
        || (err instanceof DOMException && err.name === "AbortError")) {
        if (!runIdRef.current && !terminalRef.current) cb.onCancelled?.();
        finish();
        return;
      }
      if (runIdRef.current && !terminalRef.current) { scheduleReconnect(); return; }
      cb.onError?.({ code: "NETWORK_ERROR", detail: err instanceof Error ? err.message : "Network request failed" });
      finish();
    }

    // ── per-frame dispatch (returns true to stop the pass) ──
    function dispatch(f: { event: string; data: string; id?: number }): boolean {
      // dedupe / ordering (durable only)
      let fresh = true;
      if (runIdRef.current && typeof f.id === "number") {
        if (f.id <= cursorRef.current) fresh = false;      // replayed — no side effects
        else cursorRef.current = f.id;
      }
      let data: Record<string, unknown> = {};
      if (f.data) { try { data = JSON.parse(f.data); } catch { return false; } }
      if (!data || typeof data !== "object" || Array.isArray(data)) return false;

      switch (f.event) {
        case "token": {
          if (!fresh) return false;
          const token = (data.token as string) || "";
          contentRef.current += token;
          setStreamedContentSafe(contentRef.current);
          cb.onToken?.(token);
          setPhaseSafe("streaming");
          return false;
        }
        case "complete": {
          terminalRef.current = true;
          clearReconnect();
          setPhaseSafe("done");
          cb.onComplete?.({
            messageId: (data.message_id as number) ?? 0,
            content: contentRef.current,
            citations: (data.citations as Array<Record<string, unknown>>) || [],
            sourcesUsed: data.sources_used === true,
            memoriesUsed: data.memories_used === true,
          });
          finish();
          return true;
        }
        case "error": {
          terminalRef.current = true;
          clearReconnect();
          setPhaseSafe("done");
          cb.onError?.({
            code: (data.code as string) || "UNKNOWN_ERROR",
            detail: (data.detail as string) || "An unknown error occurred",
          });
          finish();
          return true;
        }
        case "cancelled": {
          terminalRef.current = true;
          clearReconnect();
          setPhaseSafe("done");
          cb.onCancelled?.();
          finish();
          return true;
        }
        case "tool_approval_required": {
          if (runIdRef.current) {
            awaitingApprovalRef.current = true;
            setPhaseSafe("waiting_approval");
            cb.onApprovalRequired?.({
              approvalId: (data.approval_id as string) || "",
              toolId: (data.tool_id as string) || undefined,
            });
          } else {
            cb.onToolApproval?.({
              tool: data.tool as string,
              args: data.args,
              server: data.server as string,
            });
          }
          return false;
        }
        case "run_status": {
          const s = (data.status as string) || "";
          if (s === "resuming") setPhaseSafe("resuming");
          else if (s === "running") setPhaseSafe("streaming");
          return false;
        }
        case "plan": {
          if (fresh) cb.onPlan?.((data.steps as Array<Record<string, unknown>>) || []);
          return false;
        }
        case "agent_status": {
          if (fresh) cb.onAgentStatus?.({ agent: (data.agent as string) || "unknown", message: (data.message as string) || "" });
          return false;
        }
        case "research_progress": {
          if (fresh) {
            const code = data.code as string;
            if (typeof code === "string") cb.onResearchProgress?.(code);
          }
          return false;
        }
        case "agent_progress": {
          if (fresh) {
            const code = data.code as string;
            if (typeof code === "string" && AGENT_PROGRESS_CODES.has(code)) cb.onAgentProgress?.(code);
          }
          return false;
        }
        default:
          return false;                 // unknown event — ignore safely
      }
    }
  }, []);

  // ── reconnect scheduler (durable only) ───────────────────

  function scheduleReconnect() {
    if (terminalRef.current || cancelledRef.current || unmountedRef.current || !runIdRef.current) return;
    if (reconnectTimerRef.current) return;              // already scheduled
    const elapsed = Date.now() - startedAtRef.current;
    if (attemptsRef.current >= RECONNECT_MAX_ATTEMPTS || elapsed >= RECONNECT_MAX_ELAPSED_MS) {
      terminalRef.current = true;
      setPhaseSafe("done");
      cbRef.current.onError?.({ code: "reconnect_exhausted", detail: "Lost connection to the response. Please retry." });
      finish();
      return;
    }
    const delay = backoffMs(attemptsRef.current);
    attemptsRef.current += 1;
    setPhaseSafe("reconnecting");
    cbRef.current.onPhase?.("reconnecting");
    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      if (terminalRef.current || cancelledRef.current || unmountedRef.current) return;
      void runPass(true);
    }, delay);
  }

  function clearReconnect() {
    if (reconnectTimerRef.current) { clearTimeout(reconnectTimerRef.current); reconnectTimerRef.current = null; }
  }

  function setPhaseSafe(p: DurablePhase) {
    if (unmountedRef.current) return;
    setPhase(p);
    if (p === "waiting_approval" || p === "reconnecting" || p === "resuming" || p === "cancelling") {
      cbRef.current.onPhase?.(p);
    }
  }
  function setStreamedContentSafe(v: string) { if (!unmountedRef.current) setStreamedContent(v); }

  function finish() {
    contentRef.current = "";
    setStreamedContentSafe("");
    if (!unmountedRef.current) setIsStreaming(false);
    abortRef.current = null;
    readerActiveRef.current = false;
    startPendingRef.current = false;
    if (terminalRef.current) {
      clearReconnect();
      awaitingApprovalRef.current = false;
      runIdRef.current = null;
      cursorRef.current = 0;
      idemKeyRef.current = null;
      submissionRef.current = null;
      if (!unmountedRef.current) { setDurableRunId(null); setPhase(null); }
    }
  }

  // ── public API ──────────────────────────────────────────

  const startStream = useCallback(
    async (
      sessionId: number,
      message: string,
      imageUrlOrCallbacks: string | undefined | StreamCallbacks,
      callbacks?: StreamCallbacks,
      /** Canonical backend mode for THIS send only (MP-3C3D3B). Omit/undefined
       *  ("Auto") preserves the exact pre-existing omitted-mode behavior. */
      mode?: BackendChatMode,
    ) => {
      let imageUrl: string | undefined;
      let actual: StreamCallbacks;
      // 3-arg form: startStream(id, msg, callbacks). 4-arg form:
      // startStream(id, msg, imageUrl?, callbacks). Any object in slot 3 is the
      // callbacks bag (it need not contain onStart); a string/undefined is the
      // optional image url.
      const threeArg = typeof imageUrlOrCallbacks === "object" && imageUrlOrCallbacks !== null;
      if (threeArg) { actual = imageUrlOrCallbacks as StreamCallbacks; imageUrl = undefined; }
      else { imageUrl = imageUrlOrCallbacks as string | undefined; actual = callbacks ?? {}; }

      // double-submit guards: an in-flight stream, or a synchronous re-entry
      if (abortRef.current || startPendingRef.current || isStreaming) return;
      startPendingRef.current = true;

      // fresh logical send — one new Idempotency-Key even if the text repeats
      cancelledRef.current = false;
      terminalRef.current = false;
      awaitingApprovalRef.current = false;
      attemptsRef.current = 0;
      startedAtRef.current = Date.now();
      cursorRef.current = 0;
      runIdRef.current = null;
      idemKeyRef.current = newIdempotencyKey();
      submissionRef.current = { sessionId, message, imageUrl, mode };
      cbRef.current = actual;
      contentRef.current = "";
      setStreamedContent("");
      setDurableRunId(null);
      setPhase(null);
      setIsStreaming(true);
      actual.onStart?.();

      await runPass(false);
    },
    [isStreaming, runPass],
  );

  /** Legacy stop: abort the fetch. On legacy this yields `onCancelled` and no
   *  save. On a durable run it only closes the local view + stops reconnect —
   *  it does NOT cancel the run (use `cancelDurableRun`). */
  const cancelStream = useCallback(() => {
    cancelledRef.current = true;
    clearReconnect();
    if (abortRef.current) { abortRef.current.abort(); abortRef.current = null; }
  }, []);

  /** Durable explicit cancel: POST /api/runs/{id}/cancel and WAIT for the
   *  persisted `cancelled`/terminal stream event — never claim cancellation
   *  before the server confirms it. */
  const cancelDurableRun = useCallback(async () => {
    const runId = runIdRef.current;
    if (!runId || terminalRef.current) return;
    awaitingApprovalRef.current = false;
    setPhaseSafe("cancelling");
    try {
      await API.cancelRun(runId);
    } catch { /* 404 / already terminal — the stream still delivers the truth */ }
    if (!readerActiveRef.current && !terminalRef.current) {
      attemptsRef.current = 0;
      startedAtRef.current = Date.now();
      scheduleReconnect();
    }
  }, []);

  /** Called by the app after the user Approves/Denies via the dialog — keep or
   *  re-open the stream so continuation / denial events are received. */
  const resumeAfterApproval = useCallback(() => {
    if (terminalRef.current || !runIdRef.current) return;
    awaitingApprovalRef.current = false;
    setPhaseSafe("resuming");
    if (!readerActiveRef.current) {
      attemptsRef.current = 0;
      startedAtRef.current = Date.now();
      scheduleReconnect();
    }
  }, []);

  return {
    isStreaming,
    streamedContent,
    durableRunId,
    phase,
    startStream,
    cancelStream,
    cancelDurableRun,
    resumeAfterApproval,
  };
}
