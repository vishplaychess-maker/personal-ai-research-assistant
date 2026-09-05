/**
 * SkillsTab — UI for creating and managing user-defined skills (DB-backed).
 *
 * Skills are injected into agent prompts (L1 index: name + description) and
 * their full instructions load on demand (L2) when the agent activates them.
 * Disabled skills are excluded from prompts entirely.
 */
import { useCallback, useEffect, useState } from "react";
import { Plus, Pencil, Trash2, Sparkles, CheckCircle2, CircleDashed } from "lucide-react";
import { API } from "./api";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import { Textarea } from "./components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./components/ui/dialog";
import { cn } from "./lib/utils";
import type { UserSkill, UserSkillDetail } from "./types";

const emptyForm = {
  name: "",
  description: "",
  body: "",
  trigger_keywords: "",
};

export function SkillsTab() {
  const [skills, setSkills] = useState<UserSkill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Create / edit dialog state
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<UserSkill | null>(null);
  const [form, setForm] = useState({ ...emptyForm });
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Optimistic toggle: id currently flipping (rollback on error)
  const [togglingId, setTogglingId] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const loadSkills = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSkills(await API.listSkills());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load skills");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSkills();
  }, [loadSkills]);

  const openAdd = () => {
    setEditing(null);
    setForm({ ...emptyForm });
    setFormError(null);
    setDialogOpen(true);
  };

  const openEdit = async (s: UserSkill) => {
    setEditing(s);
    setFormError(null);
    setForm({
      name: s.name,
      description: s.description,
      body: "",
      trigger_keywords: s.trigger_keywords,
    });
    setDialogOpen(true);
    try {
      const detail: UserSkillDetail = await API.getSkill(s.id);
      setForm((prev) => ({ ...prev, body: detail.body }));
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Failed to load skill body");
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setFormError(null);
    try {
      if (editing) {
        await API.updateSkill(editing.id, form);
      } else {
        await API.createSkill(form);
      }
      setDialogOpen(false);
      await loadSkills();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Failed to save skill");
    } finally {
      setSaving(false);
    }
  };

  // Optimistic toggle with rollback on error
  const handleToggle = async (s: UserSkill) => {
    setTogglingId(s.id);
    setError(null);
    const prev = skills;
    setSkills((cur) =>
      cur.map((x) => (x.id === s.id ? { ...x, enabled: !x.enabled } : x))
    );
    try {
      const res = await API.toggleSkill(s.id);
      setSkills((cur) =>
        cur.map((x) => (x.id === s.id ? { ...x, enabled: res.enabled } : x))
      );
    } catch (err) {
      setSkills(prev); // rollback
      setError(err instanceof Error ? err.message : "Failed to toggle skill");
    } finally {
      setTogglingId(null);
    }
  };

  const handleDelete = async (s: UserSkill) => {
    if (!window.confirm(`Delete skill "${s.name}"? This cannot be undone.`)) return;
    setDeletingId(s.id);
    setError(null);
    try {
      await API.deleteSkill(s.id);
      setSkills((prev) => prev.filter((x) => x.id !== s.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete skill");
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div>
      <div className="mb-4 flex items-start justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          Create your own skills — reusable instruction playbooks the agent can
          follow. Skills are injected into agent prompts when relevant; disabled
          skills are ignored.
        </p>
        <Button size="sm" onClick={openAdd} className="shrink-0">
          <Plus className="h-4 w-4" />
          Create Skill
        </Button>
      </div>

      {error && <p className="mb-4 text-sm text-destructive">{error}</p>}

      {loading ? (
        <p className="py-4 text-center text-sm text-muted-foreground">Loading…</p>
      ) : skills.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-8 text-center">
          <Sparkles className="h-8 w-8 text-muted-foreground/60" />
          <p className="text-sm text-muted-foreground">
            No custom skills yet. Create one and it will be offered to the agent
            — skills are injected into agent prompts when relevant, and the full
            instructions load on demand.
          </p>
        </div>
      ) : (
        <div className="grid max-h-[40vh] gap-2 overflow-y-auto pr-1">
          {skills.map((s) => (
            <div
              key={s.id}
              className={cn(
                "flex items-start gap-3 rounded-xl border p-3",
                s.enabled && "border-primary/40 bg-primary/5"
              )}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate font-mono text-sm font-medium">
                    {s.name}
                  </span>
                  {s.enabled ? (
                    <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                      <CheckCircle2 className="h-3 w-3" /> Enabled
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                      <CircleDashed className="h-3 w-3" /> Disabled
                    </span>
                  )}
                </div>
                <p className="mt-0.5 truncate text-xs text-muted-foreground">
                  {s.description}
                </p>
                {s.trigger_keywords && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {s.trigger_keywords.split(",").map((kw) => (
                      <span
                        key={kw}
                        className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
                      >
                        {kw}
                      </span>
                    ))}
                  </div>
                )}
              </div>

              <Button
                size="sm"
                variant={s.enabled ? "default" : "outline"}
                onClick={() => handleToggle(s)}
                disabled={togglingId === s.id}
                title={s.enabled ? "Disable" : "Enable"}
              >
                {togglingId === s.id ? "…" : s.enabled ? "Disable" : "Enable"}
              </Button>
              <Button
                size="icon"
                variant="ghost"
                onClick={() => openEdit(s)}
                title="Edit"
              >
                <Pencil className="h-4 w-4" />
              </Button>
              <Button
                size="icon"
                variant="ghost"
                onClick={() => handleDelete(s)}
                disabled={deletingId === s.id}
                title="Delete"
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
      )}

      {/* Create / Edit dialog */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{editing ? "Edit Skill" : "Create Skill"}</DialogTitle>
            <DialogDescription>
              The name and description are always visible to the agent (L1); the
              body loads on demand when the agent activates the skill (L2).
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid gap-2">
              <Label>Name</Label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="my-skill"
                autoComplete="off"
                disabled={saving}
              />
              <p className="text-xs text-muted-foreground">
                2-49 lowercase letters, digits, hyphens or underscores.
              </p>
            </div>

            <div className="grid gap-2">
              <Label>Description</Label>
              <Input
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="What the skill does and when to use it (max 200 chars)"
                autoComplete="off"
                disabled={saving}
              />
            </div>

            <div className="grid gap-2">
              <Label>Instructions (markdown)</Label>
              <Textarea
                value={form.body}
                onChange={(e) => setForm({ ...form, body: e.target.value })}
                placeholder={"Step-by-step instructions the agent should follow…\n\nMax 8000 characters."}
                rows={8}
                className="min-h-[160px] font-mono text-xs"
                disabled={saving}
              />
              <p className="text-xs text-muted-foreground">
                {form.body.length}/8000 characters. Skill activation markers
                (e.g. [USE_SKILL: …]) are not allowed.
              </p>
            </div>

            <div className="grid gap-2">
              <Label>Trigger Keywords (optional)</Label>
              <Input
                value={form.trigger_keywords}
                onChange={(e) =>
                  setForm({ ...form, trigger_keywords: e.target.value })
                }
                placeholder="comma,separated,keywords (max 10)"
                autoComplete="off"
                disabled={saving}
              />
            </div>

            {formError && (
              <p className="text-sm text-destructive">{formError}</p>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleSave} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default SkillsTab;
