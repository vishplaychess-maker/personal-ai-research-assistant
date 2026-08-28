/**
 * ScheduledTasks — UI for managing scheduled autonomous tasks.
 */
import { useState, useEffect, useCallback } from "react";
import {
  Plus,
  Trash2,
  Clock,
  Calendar,
  Play,
  Pause,
  Trash2 as Trash2Icon,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  CheckCircle,
  Clock as ClockIcon,
} from "lucide-react";
import { API } from "./api";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { cn } from "./lib/utils";
import type { ScheduledTask, ScheduledTaskCreate, ScheduledTaskUpdate, Session } from "./types";

interface ScheduledTasksProps {
  sessions: Session[];
  activeSessionId: number | null;
  onClose: () => void;
}

export function ScheduledTasks({ sessions, activeSessionId, onClose }: ScheduledTasksProps) {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [schedulerHealth, setSchedulerHealth] = useState<{ running: boolean; jobs_count: number } | null>(null);

  // Form state
  const [showForm, setShowForm] = useState(false);
  const [formPrompt, setFormPrompt] = useState("");
  const [formCron, setFormCron] = useState("0 8 * * *"); // Default: 8 AM daily
  const [formSessionId, setFormSessionId] = useState<number | null>(activeSessionId);
  const [saving, setSaving] = useState(false);

  // Expanded task for details
  const [expandedTaskId, setExpandedTaskId] = useState<number | null>(null);

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await API.listScheduledTasks();
      setTasks(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tasks");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadSchedulerHealth = useCallback(async () => {
    try {
      const data = await API.getSchedulerHealth();
      setSchedulerHealth(data);
    } catch {
      // Ignore health check errors
    }
  }, []);

  useEffect(() => {
    loadTasks();
    loadSchedulerHealth();
  }, [loadTasks, loadSchedulerHealth]);

  const handleCreate = async () => {
    if (!formPrompt.trim() || !formCron.trim() || !formSessionId) {
      setError("Please fill in all fields");
      return;
    }

    setSaving(true);
    setError(null);

    try {
      const data: ScheduledTaskCreate = {
        session_id: formSessionId,
        prompt: formPrompt.trim(),
        cron_expression: formCron.trim(),
      };
      const newTask = await API.createScheduledTask(data);
      setTasks((prev) => [newTask, ...prev]);
      setShowForm(false);
      setFormPrompt("");
      setFormCron("0 8 * * *");
      setFormSessionId(activeSessionId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create task");
    } finally {
      setSaving(false);
    }
  };

  const handleToggleActive = async (task: ScheduledTask) => {
    try {
      const updated = await API.updateScheduledTask(task.id, { is_active: !task.is_active });
      setTasks((prev) => prev.map((t) => (t.id === task.id ? updated : t)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to toggle task");
    }
  };

  const handleRunNow = async (task: ScheduledTask) => {
    try {
      const result = await API.runScheduledTask(task.id);
      if (result.response) {
        // Reload tasks to show updated last_run_at
        loadTasks();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to run task");
    }
  };

  const handleDelete = async (taskId: number) => {
    if (!window.confirm("Are you sure you want to delete this scheduled task?")) {
      return;
    }

    try {
      await API.deleteScheduledTask(taskId);
      setTasks((prev) => prev.filter((t) => t.id !== taskId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete task");
    }
  };

  const handleToggleExpand = (taskId: number) => {
    setExpandedTaskId((prev) => (prev === taskId ? null : taskId));
  };

  const formatNextRun = (dateStr: string | null) => {
    if (!dateStr) return "Not scheduled";
    try {
      return new Date(dateStr).toLocaleString();
    } catch {
      return dateStr;
    }
  };

  const formatLastRun = (dateStr: string | null) => {
    if (!dateStr) return "Never";
    try {
      return new Date(dateStr).toLocaleString();
    } catch {
      return dateStr;
    }
  };

  const cronDescription = (cron: string) => {
    // Simple cron description for common patterns
    const parts = cron.split(" ");
    if (parts.length !== 5) return cron;
    
    const [minute, hour, day, month, weekday] = parts;
    
    let desc = "";
    
    if (minute === "0" && hour !== "*") {
      desc = `Daily at ${hour}:00`;
    } else if (minute !== "*" && hour !== "*") {
      desc = `Daily at ${hour}:${minute.padStart(2, "0")}`;
    } else if (hour === "*" && minute !== "*") {
      desc = `Every hour at minute ${minute}`;
    } else if (weekday !== "*") {
      const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
      const dayNames = weekday.split(",").map((w) => days[parseInt(w)]).join(", ");
      desc = `Weekly on ${dayNames} at ${hour}:${minute.padStart(2, "0")}`;
    } else {
      desc = cron;
    }
    
    return desc;
  };

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b px-4 py-3">
        <h2 className="text-lg font-semibold">Scheduled Tasks</h2>
        <div className="flex items-center gap-2">
          {schedulerHealth && (
            <div className="flex items-center gap-1.5 text-xs">
              <span className={cn("h-2 w-2 rounded-full", schedulerHealth.running ? "bg-green-500" : "bg-red-500")} />
              <span className="text-muted-foreground">
                {schedulerHealth.running ? "Running" : "Stopped"}
                {schedulerHealth.jobs_count > 0 && ` (${schedulerHealth.jobs_count} jobs)`}
              </span>
            </div>
          )}
          <Button
            size="sm"
            variant={showForm ? "default" : "outline"}
            onClick={() => setShowForm(!showForm)}
            className="h-8"
          >
            <Plus className="h-4 w-4" />
            {showForm ? "Cancel" : "New Task"}
          </Button>
          <Button
            size="icon"
            variant="ghost"
            onClick={onClose}
            className="h-8 w-8"
          >
            <ChevronDown className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Error display */}
      {error && (
        <div className="mx-4 mt-3 p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive text-sm">
          {error}
        </div>
      )}

      {/* Create Task Form */}
      {showForm && (
        <div className="mx-4 mb-4 p-4 rounded-xl border border-white/10 bg-white/3">
          <h3 className="mb-3 font-medium">Create Scheduled Task</h3>
          
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">
                Session
              </label>
              <select
                value={formSessionId ?? ""}
                onChange={(e) => setFormSessionId(e.target.value ? parseInt(e.target.value) : null)}
                className="w-full rounded-lg border border-white/10 bg-background px-3 py-2 text-sm focus:outline-none focus:border-primary/50"
              >
                <option value="">Select a session...</option>
                {sessions.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.title}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">
                Prompt <span className="text-destructive">*</span>
              </label>
              <textarea
                value={formPrompt}
                onChange={(e) => setFormPrompt(e.target.value)}
                placeholder="What should the AI do? (e.g., 'Scrape HackerNews and summarize top 5 AI articles')"
                rows={3}
                className="w-full rounded-lg border border-white/10 bg-background px-3 py-2 text-sm focus:outline-none focus:border-primary/50 placeholder:text-muted-foreground/50"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">
                Cron Expression <span className="text-destructive">*</span>
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={formCron}
                  onChange={(e) => setFormCron(e.target.value)}
                  placeholder="0 8 * * *"
                  className="flex-1 rounded-lg border border-white/10 bg-background px-3 py-2 text-sm font-mono focus:outline-none focus:border-primary/50"
                />
                <span className="text-xs text-muted-foreground px-2">
                  {cronDescription(formCron)}
                </span>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                Examples: <code className="px-1 py-0.5 rounded bg-white/5 font-mono text-xs">0 8 * * *</code> (daily 8 AM), <code className="px-1 py-0.5 rounded bg-white/5 font-mono text-xs">0 */6 * * *</code> (every 6 hours), <code className="px-1 py-0.5 rounded bg-white/5 font-mono text-xs">0 9 * * 1-5</code> (weekdays 9 AM)
              </p>
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <Button
                variant="outline"
                onClick={() => {
                  setShowForm(false);
                  setFormPrompt("");
                  setFormCron("0 8 * * *");
                  setFormSessionId(activeSessionId);
                }}
                disabled={saving}
              >
                Cancel
              </Button>
              <Button onClick={handleCreate} disabled={saving || !formPrompt.trim() || !formCron.trim() || !formSessionId}>
                {saving ? "Creating..." : "Create Task"}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Tasks List */}
      <div className="flex-1 overflow-y-auto px-4">
        {loading ? (
          <div className="flex items-center justify-center h-32">
            <div className="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent" />
          </div>
        ) : tasks.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-32 text-center text-muted-foreground">
            <ClockIcon className="h-10 w-10 opacity-30 mb-2" />
            <p className="text-sm">No scheduled tasks yet</p>
            <p className="text-xs mt-1">Create a task to run AI prompts on a schedule</p>
          </div>
        ) : (
          <div className="space-y-2">
            {tasks.map((task) => {
              const isExpanded = expandedTaskId === task.id;
              return (
                <div
                  key={task.id}
                  className={cn(
                    "rounded-xl border p-3 transition-all duration-200",
                    "bg-white/3 border-white/10",
                    task.is_active ? "hover:bg-white/5" : "opacity-50"
                  )}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium text-sm truncate">{task.prompt}</span>
                        <span className={cn(
                          "px-2 py-0.5 rounded text-[10px] font-medium",
                          task.is_active ? "bg-green-500/10 text-green-500" : "bg-gray-500/10 text-gray-400"
                        )}>
                          {task.is_active ? "Active" : "Paused"}
                        </span>
                        <span className="px-2 py-0.5 rounded text-[10px] font-mono text-muted-foreground bg-white/5">
                          {task.cron_expression}
                        </span>
                      </div>
                      
                      <div className="mt-2 flex items-center gap-3 text-xs text-muted-foreground">
                        <span className="flex items-center gap-1">
                          <Calendar className="h-3 w-3" />
                          <span>Created: {new Date(task.created_at).toLocaleDateString()}</span>
                        </span>
                        <span className="flex items-center gap-1">
                          <ClockIcon className="h-3 w-3" />
                          <span>Last: {formatLastRun(task.last_run_at)}</span>
                        </span>
                        <span className="flex items-center gap-1">
                          <Clock className="h-3 w-3" />
                          <span>Next: {formatNextRun(task.next_run_at)}</span>
                        </span>
                        <span className="flex items-center gap-1">
                          <ClockIcon className="h-3 w-3" />
                          <span>{cronDescription(task.cron_expression)}</span>
                        </span>
                      </div>

                      {isExpanded && (
                        <div className="mt-3 pt-3 border-t border-white/10 flex items-center gap-2 flex-wrap">
                          <Button
                            size="sm"
                            variant={task.is_active ? "outline" : "default"}
                            onClick={() => handleToggleActive(task)}
                            className="h-8"
                          >
                            {task.is_active ? (
                              <>
                                <Pause className="h-3.5 w-3.5 mr-1.5" />
                                Pause
                              </>
                            ) : (
                              <>
                                <Play className="h-3.5 w-3.5 mr-1.5" />
                                Resume
                              </>
                            )}
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => handleRunNow(task)}
                            className="h-8"
                          >
                            <Play className="h-3.5 w-3.5 mr-1.5" />
                            Run Now
                          </Button>
                          <Button
                            size="sm"
                            variant="destructive"
                            onClick={() => handleDelete(task.id)}
                            className="h-8"
                          >
                            <Trash2Icon className="h-3.5 w-3.5 mr-1.5" />
                            Delete
                          </Button>
                        </div>
                      )}
                    </div>
                    <button
                      onClick={() => handleToggleExpand(task.id)}
                      className={cn(
                        "p-1 rounded transition-transform",
                        isExpanded && "rotate-180"
                      )}
                      aria-label={isExpanded ? "Collapse" : "Expand"}
                    >
                      <ChevronDown className="h-4 w-4 text-muted-foreground" />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

export default ScheduledTasks;