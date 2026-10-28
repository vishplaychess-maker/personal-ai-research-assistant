/**
 * Phase 1B.2R MP-3C3D3B closeout — narrow component-INTEGRATION proof that a
 * mode selected through the real `ChatArea`/`ModeSelector` UI, inside the
 * real `App` component, reaches the real `App.handleSend` ->
 * `useStreaming.startStream` call boundary with the exact expected value.
 *
 * This complements (does not duplicate) two already-passing, already-green
 * layers of coverage:
 *   - `ModeSelector.test.tsx` — isolated component behavior (capability
 *     gating, keyboard access, freeze/reset), not wired to a real send.
 *   - `durableStreaming.test.ts` ("MP-3C3D3B — mode serialization" describe
 *     block) — proves useStreaming's OWN immutable snapshot/reconnect
 *     behavior directly against the hook, not through the UI.
 *
 * Only the missing link — UI selection -> App.handleSend -> startStream's
 * actual argument — is proven here, through a real `<App/>` render (wrapped
 * in the same `ThemeProvider` `main.tsx` always uses in production;
 * `App.test.tsx`'s own bare `render(<App/>)` has a separate, pre-existing,
 * unrelated `ThemeProvider` harness gap — see the D3B closeout report for
 * the evidence — this file avoids it correctly instead of inheriting it).
 *
 * `useStreaming` is mocked here (not in `App.test.tsx`) so this file's mock
 * cannot affect any other test file's behavior.
 */
import { describe, it, expect, vi, beforeEach, afterEach, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, act } from "@testing-library/react";
import App from "../App";
import { ThemeProvider } from "../components/ThemeProvider";
import type { ChatModeCapability } from "../types";

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

const mockAPI = vi.hoisted(() => ({
  getHealth: vi.fn(),
  listSessions: vi.fn(),
  createSession: vi.fn(),
  updateSession: vi.fn(),
  deleteSession: vi.fn(),
  listMessages: vi.fn(),
  listDocuments: vi.fn(),
  listMemories: vi.fn(),
  getMemorySetting: vi.fn(),
  getCapabilities: vi.fn(),
  listChatModels: vi.fn(),
  updateSessionModel: vi.fn(),
  getUserSettings: vi.fn(),
}));
vi.mock("../api", () => ({ API: mockAPI }));

vi.mock("../MarkdownRenderer", () => ({
  MarkdownRenderer: ({ content }: { content: string }) => <div>{content}</div>,
}));

