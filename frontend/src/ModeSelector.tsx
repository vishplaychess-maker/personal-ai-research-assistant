/**
 * ModeSelector — Phase 1B.2R MP-3C3D3B per-message response-mode picker.
 *
 * Four UI choices: Auto (frontend-only, omits `mode`), Chat, Deep Research,
 * Multi-Agent. Availability of the three backend modes comes from the
 * advisory `chat_modes` field on `GET /api/settings` (an authenticated,
 * existing endpoint — no new route). Backend validation/routing in
 * `submission.validate_request_mode` / `messages_stream.unsupported_mode_reason`
 * remains the sole authority; this is UX-only advice.
 */
import { useCallback, useEffect, useState } from "react";
import { ChevronDown, Sparkles, MessageSquare, Search, Users } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from "./components/ui/dropdown-menu";
import { cn } from "./lib/utils";
import { API } from "./api";
import type { BackendChatMode, UIChatMode, ChatModeCapability } from "./types";

// ── Static per-mode display metadata (labels only — never sent on the wire) ─

const MODE_META: Record<BackendChatMode, { label: string; icon: typeof MessageSquare }> = {
  chat: { label: "Chat", icon: MessageSquare },
  deep_research: { label: "Deep Research", icon: Search },
  multi_agent: { label: "Multi-Agent", icon: Users },
};

// Fail-safe default: Auto + Chat are always valid per the D3A/D1 contract;
// never speculatively enable the advanced modes before/without a successful
// capability fetch (requirement 14).
const SAFE_DEFAULT: Record<BackendChatMode, ChatModeCapability> = {
  chat: { mode: "chat", available: true },
  deep_research: { mode: "deep_research", available: false, reason: "Checking availability…" },
  multi_agent: { mode: "multi_agent", available: false, reason: "Checking availability…" },
};

interface ModeSelectorProps {
  mode: UIChatMode;
  onModeChange: (mode: UIChatMode) => void;
  /** Freeze the selector while a send is in flight (requirement 11). */
  disabled?: boolean;
}

export function ModeSelector({ mode, onModeChange, disabled = false }: ModeSelectorProps) {
  const [caps, setCaps] = useState<Record<BackendChatMode, ChatModeCapability>>(SAFE_DEFAULT);
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await API.getUserSettings();
      const next: Record<BackendChatMode, ChatModeCapability> = { ...SAFE_DEFAULT };
      for (const c of data.chat_modes || []) {
        if (c.mode === "chat" || c.mode === "deep_research" || c.mode === "multi_agent") {
          next[c.mode] = c;
        }
      }
      setCaps(next);
      // Requirement 13: a stale explicit selection that a fresh capability
      // read now marks unavailable resets to Auto with a visible, non-
      // destructive explanation (never left silently pointed at a mode the
      // backend would 422 on send).
      if (mode !== "auto" && !next[mode]?.available) {
        onModeChange("auto");
        setNotice(`${MODE_META[mode].label} is no longer available — switched to Auto.`);
      }
    } catch {
      // Fail safe on a fetch error: never speculatively enable advanced
      // modes (requirement 14) — fall back to the same safe default.
      setCaps(SAFE_DEFAULT);
    }
  }, [mode, onModeChange]);

  useEffect(() => {
    void refresh();
    // fetch once per mount (component/session-scoped state per spec) —
    // re-checked whenever the dropdown is reopened, see onOpenChange below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(t);
  }, [notice]);

  const label = mode === "auto" ? "Auto" : MODE_META[mode].label;

  return (
    <div className="flex flex-col gap-1 shrink-0">
      <DropdownMenu open={open} onOpenChange={(v) => { setOpen(v); if (v) void refresh(); }}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            disabled={disabled}
            aria-label={`Response mode: ${label}. Press to change.`}
            title="Choose response mode"
            className={cn(
              "flex items-center gap-1.5 rounded-xl bg-white/5 px-3 py-1.5 text-xs font-medium border border-white/10 transition-all",
              "hover:bg-white/5 hover:border-primary/20 disabled:opacity-50 disabled:cursor-not-allowed",
              open && "border-primary/30",
            )}
          >
            <Sparkles className="h-3.5 w-3.5 text-primary shrink-0" />
            <span className="max-w-[100px] truncate text-secondary">{label}</span>
            <ChevronDown className={cn("h-3.5 w-3.5 icon-secondary transition-transform shrink-0", open && "rotate-180")} />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-60">
          <DropdownMenuRadioGroup value={mode} onValueChange={(v) => onModeChange(v as UIChatMode)}>
            <DropdownMenuRadioItem value="auto">
              <Sparkles className="h-3.5 w-3.5 mr-2 shrink-0" />
              <span className="flex-1">Auto</span>
            </DropdownMenuRadioItem>
            {(Object.keys(MODE_META) as BackendChatMode[]).map((m) => {
              const cap = caps[m];
              const Icon = MODE_META[m].icon;
              const unavailable = !cap.available;
              return (
                <DropdownMenuRadioItem
                  key={m}
                  value={m}
                  disabled={unavailable}
                  title={unavailable ? (cap.reason || "Not available") : undefined}
                >
                  <Icon className="h-3.5 w-3.5 mr-2 shrink-0" />
                  <span className="flex-1">{MODE_META[m].label}</span>
                  {unavailable && (
                    <span className="ml-2 shrink-0 max-w-[92px] truncate text-[10px] text-secondary">
                      {cap.reason || "Unavailable"}
                    </span>
                  )}
                </DropdownMenuRadioItem>
              );
            })}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      {notice && (
        <span role="status" className="px-1 text-[10px] text-secondary">{notice}</span>
      )}
    </div>
  );
}

export default ModeSelector;
