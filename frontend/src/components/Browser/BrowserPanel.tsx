/**
 * BrowserPanel — built-in browsing view (V1, sandboxed iframe).
 *
 * Center-column view toggled by the sidebar's Browser entry (activeView in
 * App.tsx); the chat layout is untouched — when the panel unmounts the chat
 * remounts exactly as it was left.
 *
 * Security model:
 *  - every address goes through `validateBrowserUrl` first: only http/https
 *    survive, so `javascript:` / `data:` / `file:` / `blob:` / `vbscript:`
 *    and friends can never reach the frame.
 *  - the iframe carries the strict sandbox list mandated for V1; content
 *    running inside cannot inherit this app's cookies or storage context.
 *  - X-Frame-Options / CSP frame-ancestors refusals are NOT observable from
 *    JS in Chromium (the block page is opaque cross-origin), so before
 *    mounting the frame we ask the backend to probe the response headers
 *    (`/api/browser/embeddable`). A negative verdict shows the friendly
 *    fallback screen instead of a silent blank frame; an inconclusive
 *    verdict still loads the frame with a visible helper hint.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Globe,
  Loader2,
  RotateCw,
  ShieldAlert,
} from "lucide-react";
import { API } from "../../api";
import { Button } from "../ui/button";
import { cn } from "../../lib/utils";

// V1-mandated sandbox: scripts/forms/popups allowed, storage isolated by the
// sandbox itself (no same-origin inheritance across parent/child documents
// for third-party sites, and top-level navigation only via a user gesture).
const SANDBOX_ATTR =
  "allow-scripts allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation";

const DEFAULT_URL = "https://www.wikipedia.org";

// ── URL validation (protocol allow-list is the XSS/file-access wall) ──

export interface ValidatedUrl {
  ok: boolean;
  url?: string;
  error?: string;
}

export function validateBrowserUrl(raw: string): ValidatedUrl {
  const trimmed = raw.trim();
  if (!trimmed) return { ok: false, error: "Enter a URL to browse." };
  // Control characters would make the address bar lie about the target.
  if (/[\u0000-\u001f\u007f]/.test(trimmed)) {
    return { ok: false, error: "The URL contains invalid characters." };
  }
  // Bare-host convenience: "example.com" becomes "https://example.com".
  const candidate = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(trimmed)
    ? trimmed
    : `https://${trimmed}`;
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return { ok: false, error: "That is not a valid URL." };
  }
  // The ONLY protocols V1 allows. javascript:, data:, file:, blob:, vbscript:
  // and every other scheme are rejected here and never reach the iframe.
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return {
      ok: false,
      error: "Only http:// and https:// websites can be opened here.",
    };
  }
  if (!parsed.hostname || (parsed.hostname !== "localhost" && !parsed.hostname.includes("."))) {
    return { ok: false, error: "Enter a valid host name, e.g. example.com." };
  }
  return { ok: true, url: parsed.toString() };
}

// ── Component ─────────────────────────────────────────────

interface HistoryEntry {
  url: string;
  verdict: "allowed" | "blocked" | "unknown";
  reason: string | null;
}

interface BrowserPanelProps {
  /** Whether the model selector etc. want the header hidden is owned by AppShell; nothing here. */
  initialUrl?: string;
}