const mockUseAuth = vi.fn();
vi.mock("../AuthContext", () => ({
  useAuth: () => mockUseAuth(),
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const startStream = vi.fn();
const streamState = { isStreaming: false };
vi.mock("../useStreaming", () => ({
  useStreaming: () => ({
    isStreaming: streamState.isStreaming,
    streamedContent: "",
    durableRunId: null,
    phase: null,
    startStream,
    cancelStream: vi.fn(),
    cancelDurableRun: vi.fn(),
    resumeAfterApproval: vi.fn(),
  }),
}));

const session = (id: number, title: string) => ({
  id,
  title,
  user_id: 1,
  model: null,
  system_prompt: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
});

const authState = () => ({
  isAuthenticated: true,
  isLoading: false,
  logout: vi.fn(),
  user: { id: 1, username: "user-1", email: null, created_at: "2026-01-01T00:00:00Z" },
  token: "token-1",
  login: vi.fn(),
  register: vi.fn(),
  authError: null,
  clearError: vi.fn(),
});

const ALL_AVAILABLE: ChatModeCapability[] = [
  { mode: "chat", available: true },
  { mode: "deep_research", available: true },
  { mode: "multi_agent", available: true },
];

function settingsWith(chat_modes: ChatModeCapability[]) {
  return { llm_provider: "ollama", api_key: "", model: "", chat_modes };
}

async function renderReady(chatModes: ChatModeCapability[] = ALL_AVAILABLE, seedMessages = false) {
  mockUseAuth.mockReturnValue(authState());
  mockAPI.getHealth.mockResolvedValue({ backend: "ok", chromadb: "ok", ollama: "ok" });
  mockAPI.listSessions.mockResolvedValue([session(1, "Session One")]);
  // The bottom-bar composer (which renders regardless of `isStreaming`) only
  // appears once `messages.length > 0`; the centered/welcome composer is
  // gated on `!isStreaming` too. A caller simulating an in-flight stream
  // needs the seeded-messages composer to find the mode selector at all.
  mockAPI.listMessages.mockResolvedValue(
    seedMessages
      ? [{ id: 1, session_id: 1, role: "user", content: "hi", created_at: "2026-01-01T00:00:00Z" }]
      : [],
  );
  mockAPI.listDocuments.mockResolvedValue([]);
  mockAPI.listMemories.mockResolvedValue([]);
  mockAPI.getMemorySetting.mockResolvedValue({ enabled: true });
  mockAPI.getCapabilities.mockResolvedValue([]);
  mockAPI.listChatModels.mockResolvedValue({ models: [], error: null });
  mockAPI.getUserSettings.mockResolvedValue(settingsWith(chatModes));
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ backend: "ok", chromadb: "ok", ollama: "ok" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );

  render(
    <ThemeProvider>
      <App />
    </ThemeProvider>,
  );
  // App.tsx auto-selects the only session (`sessions[0]`) via a plain
  // `setActiveSessionId` effect that does NOT call `loadMessages` — so
  // `messages` stays `[]` (and the centered/welcome composer, gated on
  // `!hasMessages`, is what actually renders) unless a session is explicitly
  // clicked (`handleSelectSession`, which does call `loadMessages`). A caller
  // that needs the seeded messages to really land (e.g. to exercise the
  // bottom-bar composer while `isStreaming`) must click it.
  if (seedMessages) {
    // "Session One" is briefly ambiguous once auto-selected (it then also
    // appears as the chat header title) — target the first match, which is
    // the sidebar list item (rendered before the header in DOM order).
    const items = await screen.findAllByText("Session One");
    fireEvent.click(items[0]);
  }
  // Wait on the composer textarea — present in both composer variants and in
  // both streaming states (only its placeholder text changes) — as the
  // readiness signal, then hand back the mode-selector trigger.
  await screen.findByPlaceholderText(/Ask Thunder AI anything|Type your message|Generating response/);
  return screen.findByRole("button", { name: /Response mode/ });
}

function textareaOf() {
  return (
    screen.queryByPlaceholderText("Ask Thunder AI anything…") ??
    screen.getByPlaceholderText("Type your message…")
  );
}

async function openModeMenu() {
  const trigger = await screen.findByRole("button", { name: /Response mode/ });
  fireEvent.pointerDown(trigger);
  fireEvent.click(trigger);
  return trigger;
}

describe("App -> ChatArea -> ModeSelector -> startStream (MP-3C3D3B integration)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    streamState.isStreaming = false;
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it.each(["onComplete", "onError", "onCancelled"])("renders production agent progress and clears it on %s", async (terminal) => {
    await renderReady();
    fireEvent.change(textareaOf(), { target: { value: "hi" } });
    fireEvent.click(screen.getByTitle("Send message"));
    const callbacks = startStream.mock.calls[0][3];
    streamState.isStreaming = true;
    act(() => callbacks.onAgentProgress("agent_solving"));
    expect(screen.getByRole("status")).toHaveTextContent("Developing solution…");
    act(() => callbacks[terminal]({ messageId: 1, code: "TEST", detail: "Stopped" }));
    expect(screen.queryByText("Developing solution…")).not.toBeInTheDocument();
  });

  it("1. ChatArea renders the selector in the composer area, 2. defaulting to Auto", async () => {
    await renderReady();
    expect(await screen.findByRole("button", { name: /Response mode: Auto/ })).toBeTruthy();
  });

  it("3. an Auto send reaches startStream with no mode argument", async () => {
    await renderReady();
    const sendBtn = screen.getByTitle("Send message");
    fireEvent.change(textareaOf(), { target: { value: "hi" } });
    fireEvent.click(sendBtn);
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    expect(startStream.mock.calls[0][4]).toBeUndefined();
  });

  it.each([
    ["Chat", "chat"],
    ["Deep Research", "deep_research"],
    ["Multi-Agent", "multi_agent"],
  ] as const)("4-6. selecting %s then sending reaches startStream with mode=%s", async (label, canonical) => {
    await renderReady();
    const sendBtn = screen.getByTitle("Send message");
    await openModeMenu();
    const item = await screen.findByRole("menuitemradio", { name: new RegExp(label) });
    fireEvent.click(item);
    fireEvent.change(textareaOf(), { target: { value: "hi" } });
    fireEvent.click(sendBtn);
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    expect(startStream.mock.calls[0][4]).toBe(canonical);
  });

  it("7. a disabled mode cannot be selected — the next send stays Auto (no mode argument)", async () => {
    await renderReady([
      { mode: "chat", available: true },
      { mode: "deep_research", available: false, reason: "Not enabled on this deployment." },
      { mode: "multi_agent", available: true },
    ]);
    const sendBtn = screen.getByTitle("Send message");
    await openModeMenu();
    const drItem = await screen.findByRole("menuitemradio", { name: /Deep Research/ });
    fireEvent.click(drItem); // disabled — Radix must not fire onValueChange
    fireEvent.change(textareaOf(), { target: { value: "hi" } });
    fireEvent.click(sendBtn);
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    expect(startStream.mock.calls[0][4]).toBeUndefined();
  });

  it("8. selecting an option does not submit the message", async () => {
    await renderReady();
    await openModeMenu();
    const chatItem = await screen.findByRole("menuitemradio", { name: /Chat/ });
    fireEvent.click(chatItem);
    expect(startStream).not.toHaveBeenCalled();
  });

  it("9. while streaming, the selector is disabled/frozen", async () => {
    streamState.isStreaming = true;
    await renderReady(ALL_AVAILABLE, /* seedMessages */ true);
    const trigger = await screen.findByRole("button", { name: /Response mode/ });
    expect(trigger).toBeDisabled();
  });

  it("14. a capability-fetch failure preserves Auto/Chat and never enables the advanced modes", async () => {
    mockUseAuth.mockReturnValue(authState());
    mockAPI.getHealth.mockResolvedValue({ backend: "ok", chromadb: "ok", ollama: "ok" });
    mockAPI.listSessions.mockResolvedValue([session(1, "Session One")]);
    mockAPI.listMessages.mockResolvedValue([]);
    mockAPI.listDocuments.mockResolvedValue([]);
    mockAPI.listMemories.mockResolvedValue([]);
    mockAPI.getMemorySetting.mockResolvedValue({ enabled: true });
    mockAPI.getCapabilities.mockResolvedValue([]);
    mockAPI.listChatModels.mockResolvedValue({ models: [], error: null });
    mockAPI.getUserSettings.mockRejectedValue(new Error("network down"));
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    render(
      <ThemeProvider>
        <App />
      </ThemeProvider>,
    );
    await screen.findByTitle("Send message");
    await openModeMenu();
    await waitFor(() => {
      const drItem = screen.getByRole("menuitemradio", { name: /Deep Research/ });
      expect(drItem.getAttribute("aria-disabled") ?? drItem.getAttribute("data-disabled")).toBeTruthy();
    });
    const chatItem = screen.getByRole("menuitemradio", { name: /Chat/ });
    expect(chatItem.getAttribute("aria-disabled") ?? chatItem.getAttribute("data-disabled")).toBeFalsy();
  });

  it("16. no UI path sends \"auto\", null, a display label, or an internal value", async () => {
    await renderReady();
    const sendBtn = screen.getByTitle("Send message");
    await openModeMenu();
    const magItem = await screen.findByRole("menuitemradio", { name: /Multi-Agent/ });
    fireEvent.click(magItem);
    fireEvent.change(textareaOf(), { target: { value: "hi" } });
    fireEvent.click(sendBtn);
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    const modeArg = startStream.mock.calls[0][4];
    expect(modeArg).toBe("multi_agent");
    expect(modeArg).not.toBe("auto");
    expect(modeArg).not.toBeNull();
    expect(modeArg).not.toBe("Multi-Agent");
  });

  it("17. the selector does not create duplicate startStream() calls for one send", async () => {
    await renderReady();
    const sendBtn = screen.getByTitle("Send message");
    fireEvent.change(textareaOf(), { target: { value: "hi" } });
    fireEvent.click(sendBtn);
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    expect(startStream).toHaveBeenCalledTimes(1);
  });
});
