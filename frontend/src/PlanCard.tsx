/**
 * PlanCard — F6 Capability 1 (v1, preview only).
 *
 * Shows the LLM's proposed multi-step plan as numbered steps. In v1 the plan
 * is an advisory preview: [Run] is a no-op placeholder for a later capability,
 * [Edit] lets the user modify the steps (re-parsed from a JSON textarea), and
 * [Cancel] dismisses the card. Nothing here blocks the ongoing chat.
 */
import { useState } from "react";
import { ListChecks, Play, Pencil, X, RotateCcw } from "lucide-react";
import { Button } from "./components/ui/button";

export interface PlanStep {
  step: number;
  action: string;
  target: string;
  reason: string;
}

interface PlanCardProps {
  steps: Array<Record<string, unknown>>;
  onRun?: () => void;
  onCancel: () => void;
}

export function PlanCard({ steps, onRun, onCancel }: PlanCardProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const parseDraft = (): PlanStep[] => {
    try {
      const parsed = JSON.parse(draft);
      if (!Array.isArray(parsed)) return [];
      return parsed.filter(
        (s: any) =>
          s &&
          typeof s === "object" &&
          "step" in s &&
          "action" in s &&
          "target" in s &&
          "reason" in s
      ) as PlanStep[];
    } catch {
      return [];
    }
  };

  const startEdit = () => {
    setDraft(JSON.stringify(steps, null, 2));
    setEditing(true);
  };

  const applyEdit = () => {
    setDraft("");
    setEditing(false);
  };

  return (
    <div className="my-3 animate-message-in w-full max-w-[85%] rounded-xl border border-primary/20 bg-primary/5 p-4">
      <div className="flex items-center gap-2 text-sm font-medium text-primary">
        <ListChecks className="h-4 w-4" />
        Proposed plan
      </div>

      {!editing ? (
        <>
          <ol className="mt-3 space-y-2">
            {steps.map((s, idx) => (
              <li
                key={idx}
                className="flex gap-3 rounded-lg bg-white/5 px-3 py-2 text-sm"
              >
                <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/20 text-[11px] font-semibold text-primary">
                  {idx + 1}
                </span>
                <span className="min-w-0 text-white">
                  <span className="font-medium">{String(s.action)}</span>
                  {s.target ? (
                    <span className="text-secondary"> on {String(s.target)}</span>
                  ) : null}
                  {s.reason ? (
                    <span className="block text-xs text-secondary">
                      {String(s.reason)}
                    </span>
                  ) : null}
                </span>
              </li>
            ))}
          </ol>
          <div className="mt-3 flex gap-2">
            <Button
              size="sm"
              onClick={() => {
                setEditing(false);
                onRun?.();
              }}
              title="Run this plan (v1 preview — not yet executed)"
            >
              <Play className="h-3.5 w-3.5 mr-1.5" />
              Run
            </Button>
            <Button size="sm" variant="outline" onClick={startEdit}>
              <Pencil className="h-3.5 w-3.5 mr-1.5" />
              Edit
            </Button>
            <Button size="sm" variant="ghost" onClick={onCancel}>
              <X className="h-3.5 w-3.5 mr-1.5" />
              Cancel
            </Button>
          </div>
        </>
      ) : (
        <>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            rows={6}
            className="mt-3 w-full resize-none rounded-lg bg-black/30 p-2 font-mono text-xs text-white focus:outline-none"
            aria-label="Edit plan JSON"
          />
          <div className="mt-2 flex gap-2">
            <Button size="sm" onClick={applyEdit}>
              <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
              Apply
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setEditing(false)}
            >
              Close
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

export default PlanCard;
