import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Copy, Loader2, LogOut, RefreshCw } from "lucide-react";
import { API } from "./api";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./components/ui/dialog";
import { Button } from "./components/ui/button";
import type { OAuthAccountResponse, OAuthStatusResponse } from "./types";
import { cn } from "./lib/utils";

/**
 * Sign in with Grok — OAuth device-code flow against a SuperGrok / X Premium+
 * subscription, so the app spends the subscription instead of an xAI API key.
 *
 * The server owns the polling throttle: this card asks for the status on the
 * interval the server advertises, and the server answers from its own record
 * without touching xAI when a poll is not due. Nothing here ever sees a token.
 */

const MIN_POLL_MS = 2000;

function secondsLeft(iso: string | null): number | null {
  if (!iso) return null;
  const target = Date.parse(iso);
  if (Number.isNaN(target)) return null;
  return Math.max(0, Math.round((target - Date.now()) / 1000));
}

function formatRemaining(total: number | null): string {
  if (total === null) return "";
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function GrokOAuthCard() {
  const [status, setStatus] = useState<OAuthStatusResponse | null>(null);
  const [account, setAccount] = useState<OAuthAccountResponse | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [starting, setStarting] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [copied, setCopied] = useState(false);
  const [remaining, setRemaining] = useState<number | null>(null);
  const pollTimer = useRef<number | null>(null);

  const loadAccount = useCallback(async () => {
    try {
      setAccount(await API.grokAccount());
    } catch {
      // Entitlement is a nicety here: the sign-in itself already succeeded.
      setAccount(null);
    }
  }, []);

  const refreshStatus = useCallback(async (loud = false) => {
    try {
      const next = await API.grokSignInStatus();
      // "none" is the ordinary "never signed in" state; keep the card idle.
      setStatus(next.status === "none" ? null : next);
      setError(null);
      if (next.status === "connected") {
        setDialogOpen(false);
        void loadAccount();
      } else if (next.status === "expired" || next.status === "error") {
        setDialogOpen(false);
      }
      return next;
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not check sign-in";
      if (message.toLowerCase().includes("disabled")) setDisabled(true);
      // A failed background check is not a user-visible failure: nothing was
      // asked for. Only an explicit action reports it.
      if (loud) setError(message);
      return null;
    }
  }, [loadAccount]);

  // Hydrate on mount so a sign-in started before a reload is not lost.
  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  const pending = status?.status === "pending";

  // Poll while the user works in their browser. The interval comes from the
  // server, which is the party that knows xAI's pacing.
  useEffect(() => {
    if (!pending) {
      if (pollTimer.current !== null) {
        window.clearInterval(pollTimer.current);
        pollTimer.current = null;
      }
      return;
    }
    const delay = Math.max(MIN_POLL_MS, (status?.poll_interval_s ?? 5) * 1000);
    pollTimer.current = window.setInterval(() => {
      void refreshStatus();
    }, delay);
    return () => {
      if (pollTimer.current !== null) {
        window.clearInterval(pollTimer.current);
        pollTimer.current = null;
      }
    };
  }, [pending, status?.poll_interval_s, refreshStatus]);

  // Countdown for the code's own lifetime, ticked locally between polls.
  useEffect(() => {
    if (!pending || !status?.device_expires_at) {
      setRemaining(null);
      return;
    }
    setRemaining(secondsLeft(status.device_expires_at));
    const tick = window.setInterval(() => {
      setRemaining(secondsLeft(status.device_expires_at));
    }, 1000);
    return () => window.clearInterval(tick);
  }, [pending, status?.device_expires_at]);

  const signIn = async () => {
    setStarting(true);
    setError(null);
    setDisabled(false);
    try {
      await API.startGrokSignIn();
      const next = await refreshStatus(true);
      if (next) setDialogOpen(true);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not start sign-in";
      if (message.toLowerCase().includes("disabled")) setDisabled(true);
      setError(message);
    } finally {
      setStarting(false);
    }
  };

  const copyCode = async () => {
    const code = status?.user_code;
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard needs a secure context; the code stays selectable in the UI.
      setError("Copy is unavailable here. Type the code instead.");
    }
  };

  const signOut = async () => {
    if (!window.confirm("Sign out of Grok? The stored subscription credentials will be removed.")) {
      return;
    }
    setSigningOut(true);
    setError(null);
    try {
      const result = await API.signOutGrok();
      setStatus(null);
      setAccount(null);
      if (!result.revoked) {
        setError("Signed out locally; xAI did not confirm the revocation.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not sign out");
    } finally {
      setSigningOut(false);
    }
  };

  const renew = async () => {
    setError(null);
    try {
      const result = await API.refreshGrokTokens();
      setAccount(await API.grokAccount().catch(() => null));
      setStatus((current) =>
        current ? { ...current, status: "connected", access_expires_at: result.access_expires_at } : current
      );
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Could not renew the Grok session. Sign in again."
      );
      void refreshStatus(true);
    }
  };

  const connected = status?.status === "connected";
  const failed = status?.status === "expired" || status?.status === "error";

  return (
    <div className="rounded-xl border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">Grok (xAI)</span>
        <span
          className={cn(
            "rounded-full px-1.5 py-0.5 text-[10px]",
            connected && "bg-primary/10 text-primary",
            pending && "bg-amber-500/10 text-amber-400",
            failed && "bg-destructive/10 text-destructive",
            !connected && !pending && !failed && "bg-muted text-muted-foreground"
          )}
        >
          {connected ? "Subscription active" : pending ? "Waiting for sign-in" : failed ? "Sign-in failed" : "Not signed in"}
        </span>
        <div className="ml-auto flex items-center gap-2">
          {connected ? (
            <>
              <Button size="sm" variant="ghost" onClick={renew} title="Renew the access token now">
                <RefreshCw className="mr-1 h-3.5 w-3.5" />
                Renew
              </Button>
              <Button size="sm" variant="ghost" onClick={signOut} disabled={signingOut}>
                {signingOut ? (
                  <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <LogOut className="mr-1 h-3.5 w-3.5" />
                )}
                Sign out
              </Button>
            </>
          ) : (
            <Button size="sm" onClick={signIn} disabled={starting || pending}>
              {starting || pending ? (
                <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              ) : null}
              {pending ? "Waiting…" : "Sign in with Grok"}
            </Button>
          )}
        </div>
      </div>

      {connected && (status?.account_email || status?.account_name) ? (
        <p className="mt-2 truncate text-xs text-muted-foreground" aria-live="polite">
          Signed in as {status?.account_email || status?.account_name}
          {account?.entitlement_known
            ? ` · ${account.models.length} subscription model${account.models.length === 1 ? "" : "s"}`
            : ""}
        </p>
      ) : null}

      {connected && account?.models?.length ? (
        <div className="mt-2 flex flex-wrap gap-1">
          {account.models.slice(0, 6).map((model) => (
            <span key={model} className="rounded-md bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {model}
            </span>
          ))}
          {account.models.length > 6 ? (
            <span className="rounded-md bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              +{account.models.length - 6} more
            </span>
          ) : null}
        </div>
      ) : null}

      {disabled ? (
        <p className="mt-2 text-xs text-muted-foreground">
          Grok sign-in is disabled on this server. Set <code>GROK_OAUTH_ENABLED=true</code> to turn it on.
        </p>
      ) : null}

      {!disabled && (error || (failed && status?.message)) ? (
        <p className="mt-2 text-xs text-destructive" aria-live="polite">
          {error ?? status?.message}
        </p>
      ) : null}

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Sign in with Grok</DialogTitle>
            <DialogDescription>
              Open this link on any device and enter the code. This app checks
              with Grok automatically — no API key is used.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 py-2">
            <a
              href={status?.verification_uri}
              target="_blank"
              rel="noreferrer"
              className="break-all text-sm text-primary underline underline-offset-2"
            >
              {status?.verification_uri}
            </a>

            <div className="flex items-center justify-center gap-3 rounded-lg border bg-muted/40 p-4">
              <span className="select-all font-mono text-2xl font-semibold tracking-[0.2em]">
                {status?.user_code}
              </span>
              <Button size="icon" variant="ghost" onClick={copyCode} title="Copy code">
                {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
              </Button>
            </div>

            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span className="flex items-center gap-1.5">
                {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                {status?.message || "Waiting for authorization…"}
              </span>
              {remaining !== null ? (
                <span title="Time left before this code expires">
                  {formatRemaining(remaining)} left
                </span>
              ) : null}
            </div>

            {remaining !== null && remaining <= 0 ? (
              <p className="text-xs text-destructive">
                This code expired. Close this dialog and start sign-in again.
              </p>
            ) : null}
          </div>

          <DialogFooter>
            <Button variant="ghost" onClick={() => setDialogOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
