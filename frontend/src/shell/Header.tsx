import { Activity, PanelLeft, PanelLeftClose, Share, SquarePen } from "lucide-react";
import { ModelSelector } from "../ModelSelector";
import { THEMES, useTheme } from "../components/ThemeProvider";
import { Button } from "../components/ui/button";
import { cn } from "../lib/utils";

/**
 * Slim top bar: sidebar toggle, the real model catalogue, the three-theme
 * switcher, the inspector toggle and share.
 *
 * `ModelSelector` is the existing component (it fetches providers/models via
 * `api.ts`); the header only hosts it so there is one selector in the app, not
 * two.
 */
export interface HeaderProps {
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
  onNewChat: () => void;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
  sessionId: number | null;
  currentModel: string | null;
  onModelChange: (model: string | null) => void;
  onShare: () => void;
  shareDisabled?: boolean;
}

export function Header({
  sidebarOpen,
  onToggleSidebar,
  onNewChat,
  inspectorOpen,
  onToggleInspector,
  sessionId,
  currentModel,
  onModelChange,
  onShare,
  shareDisabled = false,
}: HeaderProps) {
  const { theme, setTheme } = useTheme();

  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-2 px-3 sm:px-4">
      <div className="flex min-w-0 items-center gap-1.5">
        <Button
          size="icon"
          variant="ghost"
          onClick={onToggleSidebar}
          title={sidebarOpen ? "Close sidebar" : "Open sidebar"}
          aria-label={sidebarOpen ? "Close sidebar" : "Open sidebar"}
          className="h-9 w-9 rounded-lg"
        >
          {sidebarOpen ? (
            <PanelLeftClose className="h-5 w-5 stroke-[1.75]" />
          ) : (
            <PanelLeft className="h-5 w-5 stroke-[1.75]" />
          )}
        </Button>
        {!sidebarOpen ? (
          <Button
            size="icon"
            variant="ghost"
            onClick={onNewChat}
            title="New chat"
            aria-label="New chat"
            className="h-9 w-9 rounded-lg"
          >
            <SquarePen className="h-5 w-5 stroke-[1.75]" />
          </Button>
        ) : null}

        {sessionId !== null ? (
          <ModelSelector
            key={sessionId}
            sessionId={sessionId}
            currentModel={currentModel}
            onModelChange={onModelChange}
          />
        ) : null}
      </div>

      <div className="flex items-center gap-2">
        <div
          className="hidden items-center rounded-xl border border-border bg-card p-0.5 sm:flex"
          role="group"
          aria-label="Theme"
        >
          {THEMES.map((option) => (
            <button
              key={option.id}
              type="button"
              onClick={() => setTheme(option.id)}
              aria-pressed={theme === option.id}
              className={cn(
                "rounded-lg px-2.5 py-1 text-xs transition-colors",
                theme === option.id
                  ? "bg-secondary font-medium text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              {option.label}
            </button>
          ))}
        </div>

        <button
          type="button"
          onClick={onToggleInspector}
          aria-pressed={inspectorOpen}
          className={cn(
            "flex items-center gap-1.5 rounded-xl border px-2.5 py-1.5 text-xs font-medium transition-colors",
            inspectorOpen
              ? "border-border bg-secondary text-foreground"
              : "border-border text-muted-foreground hover:bg-secondary hover:text-foreground"
          )}
          title="Toggle the inspector"
        >
          <Activity className="h-3.5 w-3.5" />
          <span className="hidden md:inline">Inspector</span>
        </button>

        <button
          type="button"
          onClick={onShare}
          disabled={shareDisabled}
          className="flex items-center gap-1.5 rounded-xl border border-border px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
          title={shareDisabled ? "Save a message first" : "Share this thread"}
        >
          <Share className="h-3.5 w-3.5" />
          <span className="hidden md:inline">Share</span>
        </button>
      </div>
    </header>
  );
}
