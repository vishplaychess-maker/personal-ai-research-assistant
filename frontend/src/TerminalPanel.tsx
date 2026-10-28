/**
 * TerminalPanel — in-app terminal. Runs an allowlisted command on the backend
 * and streams stdout/stderr over SSE into a console view.
 *
 * Separate from the agent's [PROPOSED_COMMAND] approval flow.
 */

import { useCallback, useRef, useState } from "react";
import { X, Play, Square, Trash2 } from "lucide-react";
import { getCsrfToken, getAuthHeadersAsync } from "./auth";
import { apiUrl } from "./apiBase";

type Status = "idle" | "running" | "success" | "error";

interface Line {
  stream: "stdout" | "stderr" | "meta";
  text: string;
}

interface Props {
  onClose: () => void;
}

// Minimal SSE splitter: returns complete events + leftover remainder.
function splitSSE(buf: string): { events: Array<{ type: string; data: any }>; rest: string } {
  const parts = buf.split("\n\n");
  const rest = parts.pop() ?? "";
  const events = parts.map((seg) => {
    let type = "message";
    const dataLines: string[] = [];
    for (const l of seg.split("\n")) {
      if (l.startsWith("event: ")) type = l.slice(7).trim();
      else if (l.startsWith("data: ")) dataLines.push(l.slice(6));
    }
    let data: any = {};
    try {
      data = JSON.parse(dataLines.join("\n") || "{}");
    } catch {
      data = { raw: dataLines.join("\n") };
    }
    return { type, data };
  });
  return { events, rest };
}

export function TerminalPanel({ onClose }: Props) {
  const [command, setCommand] = useState("");
  const [lines, setLines] = useState<Line[]>([]);
  const [status, setStatus] = useState<Status>("idle");
  const abortRef = useRef<AbortController | null>(null);
  const runIdRef = useRef<string | null>(null);
  const outRef = useRef<HTMLPreElement>(null);

  const append = useCallback((l: Line) => {
    setLines((prev) => [...prev, l]);
    queueMicrotask(() => {
      if (outRef.current) outRef.current.scrollTop = outRef.current.scrollHeight;
    });
  }, []);

  const run = useCallback(async () => {
    const cmd = command.trim();
    if (!cmd || status === "running") return;

    setStatus("running");
    append({ stream: "meta", text: `$ ${cmd}` });
    const controller = new AbortController();
    abortRef.current = controller;
    runIdRef.current = null;

    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      const auth = await getAuthHeadersAsync();
      if (auth.Authorization) headers.Authorization = auth.Authorization;
      const csrf = getCsrfToken();
      if (csrf) headers["X-CSRF-Token"] = csrf;

      const resp = await fetch(apiUrl("/api/terminal/execute"), {
        method: "POST",
        headers,
        credentials: "include",
        body: JSON.stringify({ command: cmd }),
        signal: controller.signal,
      });

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }));
        append({ stream: "stderr", text: `✖ ${body.detail || resp.statusText}` });
        setStatus("error");
        return;
      }

      const reader = resp.body?.getReader();
      if (!reader) {
        append({ stream: "stderr", text: "✖ no response stream" });
        setStatus("error");
        return;
      }
      const decoder = new TextDecoder();
      let buf = "";
      let exitCode: number | null | undefined;

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const { events, rest } = splitSSE(buf);
        buf = rest;
        for (const ev of events) {
          if (ev.type === "pid") runIdRef.current = ev.data.run_id ?? null;
          else if (ev.type === "stdout") append({ stream: "stdout", text: ev.data.line ?? "" });
          else if (ev.type === "stderr") append({ stream: "stderr", text: ev.data.line ?? "" });
          else if (ev.type === "start")
            append({ stream: "meta", text: `▶ ${(ev.data.argv || []).join(" ")}  (cwd ${ev.data.cwd})` });
          else if (ev.type === "exit") exitCode = ev.data.code;
          else if (ev.type === "timeout") {
            append({ stream: "stderr", text: `⏱ ${ev.data.detail || "timed out"}` });
            exitCode = -1;
          } else if (ev.type === "error")
            append({ stream: "stderr", text: `✖ ${ev.data.detail || "error"}` });
        }
      }
      append({ stream: "meta", text: `exit ${exitCode ?? "?"}` });
      setStatus(exitCode === 0 || exitCode === null ? "success" : "error");
    } catch (e: any) {
      if (e?.name === "AbortError") append({ stream: "meta", text: "■ stopped" });
      else append({ stream: "stderr", text: `✖ ${e?.message || e}` });
      setStatus(e?.name === "AbortError" ? "idle" : "error");
    } finally {
      abortRef.current = null;
    }
  }, [command, status, append]);

  const stop = useCallback(async () => {
    abortRef.current?.abort();
    const rid = runIdRef.current;
    if (rid) {
      try {
        const headers: Record<string, string> = {};
        const auth = await getAuthHeadersAsync();
        if (auth.Authorization) headers.Authorization = auth.Authorization;
        const csrf = getCsrfToken();
        if (csrf) headers["X-CSRF-Token"] = csrf;
        await fetch(apiUrl(`/api/terminal/stop/${rid}`), {
          method: "POST",
          headers,
          credentials: "include",
        });
      } catch {
        /* best effort */
      }
    }
  }, []);

  const statusColor =
    status === "running" ? "#e5c07b" : status === "success" ? "#98c379" : status === "error" ? "#e06c75" : "#8a8a8a";

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-background/95 backdrop-blur">
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <span>Terminal</span>
          <span style={{ color: statusColor }} className="text-xs font-normal">
            ● {status}
          </span>
        </div>
        <button onClick={onClose} title="Close" className="rounded-lg p-1.5 hover:bg-white/10">
          <X className="h-4 w-4" />
        </button>
      </div>

      <pre
        ref={outRef}
        className="flex-1 overflow-auto m-0 p-4 text-[12.5px] leading-relaxed"
        style={{ background: "#0c0c0c", color: "#d4d4d4", fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" }}
      >
        {lines.length === 0 ? (
          <span style={{ color: "#666" }}>
            Runs allowlisted commands on the server (shell=False, fixed cwd, 30s cap). Not a full shell.
          </span>
        ) : (
          lines.map((l, i) => (
            <div
              key={i}
              style={{
                color: l.stream === "stderr" ? "#e06c75" : l.stream === "meta" ? "#7aa2f7" : "#d4d4d4",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {l.text}
            </div>
          ))
        )}
      </pre>

      <div className="flex items-center gap-2 border-t border-border p-3">
        <span style={{ fontFamily: "ui-monospace, monospace" }} className="text-muted-foreground">
          $
        </span>
        <input
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              run();
            }
          }}
          placeholder="echo hello   ·   git status   ·   pytest -q"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          className="flex-1 rounded-md border border-border bg-transparent px-3 py-1.5 text-sm outline-none"
          style={{ fontFamily: "ui-monospace, monospace" }}
        />
        {status === "running" ? (
          <button
            onClick={stop}
            className="flex items-center gap-1 rounded-md bg-destructive/90 px-3 py-1.5 text-sm text-white"
          >
            <Square className="h-3.5 w-3.5" /> Stop
          </button>
        ) : (
          <button
            onClick={run}
            disabled={!command.trim()}
            className="flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-40"
          >
            <Play className="h-3.5 w-3.5" /> Run
          </button>
        )}
        <button
          onClick={() => {
            setLines([]);
            setStatus("idle");
          }}
          title="Clear"
          className="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-white/5"
        >
          <Trash2 className="h-3.5 w-3.5" /> Clear
        </button>
      </div>
    </div>
  );
}
