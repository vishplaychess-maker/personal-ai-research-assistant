import {
  BookOpen,
  Calendar,
  Monitor,
  Puzzle,
  Server,
  SlidersHorizontal,
  Sparkles,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { cn } from "../../lib/utils";

export type SettingsTab =
  | "providers"
  | "scheduler"
  | "mcp"
  | "directives"
  | "skills"
  | "plugins"
  | "desktop";

const TABS: { id: SettingsTab; label: string; icon: LucideIcon }[] = [
  { id: "providers", label: "Providers", icon: Zap },
  { id: "scheduler", label: "Scheduled Tasks", icon: Calendar },
  { id: "mcp", label: "MCP Servers", icon: Server },
  { id: "directives", label: "Lessons Learned", icon: BookOpen },
  { id: "skills", label: "Skills", icon: Sparkles },
  { id: "plugins", label: "Plugins", icon: Puzzle },
  { id: "desktop", label: "Desktop", icon: Monitor },
];

export function SettingsSidebar({
  activeTab,
  onSelect,
}: {
  activeTab: SettingsTab;
  onSelect: (tab: SettingsTab) => void;
}) {
  return (
    <aside className="flex shrink-0 flex-col border-b border-border/70 bg-card/40 md:w-64 md:overflow-hidden md:border-b-0 md:border-r">
      <div className="hidden items-center gap-3 px-5 pb-4 pt-6 md:flex">
        <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-primary/25 via-fuchsia-500/15 to-transparent text-primary ring-1 ring-primary/20">
          <SlidersHorizontal className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <p className="text-[10px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">
            Workspace
          </p>
          <h2 className="truncate text-base font-semibold tracking-tight text-foreground">
            Settings
          </h2>
        </div>
      </div>

      <nav
        role="tablist"
        aria-label="Settings sections"
        className="grid grid-cols-2 gap-1 p-2 min-[480px]:grid-cols-3 md:flex md:min-h-0 md:flex-1 md:flex-col md:overflow-y-auto md:overflow-x-hidden md:p-3"
      >
        {TABS.map(({ id, label, icon: Icon }) => {
          const active = id === activeTab;
          return (
            <button
              key={id}
              type="button"
              role="tab"
              id={`settings-tab-${id}`}
              aria-selected={active}
              aria-controls={`settings-panel-${id}`}
              onClick={() => onSelect(id)}
              className={cn(
                "group relative flex min-w-0 items-center gap-2.5 overflow-hidden rounded-xl px-3 py-2.5 text-sm font-medium transition-all duration-200 md:w-full",
                active
                  ? "bg-gradient-to-r from-primary/15 via-fuchsia-500/10 to-transparent text-foreground shadow-sm"
                  : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground"
              )}
            >
              {active && (
                <span
                  aria-hidden
                  className="absolute inset-y-2 left-0 w-0.5 rounded-full bg-gradient-to-b from-primary to-fuchsia-400"
                />
              )}
              <span
                className={cn(
                  "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg transition-colors",
                  active
                    ? "bg-gradient-to-br from-primary/20 to-fuchsia-500/15 text-primary ring-1 ring-primary/20"
                    : "bg-secondary/60 text-muted-foreground group-hover:bg-secondary group-hover:text-foreground"
                )}
              >
                <Icon className="h-4 w-4" />
              </span>
              <span className="truncate">{label}</span>
            </button>
          );
        })}
      </nav>

      <div className="mt-auto hidden shrink-0 border-t border-border/60 px-5 py-4 md:block">
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          Changes are saved to your account and apply to new agent tasks.
        </p>
      </div>
    </aside>
  );
}

export default SettingsSidebar;
