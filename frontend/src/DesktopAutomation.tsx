import { useCallback, useEffect, useMemo, useState } from "react";
import { Camera, FileCode2, FolderOpen, Loader2, Play, Save } from "lucide-react";
import { API } from "./api";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import { cn } from "./lib/utils";
import type {
  DesktopRunResult,
  DesktopScreenshotResult,
  DesktopStatus,
  DesktopTreeEntry,
} from "./types";

/**
 * Desktop automation panel.
 *
 * Two switches have to agree before anything runs: the server-side
 * `DESKTOP_AUTOMATION_ENABLED` master switch (read from /status) and this
 * local "Enable" toggle, which starts off on every visit. Nothing here can
 * reach outside the workspace — the server refuses such paths with a 403 whose
 * message this panel simply displays.
 */
export function DesktopAutomation() {
  const [status, setStatus] = useState<DesktopStatus | null>(null);
  const [armed, setArmed] = useState(false);
  const [entries, setEntries] = useState<DesktopTreeEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [command, setCommand] = useState("git");
  const [args, setArgs] = useState("status");
  const [output, setOutput] = useState<DesktopRunResult | null>(null);
  const [shot, setShot] = useState<DesktopScreenshotResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await API.desktopStatus());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not read desktop status");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const loadTree = useCallback(async () => {
    try {
      const tree = await API.desktopTree();
      setEntries(tree.entries);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not list the workspace");
    }
  }, []);

  useEffect(() => {
    if (armed) void loadTree();
  }, [armed, loadTree]);

  const run = async (label: string, action: () => Promise<void>) => {
    setBusy(label);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The request was refused");
    } finally {
      setBusy(null);
    }
  };

  const openFile = (entry: DesktopTreeEntry) =>
    run("read", async () => {
      const file = await API.desktopRead(entry.path);
      setSelected(file.path);
      setContent(file.content);
      setShot(null);
    });

  const saveFile = () => {
    if (!selected) return;
    void run("write", async () => {
      await API.desktopWrite(selected, content);
      await loadTree();
    });
  };

  const runCommand = () =>
    run("run", async () => {
      const argv = args.trim() ? args.trim().split(/\s+/) : [];
      setOutput(await API.desktopRun(command.trim(), argv));
      await loadTree();
    });

  const screenshot = () =>
    run("shot", async () => {
      setShot(await API.desktopScreenshot());
    });

  const commands = useMemo(
    () => [...(status?.commands ?? []), ...(status?.internal_commands ?? [])],
    [status]
  );

  if (loading) {
    return <p className="py-4 text-center text-sm text-muted-foreground">Loading…</p>;
  }

  return (
    <div className="grid gap-4">
      <div className="rounded-xl border p-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium">Enable Desktop Automation</div>
            <div className="truncate text-xs text-muted-foreground">
              {status?.enabled
                ? `Workspace: ${status.workspace}`
                : "Disabled on this server — set DESKTOP_AUTOMATION_ENABLED=true and restart."}
            </div>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={armed}
            aria-label="Enable Desktop Automation"
            disabled={!status?.enabled}
            onClick={() => {
              setArmed((current) => !current);
              setError(null);
              setOutput(null);
              setShot(null);
            }}
            className={cn(
              "relative h-6 w-11 rounded-full border transition-colors",
              armed ? "bg-primary" : "bg-muted",
              !status?.enabled && "cursor-not-allowed opacity-50"
            )}
          >
            <span
              className={cn(
                "absolute top-0.5 h-4.5 w-4.5 rounded-full bg-background transition-all",
                armed ? "left-[22px]" : "left-0.5"
              )}
            />
          </button>
        </div>
        {armed ? (
          <p className="mt-2 text-xs text-muted-foreground">
            Commands run inside the workspace only, with no shell and a {status?.timeout_s}s
            timeout. Blocked: {(status?.blocked_commands ?? []).join(", ")}.
          </p>
        ) : null}
      </div>

      {error ? (
        <p className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive" aria-live="polite">
          {error}
        </p>
      ) : null}

      {armed ? (
        <div className="grid gap-4 md:grid-cols-[minmax(0,240px)_minmax(0,1fr)]">
          {/* File tree */}
          <div className="rounded-xl border p-2">
            <div className="mb-2 flex items-center gap-2 px-1 text-xs font-medium text-muted-foreground">
              <FolderOpen className="h-3.5 w-3.5" /> Workspace
            </div>
            <div className="max-h-[38vh] overflow-y-auto">
              {entries.length === 0 ? (
                <p className="px-1 py-2 text-xs text-muted-foreground">Empty workspace.</p>
              ) : (
                entries.map((entry) => (
                  <button
                    key={entry.path}
                    type="button"
                    onClick={() => !entry.is_dir && openFile(entry)}
                    className={cn(
                      "flex w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-left text-xs hover:bg-muted",
                      selected === entry.path && "bg-primary/10 text-primary"
                    )}
                    style={{ paddingLeft: 6 + (entry.depth - 1) * 12 }}
                  >
                    <FileCode2 className="h-3.5 w-3.5 shrink-0 opacity-60" />
                    <span className="truncate">{entry.name}</span>
                    {entry.is_dir ? <span className="ml-auto opacity-50">/</span> : null}
                  </button>
                ))
              )}
            </div>
          </div>

          {/* Editor + command + output */}
          <div className="grid gap-3">
            <div className="rounded-xl border p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <Label className="truncate text-xs">
                  {selected ?? "Select a file to edit"}
                </Label>
                <Button size="sm" variant="ghost" onClick={saveFile} disabled={!selected || busy !== null}>
                  {busy === "write" ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Save className="mr-1 h-3.5 w-3.5" />}
                  Save
                </Button>
              </div>
              <textarea
                value={content}
                onChange={(event) => setContent(event.target.value)}
                disabled={!selected}
                spellCheck={false}
                className="h-32 w-full resize-y rounded-md border bg-transparent p-2 font-mono text-xs disabled:opacity-50"
                aria-label="File content"
              />
            </div>

            <div className="rounded-xl border p-3">
              <div className="mb-2 flex flex-wrap items-end gap-2">
                <div className="grid flex-1 gap-1">
                  <Label className="text-xs">Command</Label>
                  <Input
                    list="desktop-commands"
                    value={command}
                    onChange={(event) => setCommand(event.target.value)}
                    placeholder="git"
                    className="h-8 text-xs"
                  />
                  <datalist id="desktop-commands">
                    {commands.map((item) => (
                      <option key={item} value={item} />
                    ))}
                  </datalist>
                </div>
                <div className="grid flex-[2] gap-1">
                  <Label className="text-xs">Arguments (space separated, no shell)</Label>
                  <Input
                    value={args}
                    onChange={(event) => setArgs(event.target.value)}
                    placeholder="status"
                    className="h-8 text-xs"
                  />
                </div>
                <Button size="sm" onClick={runCommand} disabled={busy !== null || !command.trim()}>
                  {busy === "run" ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Play className="mr-1 h-3.5 w-3.5" />}
                  Run
                </Button>
                <Button size="sm" variant="outline" onClick={screenshot} disabled={busy !== null}>
                  {busy === "shot" ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Camera className="mr-1 h-3.5 w-3.5" />}
                  Describe screen
                </Button>
              </div>

              {shot ? (
                <p className="rounded-md bg-muted px-2 py-1.5 text-xs" aria-live="polite">
                  <span className="font-medium">{shot.status}</span>
                  {shot.description ? ` — ${shot.description}` : ""}
                  {shot.notice ? ` (${shot.notice})` : ""}
                </p>
              ) : null}

              {output ? (
                <div className="mt-2 rounded-md bg-muted/50 p-2 font-mono text-[11px]">
                  <div className="mb-1 flex gap-3 text-muted-foreground">
                    <span>exit {output.returncode}</span>
                    <span>{output.duration_ms} ms</span>
                    {output.timed_out ? <span className="text-destructive">timed out</span> : null}
                    {output.truncated ? <span>output truncated</span> : null}
                  </div>
                  <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words">
                    {output.stdout || output.stderr || "(no output)"}
                  </pre>
                </div>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
