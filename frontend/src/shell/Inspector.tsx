import { useCallback, useEffect, useState } from "react";
import { Activity, Blocks, Database, FileCode, Loader2, RefreshCw, X } from "lucide-react";
import { API } from "../api";
import type { TurnMeta } from "./Message";
import { cn } from "../lib/utils";

/**
 * Inspector drawer — real data only.
 *
 * Execution   : this turn's `agent_status` + `plan` frames (passed in from App).
 * MCP & vector: GET /api/mcp/servers and the session's documents. ChromaDB has
 *               no status endpoint, so the panel says exactly that rather than
 *               inventing index telemetry.
 * Code canvas : /api/desktop/tree + /api/desktop/read, and a clear "disabled"
 *               state when the server reports desktop automation off.
 */
type Tab = "trace" | "mcp" | "canvas";

const TABS: { id: Tab; label: string; icon: typeof Activity }[] = [
  { id: "trace", label: "Execution", icon: Activity },
  { id: "mcp", label: "MCP & Vector DB", icon: Database },
  { id: "canvas", label: "Code Canvas", icon: FileCode },
];

function Spinner({ label }: { label: string }) {
  return (
    <p className="flex items-center gap-2 text-[11px] text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      {label}
    </p>
  );
}

export function Inspector({
  open,
  onClose,
  turnMeta,
  sessionId,
}: {
  open: boolean;
  onClose: () => void;
  turnMeta?: TurnMeta;
  sessionId?: number | null;
}) {
  const [tab, setTab] = useState<Tab>("trace");
  const [desktopEnabled, setDesktopEnabled] = useState<boolean | null>(null);
  const [servers, setServers] = useState<Array<Record<string, unknown>> | null>(null);
  const [docs, setDocs] = useState<Array<Record<string, unknown>> | null>(null);
  const [tree, setTree] = useState<Array<Record<string, unknown>> | null>(null);
  const [file, setFile] = useState<{ path: string; content: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || desktopEnabled !== null) return;
    API.desktopStatus()
      .then((s) => setDesktopEnabled(s.enabled))
      .catch(() => setDesktopEnabled(false));
  }, [open, desktopEnabled]);

  const loadMcp = useCallback(async () => {
    setServers(null);
    try {
      setServers(await API.listMcpServers());
    } catch (err) {
      setServers([]);
      setError(err instanceof Error ? err.message : "Could not load MCP servers");
    }
  }, []);

  const loadDocs = useCallback(async () => {
    if (sessionId == null) {
      setDocs([]);
      return;
    }
    setDocs(null);
    try {
      setDocs((await API.listDocuments(sessionId)) as unknown as Array<Record<string, unknown>>);
    } catch {
      setDocs([]);
    }
  }, [sessionId]);

  const loadTree = useCallback(async () => {
    setTree(null);
    try {
      setTree((await API.desktopTree()).entries as unknown as Array<Record<string, unknown>>);
    } catch (err) {
      setTree([]);
      setError(err instanceof Error ? err.message : "Could not list the workspace");
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    if (tab === "mcp" && servers === null) {
      void loadMcp();
      void loadDocs();
    }
    if (tab === "canvas" && desktopEnabled === true && tree === null) void loadTree();
  }, [open, tab, servers, tree, desktopEnabled, loadMcp, loadDocs, loadTree]);

  useEffect(() => {
    if (desktopEnabled === false && tab === "canvas") setTab("trace");
  }, [desktopEnabled, tab]);

  const visibleTabs = TABS.filter((t) => t.id !== "canvas" || desktopEnabled === true);
  const readyDocs = (docs ?? []).filter((d) => d.status === "ready").length;
  const chunks = (docs ?? []).reduce((sum, d) => sum + Number(d.chunk_count ?? 0), 0);

  return (
    <aside aria-label="Inspector" className="flex h-full flex-col bg-background">
      <div className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
        <div className="flex items-center gap-2">
          <Blocks className="h-4 w-4 text-muted-foreground" />
          <span className="text-xs font-semibold uppercase tracking-tight text-foreground">
            Agent & Research Inspector
          </span>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close inspector"
          className="rounded-lg p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex items-center gap-1 border-b border-border px-3 pb-2 pt-2.5">
        {visibleTabs.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            aria-pressed={tab === id}
            className={cn(
              "flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors",
              tab === id
                ? "bg-secondary text-foreground"
                : "text-muted-foreground hover:bg-secondary hover:text-foreground"
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto p-4 text-xs">
        {error ? (
          <p className="mb-3 rounded-md border border-destructive/40 px-2 py-1.5 text-[11px] text-destructive">
            {error}
          </p>
        ) : null}

        {tab === "trace" ? (
          turnMeta && (turnMeta.agentEvents.length > 0 || turnMeta.planSteps.length > 0) ? (
            <div className="space-y-3">
              <div className="text-[11px] font-medium uppercase text-muted-foreground">
                This turn
              </div>
              <div className="space-y-2 rounded-xl border border-border bg-card p-3">
                {turnMeta.agentEvents.map((event, i) => (
                  <div key={`${event.at}-${i}`}>
                    <div className="font-medium text-foreground">{event.agent}</div>
                    <div className="text-[11px] text-muted-foreground">{event.message}</div>
                  </div>
                ))}
                {turnMeta.planSteps.map((step, i) => (
                  <div key={`plan-${i}`} className="text-muted-foreground">
                    {i + 1}. {String(step.step ?? step.action ?? "")}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="text-[11px] text-muted-foreground">
              No agent frames for this turn. Simple answers produce none.
            </p>
          )
        ) : null}

        {tab === "mcp" ? (
          <div className="space-y-4">
            <div>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[11px] font-medium uppercase text-muted-foreground">
                  MCP servers
                </span>
                <button
                  type="button"
                  onClick={() => void loadMcp()}
                  className="rounded p-1 text-muted-foreground hover:text-foreground"
                  aria-label="Reload MCP servers"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                </button>
              </div>
              {servers === null ? (
                <Spinner label="Loading servers…" />
              ) : servers.length === 0 ? (
                <p className="text-[11px] text-muted-foreground">No MCP servers configured.</p>
              ) : (
                <div className="space-y-2">
                  {servers.map((srv, i) => (
                    <div
                      key={String(srv.id ?? i)}
                      className="rounded-xl border border-border bg-card p-2.5"
                    >
                      <div className="font-mono font-medium text-foreground">
                        {String(srv.name ?? "server")}
                      </div>
                      <div className="text-[11px] text-muted-foreground">
                        {String(srv.transport ?? "stdio")} · enabled:{" "}
                        {String(srv.is_enabled ?? srv.enabled ?? "unknown")}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div>
              <div className="mb-2 text-[11px] font-medium uppercase text-muted-foreground">
                Documents in this session
              </div>
              {sessionId == null ? (
                <p className="text-[11px] text-muted-foreground">Open a session to see counts.</p>
              ) : docs === null ? (
                <Spinner label="Loading documents…" />
              ) : (
                <div className="rounded-xl border border-border bg-card p-3 text-[11px]">
                  <div className="text-foreground">
                    {docs.length} document{docs.length === 1 ? "" : "s"} · {readyDocs} ready
                  </div>
                  {chunks > 0 ? (
                    <div className="mt-1 text-muted-foreground">{chunks} indexed chunks</div>
                  ) : null}
                  <p className="mt-2 text-muted-foreground">
                    ChromaDB index status is not exposed by the backend.
                  </p>
                </div>
              )}
            </div>
          </div>
        ) : null}

        {tab === "canvas" ? (
          desktopEnabled !== true ? (
            <p className="text-[11px] text-muted-foreground">
              {desktopEnabled === null
                ? "Checking desktop automation…"
                : "Desktop automation is disabled in settings."}
            </p>
          ) : (
            <div className="space-y-2">
              {tree === null ? (
                <Spinner label="Loading workspace…" />
              ) : tree.length === 0 ? (
                <p className="text-[11px] text-muted-foreground">Workspace is empty.</p>
              ) : (
                <div className="max-h-[35vh] overflow-y-auto rounded-xl border border-border">
                  {tree
                    .filter((e) => e.is_dir !== true)
                    .map((e) => (
                      <button
                        key={String(e.path)}
                        type="button"
                        onClick={() =>
                          API.desktopRead(String(e.path))
                            .then((r) => setFile({ path: r.path, content: r.content }))
                            .catch((err) =>
                              setError(err instanceof Error ? err.message : "Could not read file")
                            )
                        }
                        className={cn(
                          "block w-full truncate px-2.5 py-1.5 text-left font-mono text-[11px] hover:bg-secondary",
                          file?.path === e.path && "bg-secondary text-foreground"
                        )}
                      >
                        {String(e.path)}
                      </button>
                    ))}
                </div>
              )}
              {file ? (
                <div>
                  <div className="mb-1 font-mono text-[11px] text-muted-foreground">
                    {file.path} (read-only)
                  </div>
                  <pre className="max-h-[40vh] overflow-auto rounded-xl border border-border bg-card p-2.5 font-mono text-[11px]">
                    {file.content}
                  </pre>
                </div>
              ) : null}
            </div>
          )
        ) : null}
      </div>
    </aside>
  );
}
