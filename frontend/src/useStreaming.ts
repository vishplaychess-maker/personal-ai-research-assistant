/**
 * useStreaming — React hook for SSE-based token streaming.
 *
 * Connects to POST /api/sessions/{id}/messages/stream and parses
 * SSE events (start, token, complete, error, cancelled). Supports
 * cancellation via AbortController.
 */

import { useState, useRef, useCallback, useEffect } from "react";
import { getCsrfToken, getAuthHeadersAsync } from "./auth";

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
    async (sessionId: number, message: string, imageUrlOrCallbacks: string | undefined | StreamCallbacks, callbacks?: StreamCallbacks) => {
      // Support both old signature (sessionId, message, callbacks) and new signature (sessionId, message, imageUrl, callbacks)
      let imageUrl: string | undefined;
      let actualCallbacks: StreamCallbacks;
      
      const isOldSignature = typeof imageUrlOrCallbacks === "object" && imageUrlOrCallbacks !== null && "onStart" in imageUrlOrCallbacks;
      
      if (isOldSignature) {
        // Old signature: (sessionId, message, callbacks)
        actualCallbacks = imageUrlOrCallbacks;
        imageUrl = undefined;
      } else {
        // New signature: (sessionId, message, imageUrl, callbacks)
        imageUrl = imageUrlOrCallbacks as string | undefined;
        actualCallbacks = callbacks!;
      }
      // Prevent double-submission
      if (abortRef.current) return;

      cancelledRef.current = false;
      const controller = new AbortController();
      abortRef.current = controller;
      setIsStreaming(true);
      contentRef.current = "";
      setStreamedContent("");
      actualCallbacks.onStart?.();

      try {
        const headers: Record<string, string> = {
          "Content-Type": "application/json",
        };
        // Phase 7C: attach the access token so the backend can identify the
        // session owner (no more fallback to the default user).
        const auth = await getAuthHeadersAsync();
        if (auth.Authorization) headers.Authorization = auth.Authorization;
        // Phase 7C: state-changing POST requires the double-submit CSRF token.
        const csrf = getCsrfToken();
        if (csrf) headers["X-CSRF-Token"] = csrf;

        const response = await fetch(
          `/api/sessions/${sessionId}/messages/stream`,
          {
            method: "POST",
            headers,
            body: JSON.stringify({ message, image_url: imageUrl }),
            credentials: "include",
            signal: controller.signal,
          }
        );

        if (cancelledRef.current) {
          actualCallbacks.onCancelled?.();
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
          actualCallbacks.onError?.({
            code,
            detail: body.detail || `Request failed with status ${response.status}`,
          });
          finish();
          return;
        }

        const reader = response.body?.getReader();
        if (!reader) {
          actualCallbacks.onError?.({
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
            actualCallbacks.onCancelled?.();
            finish();
            return;
          }

          const { done, value } = await reader.read();

          if (cancelledRef.current) {
            actualCallbacks.onCancelled?.();
            finish();
            return;
          }

          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const { events, remainder } = parseSSEBuffer(buffer);
          buffer = remainder;

          for (const event of events) {
            if (cancelledRef.current) {
actualCallbacks.onCancelled?.();
              finish();
              return;
            }

            switch (event.type) {
              case "token": {
                const token = (event.data.token as string) || "";
                contentRef.current += token;
                setStreamedContent(contentRef.current);
                actualCallbacks.onToken?.(token);
                break;
              }
              case "complete": {
actualCallbacks.onComplete?.({
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
actualCallbacks.onError?.({
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
          actualCallbacks.onCancelled?.();
        } else if (contentRef.current) {
          actualCallbacks.onComplete?.({
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
          actualCallbacks.onCancelled?.();
        } else {
          actualCallbacks.onError?.({
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
