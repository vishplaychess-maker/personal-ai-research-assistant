/**
 * ApprovalDialog — Phase 1B.2R MP-3C3C MCP tool-approval UX.
 *
 * Shown when a durable run emits `tool_approval_required`. Fetches the redacted
 * approval detail from the authenticated approval API (the ONLY sanctioned
 * source — never reconstructed from marker text), presents an accessible modal
 * with explicit Approve / Deny buttons, and calls back so the parent can resume
 * the stream. Approve/Deny require an explicit click; there is no countdown and
 * no auto-approve. The approve/deny API call NEVER executes MCP in the browser.
 *
 * Never renders or logs: API keys, action hashes, session-family ids, raw
 * provider metadata, envelope fields. Only tool id, redacted summary/purpose,
 * risk/lock status, request time and state.
 */

import * as React from "react";
import { API } from "../api";
import type { ApprovalActionPreview, ApprovalDetail } from "../types";
import type { DurablePhase } from "../useStreaming";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "./ui/dialog";
import { Button } from "./ui/button";
import { cn } from "../lib/utils";

/** MP-4C decision 2 — the two server-owned save tool ids. */
const SAVE_TOOL_IDS = ["memory_write", "directive_write"];

function isSaveTool(toolId: string | undefined | null): boolean {
  return !!toolId && SAVE_TOOL_IDS.includes(toolId);
}

function isPreviewTool(toolId: string | undefined | null): boolean {
  return toolId === "python_sandbox" || isSaveTool(toolId);
}

type Local =
  | "loading"
  | "ready"
  | "deciding"
  | "approved"
  | "denied"
  | "error";

export interface ApprovalDialogProps {
  /** null ⇒ dialog closed */
  approvalId: string | null;
  /** durable run phase — used to show "resuming"/"reconnecting" after a decision */
  phase: DurablePhase | null;
  /** run reached a terminal state — allow closing regardless of local state */
  runTerminal?: boolean;
  onApprove: () => void;
  onDeny: () => void;
  onDismiss: () => void;
}

function statusLine(local: Local, phase: DurablePhase | null): string {
  if (local === "loading") return "Loading approval request…";
  if (local === "deciding") return "Submitting your decision…";
  if (local === "error") return "This approval request could not be loaded or is no longer available.";
  if (local === "approved") {
    if (phase === "reconnecting") return "Approved — reconnecting to the response…";
    if (phase === "resuming" || phase === "streaming") return "Approved — resuming the response…";
    return "Approved — resuming.";
  }
  if (local === "denied") return "Denied. The tool call was not run.";
  return "Review the tool call below and choose Approve or Deny.";
}

