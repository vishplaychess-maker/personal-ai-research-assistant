/**
 * useStreaming — React hook for SSE-based token streaming.
 *
 * Connects to POST /api/sessions/{id}/messages/stream and parses
 * SSE events (start, token, complete, error, cancelled). Supports
 * cancellation via AbortController.
 */

import { useState, useRef, useCallback, useEffect } from "react";

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

export interface StreamCallbacks {
  onStart?: () => void;
  onToken?: (token: string) => void;
  onComplete?: (result: StreamResult) => void;
  onError?: (error: StreamError) => void;
  onCancelled?: () => void;
}

// ── SSE Parser ────────────────────────────────────────────

interface ParsedEvent {
  type: "start" | "token" | "complete" | "error" | "cancelled";
  data: Record<string, unknown>;
}

/**
 * Parse a SSE-formatted buffer into events and return any leftover text.
 *
 * Handles the standard SSE format:
 *   event: start\n
 *   data: {...}\n
 *   \n
 */
/**
 * Parse SSE-formatted text into events.
 *
 * SSE events are separated by double newlines (\n\n).
 * The last segment is treated as a remainder (incomplete event)
 * that should be prepended to the next chunk of data.
 */
export function parseSSEBuffer(buffer: string): {
  events: ParsedEvent[];
  remainder: string;
} {
  const events: ParsedEvent[] = [];

  // Split on double newline — each segment is a complete event
  // except the last segment, which may be incomplete
  const segments = buffer.split("\n\n");

  for (let i = 0; i < segments.length - 1; i++) {
    const event = parseSingleEvent(segments[i]);
    if (event) {
      events.push(event);
    }
  }

  // The last segment is the remainder (incomplete event)
  const remainder = segments[segments.length - 1] ?? "";

  return { events, remainder };
}

function parseSingleEvent(text: string): ParsedEvent | null {
  const lines = text.split("\n");
  let eventType = "";
  const dataLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith("event: ")) {
      eventType = line.slice(7);
    } else if (line.startsWith("data: ")) {
      dataLines.push(line.slice(6));
    }
  }

  if (!eventType || dataLines.length === 0) return null;

  try {
    return {
      type: eventType as ParsedEvent["type"],
      data: JSON.parse(dataLines.join("")),
    };
  } catch {
    return null;
  }
}

// ── Hook ──────────────────────────────────────────────────

export function useStreaming() {
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamedContent, setStreamedContent] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const contentRef = useRef("");
  const cancelledRef = useRef(false);

  // Cleanup on unmount — abort any active stream
  useEffect(() => {
    return () => {
      cancelledRef.current = true;
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
    };
  }, []);

  const startStream = useCallback(
    async (sessionId: number, message: string, callbacks: StreamCallbacks) => {
      // Prevent double-submission
      if (abortRef.current) return;

      cancelledRef.current = false;
      const controller = new AbortController();
      abortRef.current = controller;
      setIsStreaming(true);
      contentRef.current = "";
      setStreamedContent("");
      callbacks.onStart?.();

      try {
        const response = await fetch(
          `/api/sessions/${sessionId}/messages/stream`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message }),
            signal: controller.signal,
          }
        );

        if (cancelledRef.current) {
          callbacks.onCancelled?.();
          finish();
          return;
        }

        if (!response.ok) {
          const body = await response.json().catch(() => ({
            detail: `Request failed with status ${response.status}`,
          }));
          const code =
            response.status === 404
              ? "SESSION_NOT_FOUND"
              : response.status === 422
                ? "VALIDATION_ERROR"
                : "HTTP_ERROR";
          callbacks.onError?.({
            code,
            detail: body.detail || `Request failed with status ${response.status}`,
          });
          finish();
          return;
        }

        const reader = response.body?.getReader();
        if (!reader) {
          callbacks.onError?.({
            code: "READ_ERROR",
            detail: "Failed to read response stream",
          });
          finish();
          return;
        }

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          if (cancelledRef.current) {
            callbacks.onCancelled?.();
            finish();
            return;
          }

          const { done, value } = await reader.read();

          if (cancelledRef.current) {
            callbacks.onCancelled?.();
            finish();
            return;
          }

          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const { events, remainder } = parseSSEBuffer(buffer);
          buffer = remainder;

          for (const event of events) {
            if (cancelledRef.current) {
              callbacks.onCancelled?.();
              finish();
              return;
            }

            switch (event.type) {
              case "token": {
                const token = (event.data.token as string) || "";
                contentRef.current += token;
                setStreamedContent(contentRef.current);
                callbacks.onToken?.(token);
                break;
              }
              case "complete": {
                callbacks.onComplete?.({
                  messageId: event.data.message_id as number,
                  content: contentRef.current,
                  citations: (event.data.citations ||
                    []) as Array<Record<string, unknown>>,
                  sourcesUsed: event.data.sources_used === true,
                  memoriesUsed: event.data.memories_used === true,
                });
                finish();
                return;
              }
              case "error": {
                callbacks.onError?.({
                  code: (event.data.code as string) || "UNKNOWN_ERROR",
                  detail: (event.data.detail as string) || "An unknown error occurred",
                });
                finish();
                return;
              }
            }
          }
        }

        // Stream ended without a terminal event
        if (cancelledRef.current) {
          callbacks.onCancelled?.();
        } else if (contentRef.current) {
          callbacks.onComplete?.({
            messageId: 0,
            content: contentRef.current,
            citations: [],
            sourcesUsed: false,
            memoriesUsed: false,
          });
        }
        finish();
      } catch (err: unknown) {
        if (cancelledRef.current || (err instanceof DOMException && err.name === "AbortError")) {
          callbacks.onCancelled?.();
        } else {
          callbacks.onError?.({
            code: "NETWORK_ERROR",
            detail:
              err instanceof Error
                ? err.message
                : "Network request failed",
          });
        }
        finish();
      }

      function finish() {
        contentRef.current = "";
        setStreamedContent("");
        setIsStreaming(false);
        abortRef.current = null;
      }
    },
    []
  );

  const cancelStream = useCallback(() => {
    cancelledRef.current = true;
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }, []);

  return { isStreaming, streamedContent, startStream, cancelStream };
}