export function BrowserPanel({ initialUrl }: BrowserPanelProps) {
  const [current, setCurrent] = useState<HistoryEntry | null>(null);
  const [inputUrl, setInputUrl] = useState(initialUrl ?? DEFAULT_URL);
  const [isLoading, setIsLoading] = useState(false);
  const [navError, setNavError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  // App-level history stack: cross-origin frames do not expose their own
  // session history to the parent, so Back/Forward replay OUR stack.
  const historyRef = useRef<HistoryEntry[]>([]);
  const indexRef = useRef(-1);
  const [historyPos, setHistoryPos] = useState(0);
  const historyLenRef = useRef(0);

  const commit = useCallback((entry: HistoryEntry, push: boolean) => {
    if (push) {
      const next = historyRef.current.slice(0, indexRef.current + 1);
      next.push(entry);
      historyRef.current = next;
      indexRef.current = next.length - 1;
      historyLenRef.current = next.length;
    } else {
      historyRef.current[indexRef.current] = entry;
    }
    setHistoryPos(indexRef.current);
    setCurrent(entry);
    setNavError(null);
    if (entry.verdict === "blocked") {
      setIsLoading(false);
    } else {
      setIsLoading(true); // iframe onLoad flips this back off
    }
    setReloadKey((k) => k + 1);
  }, []);

  const navigate = useCallback(
    async (rawInput: string, push = true) => {
      const validated = validateBrowserUrl(rawInput);
      if (!validated.ok || !validated.url) {
        setNavError(validated.error ?? "Invalid URL");
        return;
      }
      setIsLoading(true);
      try {
        // Header probe (X-Frame-Options / CSP frame-ancestors). Chromium
        // makes blocked frames unobservable from JS, so the backend verdict
        // is what turns e.g. google.com into the friendly fallback screen.
        const verdict = await API.checkBrowserEmbeddable(validated.url);
        commit(
          {
            url: validated.url,
            verdict: verdict.embeddable ? "allowed" : "blocked",
            reason: verdict.reason,
          },
          push,
        );
      } catch {
        // Probe unavailable → optimistic V1 load with an honest hint.
        commit({ url: validated.url, verdict: "unknown", reason: null }, push);
      }
    },
    [commit],
  );

  const goBack = useCallback(() => {
    if (indexRef.current <= 0) return;
    indexRef.current -= 1;
    setHistoryPos(indexRef.current);
    const entry = historyRef.current[indexRef.current];
    if (entry) {
      setCurrent(entry);
      setNavError(null);
      setIsLoading(entry.verdict !== "blocked");
      setReloadKey((k) => k + 1);
    }
  }, []);

  const goForward = useCallback(() => {
    if (indexRef.current >= historyLenRef.current - 1) return;
    indexRef.current += 1;
    setHistoryPos(indexRef.current);
    const entry = historyRef.current[indexRef.current];
    if (entry) {
      setCurrent(entry);
      setNavError(null);
      setIsLoading(entry.verdict !== "blocked");
      setReloadKey((k) => k + 1);
    }
  }, []);

  // First-visit: nothing loads automatically — a quiet start screen keeps
  // the app dependency-free and never surprises with network traffic.
  const openInNewTab = (url: string) => {
    window.open(url, "_blank", "noopener,noreferrer");
  };

  return (
    <main className="flex h-full min-w-0 flex-1 flex-col bg-background" data-testid="browser-panel">
      {/* Address bar */}
      <div className="relative z-10 flex items-center gap-1.5 border-b border-white/10 bg-background/80 px-3 py-2 backdrop-blur-xl">
        <Button
          size="icon"
          variant="ghost"
          onClick={goBack}
          disabled={historyPos <= 0}
          title="Back"
          aria-label="Back"
          className="h-9 w-9 shrink-0 rounded-xl transition-colors hover:bg-white/5"
        >
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <Button
          size="icon"
          variant="ghost"
          onClick={goForward}
          disabled={historyPos >= historyLenRef.current - 1}
          title="Forward"
          aria-label="Forward"
          className="h-9 w-9 shrink-0 rounded-xl transition-colors hover:bg-white/5"
        >
          <ArrowRight className="h-4 w-4" />
        </Button>
        <Button
          size="icon"
          variant="ghost"
          onClick={() => current && commit(current, false)}
          disabled={!current || isLoading}
          title="Refresh"
          aria-label="Refresh"
          className="h-9 w-9 shrink-0 rounded-xl transition-colors hover:bg-white/5"
        >
          <RotateCw className="h-4 w-4" />
        </Button>
        <div className="relative flex min-w-0 flex-1 items-center">
          <Globe className="pointer-events-none absolute left-3 h-3.5 w-3.5 text-secondary" />
          <input
            type="text"
            value={inputUrl}
            onChange={(e) => setInputUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void navigate(inputUrl);
            }}
            placeholder={DEFAULT_URL}
            aria-label="Address bar"
            spellCheck={false}
            className="h-9 w-full rounded-xl border border-white/10 bg-white/5 pl-9 pr-3 text-sm text-foreground placeholder:text-placeholder transition-colors focus:border-primary/30 focus:outline-none"
          />
        </div>
        <Button
          size="sm"
          onClick={() => void navigate(inputUrl)}
          disabled={isLoading}
          className="h-9 shrink-0 rounded-xl px-4"
        >
          {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : "Go"}
        </Button>
      </div>

      {navError && (
        <div className="px-4 pt-2">
          <p className="text-xs text-destructive" role="alert">{navError}</p>
        </div>
      )}

      {/* Viewport */}
      <div className="relative min-h-0 flex-1 overflow-hidden">
        {/* Start screen — nothing loaded yet */}
        {!current && (
          <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center text-secondary">
            <Globe className="h-10 w-10 opacity-30" />
            <h2 className="text-sm font-medium text-foreground">Built-in browser</h2>
            <p className="max-w-md text-xs">
              Enter an address above to browse inside this session. Sandboxed,
              script-restricted, and isolated from this app's cookies.
              Sites like Wikipedia and Example.com embed well.
            </p>
          </div>
        )}

        {/* Blocked fallback — header probe refused this site */}
        {current?.verdict === "blocked" && (
          <div
            role="status"
            className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center"
            data-testid="browser-blocked-fallback"
          >
            <ShieldAlert className="h-10 w-10 text-amber-400/80" />
            <h2 className="text-sm font-medium text-foreground">
              This website does not allow being displayed in an embedded browser.
            </h2>
            <p className="max-w-md text-xs text-secondary">
              {current.reason
                ? `${current.reason} `
                : ""}
              Many sites (e.g. google.com, github.com) send X-Frame-Options or
              CSP rules that forbid embedding to defend against clickjacking.
              Try Wikipedia or Example.com instead.
            </p>
            <div className="flex flex-wrap items-center justify-center gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => void navigate("https://example.com")}
              >
                Try Example.com
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => void navigate("https://www.wikipedia.org")}
              >
                Try Wikipedia
              </Button>
              <Button size="sm" variant="ghost" onClick={() => openInNewTab(current.url)}>
                Open externally
              </Button>
            </div>
          </div>
        )}

        {/* Loading veil over the frame area (never blocks the rest of the app) */}
        {current && current.verdict !== "blocked" && (
          <>
            <iframe
              key={`${current.url}#${reloadKey}`}
              src={current.url}
              title="Embedded browser viewport"
              sandbox={SANDBOX_ATTR}
              onLoad={() => setIsLoading(false)}
              className="h-full w-full border-0 bg-white"
            />
            {isLoading && (
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-background/60">
                <Loader2 className="h-6 w-6 animate-spin text-secondary" />
              </div>
            )}
            {current.verdict === "unknown" && !isLoading && (
              <div className={cn("absolute bottom-3 left-1/2 -translate-x-1/2")}>
                <p className="rounded-full border border-white/10 bg-background/90 px-3 py-1.5 text-[11px] text-secondary shadow-lg">
                  Page staying blank? This site may refuse embedding — try
                  Wikipedia or Example.com.
                </p>
              </div>
            )}
          </>
        )}
      </div>
    </main>
  );
}

export default BrowserPanel;
