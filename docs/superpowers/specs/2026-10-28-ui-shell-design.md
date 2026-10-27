# UI shell upgrade — 3-column layout + 3 themes (design)

Status: approved 2026-10-28 (phased delivery).
Reference: user-provided premium chat component (ChatGPT Matte / Obsidian /
Daylight Paper), adopted for layout and feel, not for its mock data.

## Decisions

1. **Phased.** Phase 1 = shell + themes + composer, existing chat rendering
   untouched. Phase 2 = message rendering (typography, code blocks, thought
   accordion). Phase 3 = inspector integrations.
2. **Themes are CSS variables.** The reference's exact hex values become
   `[data-theme=…]` blocks in `index.css`, mapped onto the existing semantic
   tokens (`--background`, `--foreground`, `--card`, `--muted`, `--border`,
   `--primary`…). Existing components restyle themselves; only the code-block
   and send-button colours stay literal, as in the reference.
3. **Navigation lives in the sidebar** (reference shape): new thread, Knowledge
   Graph, Memories, Documents, MCP, Terminal, Media, then date-grouped history;
   Settings/Sign out in the bottom user pill.

## Compatibility constraints

* Many existing components use `dark:` utilities. `ThemeProvider` therefore
  sets **both** `data-theme` and the legacy `.dark` class for matte/obsidian,
  and drops `.dark` for paper. Removing `.dark` without this bridge would break
  half the app visually.
* Stored theme migration: `light` → `paper`, `dark` → `matte`; unknown →
  `matte`. Storage key unchanged (`research-assistant-theme`).
* Auth/CSRF untouched: every call keeps going through `api.ts`.
* `DESKTOP_AUTOMATION_ENABLED=false` (the API answers 503) hides the Code
  Canvas tab; nothing else in the inspector depends on it.

## Phase 1 scope

* `index.css`: three `[data-theme]` variable sets.
* `ThemeProvider`: three themes + migration + legacy class bridge.
* `shell/Header.tsx`: sidebar toggle, theme switcher, inspector toggle, share.
* `shell/Inspector.tsx`: collapsed right drawer with tab placeholders; Code
  Canvas tab only when desktop automation is enabled.
* `App.tsx`: wrap `Sidebar` + `ChatArea` in the 3-column shell; sidebar
  collapse state; inspector open state. `ChatArea` keeps its streaming,
  uploads, citations and retry logic.
* Composer: restyle the existing textareas (rounded-3xl container, auto-resize,
  existing Search/Deep Research/attach controls) in place — no logic move in
  phase 1.

## Phase 2 scope

Message list components: reference typography, code block with Copy / "Open in
Inspector", citation pills, thought accordion fed by real `agent_status`,
`plan`, `steps` frames (hidden when a turn produced none).

## Phase 3 scope

Inspector tabs wired to real data: execution frames, `GET /api/mcp/servers`,
`/api/desktop/tree|read` for the code canvas, and document counts for the vector
section (ChromaDB exposes no status endpoint; the panel says so rather than
inventing telemetry).

## Verification

Per phase: `npx tsc --noEmit`, `npm run build`, existing vitest suite, and a
browser pass (theme switch over a dialog and the chat, sidebar collapse,
send/stop, inspector toggle). No backend contract changes.
