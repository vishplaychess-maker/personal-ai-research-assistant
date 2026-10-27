/**
 * SkillsPanel — capability grid for Settings > Skills.
 *
 * Phase 1 skeleton: three mocked capabilities with local-only enable state.
 * Nothing here executes; no network calls. Toggles are persisted in
 * localStorage so the UI survives a reload until the backend registry exists.
 */
import { useEffect, useMemo, useState } from "react";
import { FileText, Globe, Search, Sparkles, Terminal, type LucideIcon } from "lucide-react";
import { Card, CardContent } from "../ui/card";
import { Input } from "../ui/input";
import { Switch } from "../ui/switch";
import { cn } from "../../lib/utils";

interface CapabilitySkill {
  id: string;
  name: string;
  description: string;
  category: string;
  icon: LucideIcon;
}

const MOCK_SKILLS: CapabilitySkill[] = [
  {
    id: "web-search",
    name: "Web Search",
    description: "Search the web and read pages the agent cites in its answers.",
    category: "Research",
    icon: Globe,
  },
  {
    id: "code-executor",
    name: "Code Executor",
    description: "Run snippets in an isolated workspace and return the output.",
    category: "Automation",
    icon: Terminal,
  },
  {
    id: "file-reader",
    name: "File Reader",
    description: "Open files from the workspace to summarize or extract text.",
    category: "Files",
    icon: FileText,
  },
];

const STORAGE_KEY = "thunder.capabilities.v1";

function defaultEnabled(): Record<string, boolean> {
  return Object.fromEntries(MOCK_SKILLS.map((s) => [s.id, s.id !== "code-executor"]));
}

function loadEnabled(): Record<string, boolean> {
  const defaults = defaultEnabled();
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return defaults;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return Object.fromEntries(
      MOCK_SKILLS.map((s) => [
        s.id,
        typeof parsed[s.id] === "boolean" ? (parsed[s.id] as boolean) : defaults[s.id],
      ])
    );
  } catch {
    return defaults;
  }
}

export function SkillsPanel() {
  const [enabled, setEnabled] = useState<Record<string, boolean>>(loadEnabled);
  const [query, setQuery] = useState("");

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(enabled));
    } catch {
      // Storage can be unavailable (private mode); toggles still work for the session.
    }
  }, [enabled]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return MOCK_SKILLS;
    return MOCK_SKILLS.filter((s) =>
      `${s.name} ${s.description} ${s.category}`.toLowerCase().includes(q)
    );
  }, [query]);

  const enabledCount = MOCK_SKILLS.filter((s) => enabled[s.id]).length;

  return (
    <section data-testid="capabilities-panel" className="mb-6">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            <h3 className="text-sm font-medium">Capabilities</h3>
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {enabledCount} of {MOCK_SKILLS.length} enabled
            </span>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            Turn individual capabilities on or off. Preview only — execution is not
            wired yet, and switches are saved in this browser.
          </p>
        </div>
        <div className="relative w-full shrink-0 sm:w-56">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search capabilities"
            aria-label="Search capabilities"
            className="h-8 pl-8 text-xs"
          />
        </div>
      </div>

      {filtered.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No capabilities match “{query}”.
        </p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {filtered.map((skill) => {
            const Icon = skill.icon;
            const isEnabled = Boolean(enabled[skill.id]);
            return (
              <Card
                key={skill.id}
                className={cn(
                  "transition-colors",
                  isEnabled && "border-primary/40 bg-primary/5"
                )}
              >
                <CardContent className="flex items-start gap-3 p-4">
                  <span
                    className={cn(
                      "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg",
                      isEnabled ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"
                    )}
                  >
                    <Icon className="h-4 w-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">{skill.name}</span>
                      <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                        {skill.category}
                      </span>
                    </div>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                      {skill.description}
                    </p>
                  </div>
                  <Switch
                    checked={isEnabled}
                    onCheckedChange={(checked) =>
                      setEnabled((prev) => ({ ...prev, [skill.id]: checked }))
                    }
                    aria-label={`Enable ${skill.name}`}
                  />
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </section>
  );
}

export default SkillsPanel;
