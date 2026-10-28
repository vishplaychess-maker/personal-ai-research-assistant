/**
 * AUG-1 regression: the Settings > Skills tab rendered ONLY user-created
 * skills (GET /api/skills), so the bundled SKILL.md playbooks were invisible.
 * Reproduced in the running app (127.0.0.1:5173): the Settings modal opened,
 * the Skills tab was visible and clickable, yet the panel showed
 * "No custom skills yet", 0 skill cards, and the only API call was
 * /api/skills -> 200 []. The fix renders the built-in catalog
 * (GET /api/skills/catalog) as a separate, read-only section.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { SkillsTab } from "../SkillsTab";

const mockAPI = vi.hoisted(() => ({
  listSkills: vi.fn(),
  listBuiltinSkills: vi.fn(),
}));
vi.mock("../api", () => ({ API: mockAPI }));

const BUILTINS = [
  { name: "web-research", description: "Perform deep web research.", pinned: false },
  { name: "code-review", description: "Review code for bugs.", pinned: false },
  { name: "browser-automation", description: "Drive a real browser.", pinned: false },
  { name: "commit-message", description: "Generate commit messages.", pinned: false },
  { name: "knowledge-graph", description: "Query the knowledge graph.", pinned: false },
  { name: "image-generation", description: "Generate AI images from a text prompt.", pinned: false },
  { name: "video-generation", description: "Generate short AI videos from a text prompt.", pinned: false },
];

const USER_SKILL = {
  id: 1, name: "my-playbook", description: "Mine", trigger_keywords: "alpha",
  enabled: true, created_at: "2026-01-01T00:00:00", updated_at: "2026-01-01T00:00:00",
};

beforeEach(() => {
  vi.resetAllMocks();
});

describe("SkillsTab built-in skills", () => {
  it("AUG-1: shows built-in skills even when the user has none", async () => {
    mockAPI.listSkills.mockResolvedValue([]);
    mockAPI.listBuiltinSkills.mockResolvedValue({ enabled: true, skills: BUILTINS });
    render(<SkillsTab />);

    expect(await screen.findByTestId("builtin-skills")).toBeTruthy();
    expect(screen.getAllByTestId("builtin-skill")).toHaveLength(7);
    expect(screen.getByText("web-research")).toBeTruthy();
    expect(screen.getByText("Your skills")).toBeTruthy();
    expect(screen.getByText(/No custom skills yet/i)).toBeTruthy();
  });

  it("distinguishes user-created skills from built-in ones", async () => {
    mockAPI.listSkills.mockResolvedValue([USER_SKILL]);
    mockAPI.listBuiltinSkills.mockResolvedValue({ enabled: true, skills: BUILTINS });
    render(<SkillsTab />);

    expect(await screen.findByTestId("builtin-skills")).toBeTruthy();
    expect(screen.getByText("my-playbook")).toBeTruthy();
    expect(screen.getAllByText("Built-in").length).toBeGreaterThan(0);
  });

  it("shows a clear disabled explanation instead of rendering nothing", async () => {
    mockAPI.listSkills.mockResolvedValue([]);
    mockAPI.listBuiltinSkills.mockResolvedValue({ enabled: false, skills: [] });
    render(<SkillsTab />);

    expect(await screen.findByTestId("builtin-skills-disabled")).toBeTruthy();
    expect(screen.queryAllByTestId("builtin-skill")).toHaveLength(0);
    expect(screen.getByText(/thunder_skills_enabled=false/)).toBeTruthy();
  });

  it("keeps user skills usable when the catalog call fails", async () => {
    mockAPI.listSkills.mockResolvedValue([USER_SKILL]);
    mockAPI.listBuiltinSkills.mockRejectedValue(new Error("404"));
    render(<SkillsTab />);

    expect(await screen.findByText("my-playbook")).toBeTruthy();
    expect(screen.queryByTestId("builtin-skills-disabled")).toBeNull();
    expect(screen.queryAllByTestId("builtin-skill")).toHaveLength(0);
  });
});