export function ApprovalDialog({
  approvalId,
  phase,
  runTerminal,
  onApprove,
  onDeny,
  onDismiss,
}: ApprovalDialogProps): React.ReactElement {
  const [detail, setDetail] = React.useState<ApprovalDetail | null>(null);
  const [local, setLocal] = React.useState<Local>("loading");
  // MP-4C — the EXACT Python action. Owner-only, requested-only, read-only.
  const [preview, setPreview] = React.useState<ApprovalActionPreview | null>(null);
  const [previewFailed, setPreviewFailed] = React.useState(false);
  const decidedRef = React.useRef(false);
  const openId = approvalId ?? "";

  // fetch redacted detail when a new approval opens
  React.useEffect(() => {
    if (!approvalId) return;
    let cancelled = false;
    decidedRef.current = false;
    setDetail(null);
    setLocal("loading");
    API.getApproval(approvalId)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        // an already-decided / expired / consumed approval is not actionable
        setLocal(d.state === "requested" ? "ready" : "error");
      })
      .catch(() => {
        if (!cancelled) { setDetail(null); setLocal("error"); }
      });
    return () => {
      cancelled = true;
    };
  }, [approvalId]);

  // MP-4C — fetch the exact code ONLY for a requested python_sandbox approval.
  // The endpoint is owner-scoped; anything else 409s and Approve stays disabled.
  // The preview is cleared whenever the approval id changes or the dialog closes.
  React.useEffect(() => {
    setPreview(null);
    setPreviewFailed(false);
    if (!approvalId || !detail) return;
    if (!isPreviewTool(detail.tool_id) || detail.state !== "requested") return;
    let cancelled = false;
    API.getApprovalAction(approvalId)
      .then((p) => {
        if (cancelled) return;
        const identityOk =
          !!p &&
          p.approval_id === approvalId &&
          p.tool_id === detail.tool_id &&
          (!detail.action_hash || p.action_hash === detail.action_hash);
        if (identityOk) setPreview(p);
        else setPreviewFailed(true);
      })
      .catch(() => {
        if (!cancelled) setPreviewFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [approvalId, detail]);

  const previewRequired = isPreviewTool(detail?.tool_id);
  const approveReady =
    local === "ready" && (!previewRequired || (preview !== null && !previewFailed));

  const canClose =
    local === "error" || local === "approved" || local === "denied" || !!runTerminal;

  const submit = React.useCallback(
    async (kind: "approve" | "deny") => {
      if (!approvalId || decidedRef.current || local === "deciding") return;
      decidedRef.current = true;
      setLocal("deciding");
      try {
        if (kind === "approve") await API.approveRun(approvalId);
        else await API.denyRun(approvalId);
        setLocal(kind === "approve" ? "approved" : "denied");
        if (kind === "approve") onApprove();
        else onDeny();
      } catch {
        // a stale / consumed / already-decided approval: surface a safe state,
        // let the stream deliver the true outcome.
        setLocal("error");
        decidedRef.current = false;
      }
    },
    [approvalId, local, onApprove, onDeny],
  );

  const risk = (detail?.risk_level || "").toLowerCase();
  const requestedAt = detail?.created_at
    ? new Date(detail.created_at).toLocaleString()
    : null;
  const summaryText = (() => {
    if (detail?.purpose_redacted && detail.purpose_redacted.trim()) return detail.purpose_redacted;
    const s = detail?.display_summary;
    if (s && typeof s === "object" && Object.keys(s).length) {
      try {
        return JSON.stringify(s);
      } catch {
        return "";
      }
    }
    return "";
  })();

  return (
    <Dialog
      open={!!approvalId}
      onOpenChange={(open) => {
        if (!open && canClose) onDismiss();
      }}
    >
      <DialogContent
        className="max-w-md"
        onEscapeKeyDown={(e) => {
          if (!canClose) e.preventDefault();
        }}
        onInteractOutside={(e) => {
          e.preventDefault(); // modal — outside click never dismisses
        }}
        aria-describedby={`approval-status-${openId}`}
      >
        <DialogHeader>
          <DialogTitle>Tool approval required</DialogTitle>
          <DialogDescription>
            {isSaveTool(detail?.tool_id)
              ? "The assistant wants to save something to your account. Nothing is written until you approve."
              : "The assistant wants to run a tool. It will not run until you approve."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          <div>
            <div className="text-xs uppercase tracking-wide text-muted-foreground">Tool</div>
            <div className="font-mono break-all">{detail?.tool_id || "…"}</div>
          </div>

          {risk && (
            <div className="flex items-center gap-2">
              <span className="text-xs uppercase tracking-wide text-muted-foreground">Risk</span>
              <span
                className={cn(
                  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium",
                  risk === "high" && "border-destructive text-destructive",
                  risk === "medium" && "border-amber-500 text-amber-600 dark:text-amber-400",
                  risk === "low" && "border-input text-muted-foreground",
                )}
              >
                {/* not colour-only: also spelled out */}
                {risk} risk
              </span>
            </div>
          )}

          {summaryText && (
            <div>
              <div className="text-xs uppercase tracking-wide text-muted-foreground">Details</div>
              <p className="break-words text-muted-foreground">{summaryText}</p>
            </div>
          )}

          {preview && preview.kind && (
            <div>
              <div
                role="note"
                className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300"
              >
                ⚠️ Approving saves the exact{" "}
                {preview.kind === "memory" ? "memory" : "directive"} shown below.
                Review it before approving.
              </div>
              <div className="mt-2 text-xs uppercase tracking-wide text-muted-foreground">
                Exact {preview.kind === "memory" ? "memory" : "directive"} to be saved
              </div>
              <pre
                aria-label="Exact content to be saved"
                className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 font-mono text-xs"
              >
                {preview.content}
              </pre>
            </div>
          )}

          {preview && !preview.kind && (
            <div>
              <div
                role="note"
                className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300"
              >
                ⚠️ This will run Python code exactly as shown below. Review every
                line before approving — approving runs this code.
              </div>
              <div className="mt-2 text-xs uppercase tracking-wide text-muted-foreground">
                Exact code
              </div>
              <pre
                aria-label="Exact Python code to be executed"
                className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 font-mono text-xs"
              >
                {preview.code}
              </pre>
            </div>
          )}

          {previewRequired && previewFailed && (
            <p role="alert" className="text-xs text-destructive">
              {isSaveTool(detail?.tool_id)
                ? "The exact content could not be loaded, so approval is disabled. You can still deny."
                : "The exact code could not be loaded, so approval is disabled. You can still deny."}
            </p>
          )}

          {requestedAt && (
            <div className="text-xs text-muted-foreground">Requested {requestedAt}</div>
          )}

          <div
            id={`approval-status-${openId}`}
            role="status"
            aria-live="polite"
            className="rounded-md bg-muted px-3 py-2 text-muted-foreground"
          >
            {statusLine(local, phase)}
          </div>
        </div>

        <DialogFooter className="gap-2">
          {canClose ? (
            <Button variant="secondary" onClick={onDismiss} autoFocus>
              Close
            </Button>
          ) : (
            <>
              <Button
                variant="outline"
                onClick={() => submit("deny")}
                disabled={local !== "ready"}
              >
                Deny
              </Button>
              <Button
                onClick={() => submit("approve")}
                disabled={!approveReady}
                autoFocus
              >
                Approve
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default ApprovalDialog;
