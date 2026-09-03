/**
 * ConfidenceBadge — F6 Capability 2.
 *
 * Shows a small color-coded confidence score under an assistant message.
 * Colors: green (80-100), amber (50-79), red (0-49). Hidden entirely when the
 * score is null (model didn't produce one, or evaluation was skipped).
 */
import { Gauge } from "lucide-react";
import { cn } from "./lib/utils";

interface ConfidenceBadgeProps {
  confidence: number | null | undefined;
  reason?: string | null;
}

export function ConfidenceBadge({ confidence, reason }: ConfidenceBadgeProps) {
  if (confidence === null || confidence === undefined) return null;

  const tone =
    confidence >= 80
      ? "bg-green-500/15 text-green-400 border-green-500/30"
      : confidence >= 50
        ? "bg-amber-500/15 text-amber-400 border-amber-500/30"
        : "bg-red-500/15 text-red-400 border-red-500/30";

  return (
    <span
      title={reason || undefined}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium",
        tone
      )}
    >
      <Gauge className="h-3 w-3" />
      {confidence}%
    </span>
  );
}

export default ConfidenceBadge;
