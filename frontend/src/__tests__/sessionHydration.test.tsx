/// <reference types="vite/client" />
import { StrictMode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

// Vite `?raw` import keeps the source-level guards free of Node builtins, so this
// file typechecks under the DOM-only tsconfig that `npm run build` uses.
import appSource from "../App.tsx?raw";
import App from "../App";
import { ThemeProvider } from "../components/ThemeProvider";

/** D6C-J4 session hydration.
 *
 * PROVEN DEFECT (now FIXED): the mount effect restored the active session but
 * never hydrated its messages, so a page reload showed "0 messages" and an empty
 * conversation even though the DB held the user Message and
 * GET /api/sessions/{id}/messages returned it. Only clicking the session row
 * (handleSelectSession -> loadMessages) rendered it.
 *
 * The source-level guards below started life as an `it.fails` RED regression and
 * are now normal passing tests. The behavioural tests then pin the required
 * contract: hydrate once, never twice under StrictMode, never with zero sessions,
 * never replacing an active session, never letting a late hydration response
 * overwrite a newer selection, and never mutating state after logout.
 *
 * `ThemeProvider` wraps `App` exactly as `main.tsx` does in production
 * (`Sidebar` calls `useTheme()`).
 */

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

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

const session = (id: number, title: string) => ({
  id,
  title,
  user_id: 1,
  model: null,
  system_prompt: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
});

const message = (id: number, sessionId: number, content: string) => ({
  id,
  session_id: sessionId,
  role: "user",
  content,
  image_url: null,
  citations: null,
  confidence: null,
  confidence_reason: null,
  created_at: "2026-01-01T00:00:00Z",
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

const renderApp = () => render(<ThemeProvider><App /></ThemeProvider>);
// The active session is asserted from the conversation header, which renders
// activeSession.title (ChatArea.tsx). The older .sidebar-item.active selector no
// longer matches the current Sidebar markup (it is now a listbox of rows).
const activeTitle = () =>
  document.querySelector("main h1")?.textContent
  ?? document.querySelector("h1")?.textContent
  ?? null;

/** Isolate EXACTLY the mount effect, from its useEffect( to its dependency array.
 *  A plain character window is not safe here: handleSelectSession sits a few lines
 *  below and calls loadMessages itself, so a wide window would make the defect
 *  assertion pass spuriously (verified: a +/-500 char window did). */
function autoSelectEffect(): string {
  const marker = "const firstId = sessions[0].id;";
  const at = appSource.indexOf(marker);
  if (at === -1) return "";
  const start = appSource.lastIndexOf("useEffect(", at);
  const end = appSource.indexOf("}, [", at);
  if (start === -1 || end === -1) return "";
  return appSource.slice(start, end);
}

beforeEach(() => {
  vi.resetAllMocks();
  mockUseAuth.mockImplementation(() => authState());
  mockAPI.getHealth.mockResolvedValue({ backend: "ok", chromadb: "ok", ollama: "ok" });
  mockAPI.listSessions.mockResolvedValue([]);
  mockAPI.listMessages.mockResolvedValue([]);
  mockAPI.listDocuments.mockResolvedValue([]);
  mockAPI.listMemories.mockResolvedValue([]);
  mockAPI.getMemorySetting.mockResolvedValue({ enabled: true });
  mockAPI.getCapabilities.mockResolvedValue([]);
  mockAPI.listChatModels.mockResolvedValue({ models: [], error: null });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("session hydration source guards (D6C-J4)", () => {
  it("restores the most recent session as active on mount", () => {
    expect(appSource).toContain("const firstId = sessions[0].id;");
    expect(appSource).toContain("setActiveSessionId(firstId);");
  });

  it("hydrates messages on the explicit click path", () => {
    const at = appSource.indexOf("const handleSelectSession");
    expect(at).toBeGreaterThan(-1);
    expect(appSource.slice(at, at + 400)).toContain("loadMessages(");
  });

  it("control: the effect extraction isolates the effect and excludes the click path", () => {
    const block = autoSelectEffect();
    expect(block).not.toBe("");
    expect(block).toContain("const firstId = sessions[0].id;");
    expect(block).not.toContain("handleSelectSession");
  });

  it("hydrates messages for the auto-restored session on mount", () => {
    const block = autoSelectEffect();
    expect(block).not.toBe("");
    expect(block).toContain("loadMessages(firstId)");
  });
});

describe("session hydration behaviour (D6C-J4)", () => {
  it("loads and renders the first session's messages without any click", async () => {
    mockAPI.listSessions.mockResolvedValue([session(1, "Session One"), session(2, "Session Two")]);
    mockAPI.listMessages.mockImplementation((id: number) =>
      Promise.resolve(id === 1 ? [message(11, 1, "hydrated-first-session")] : []));

    renderApp();

    expect(await screen.findByText("hydrated-first-session")).toBeTruthy();
    expect(mockAPI.listMessages).toHaveBeenCalledWith(1);
    expect(activeTitle()).toBe("Session One");
    expect(screen.getByText("1 message")).toBeTruthy();
    expect(screen.queryByText("0 messages")).toBeNull();
  });

  it("requests messages exactly once under StrictMode", async () => {
    mockAPI.listSessions.mockResolvedValue([session(1, "Session One")]);
    mockAPI.listMessages.mockResolvedValue([message(11, 1, "strictmode-once")]);

    render(<StrictMode><ThemeProvider><App /></ThemeProvider></StrictMode>);

    expect(await screen.findByText("strictmode-once")).toBeTruthy();
    expect(mockAPI.listMessages).toHaveBeenCalledTimes(1);
  });

  it("requests no messages when the account has no sessions", async () => {
    mockAPI.listSessions.mockResolvedValue([]);

    renderApp();

    await waitFor(() => expect(mockAPI.listSessions).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 25));
    expect(mockAPI.listMessages).not.toHaveBeenCalled();
  });

  it("does not replace an already-active session or re-hydrate it", async () => {
    mockAPI.listSessions.mockResolvedValue([session(1, "Session One"), session(2, "Session Two")]);
    mockAPI.listMessages.mockImplementation((id: number) =>
      Promise.resolve(id === 1 ? [message(11, 1, "one-body")] : [message(22, 2, "two-body")]));

    const { rerender } = renderApp();
    expect(await screen.findByText("one-body")).toBeTruthy();

    fireEvent.click(screen.getByText("Session Two"));
    expect(await screen.findByText("two-body")).toBeTruthy();
    expect(activeTitle()).toBe("Session Two");

    rerender(<ThemeProvider><App /></ThemeProvider>);
    await new Promise((resolve) => setTimeout(resolve, 25));

    // still session two, and no extra hydration for the already-active selection
    expect(activeTitle()).toBe("Session Two");
    expect(screen.getByText("two-body")).toBeTruthy();
    expect(screen.queryByText("one-body")).toBeNull();
    expect(mockAPI.listMessages).toHaveBeenCalledTimes(2);
  });

  it("still loads the clicked session on the manual path", async () => {
    mockAPI.listSessions.mockResolvedValue([session(1, "Session One"), session(2, "Session Two")]);
    mockAPI.listMessages.mockImplementation((id: number) =>
      Promise.resolve(id === 1 ? [message(11, 1, "one-body")] : [message(22, 2, "two-body")]));

    renderApp();
    expect(await screen.findByText("one-body")).toBeTruthy();

    fireEvent.click(screen.getByText("Session Two"));

    expect(await screen.findByText("two-body")).toBeTruthy();
    expect(mockAPI.listMessages).toHaveBeenCalledWith(2);
    expect(activeTitle()).toBe("Session Two");
  });

  it("does not let a late hydration response overwrite a newer selection", async () => {
    let releaseFirst!: (value: unknown) => void;
    const firstLoad = new Promise((resolve) => { releaseFirst = resolve; });
    mockAPI.listSessions.mockResolvedValue([session(1, "Session One"), session(2, "Session Two")]);
    mockAPI.listMessages.mockImplementation((id: number) =>
      id === 1 ? firstLoad : Promise.resolve([message(22, 2, "two-body")]));

    renderApp();
    await screen.findByText("Session Two");
    fireEvent.click(screen.getByText("Session Two"));
    expect(await screen.findByText("two-body")).toBeTruthy();

    // the slow hydration for session 1 finally lands, after the manual selection
    releaseFirst([message(11, 1, "stale-one-body")]);
    await new Promise((resolve) => setTimeout(resolve, 25));

    expect(screen.queryByText("stale-one-body")).toBeNull();
    expect(screen.getByText("two-body")).toBeTruthy();
    expect(activeTitle()).toBe("Session Two");
  });

  it("discards an in-flight message load after logout", async () => {
    let releaseLoad!: (value: unknown) => void;
    const pending = new Promise((resolve) => { releaseLoad = resolve; });
    // widened so the logged-out shape (user/token null) is assignable
    let currentAuth: Record<string, unknown> = authState();
    mockUseAuth.mockImplementation(() => currentAuth);
    mockAPI.listSessions.mockResolvedValue([session(1, "Session One")]);
    mockAPI.listMessages.mockImplementation(() => pending);

    const { rerender } = renderApp();
    await waitFor(() => expect(mockAPI.listMessages).toHaveBeenCalled());

    currentAuth = { ...authState(), isAuthenticated: false, user: null, token: null };
    rerender(<ThemeProvider><App /></ThemeProvider>);
    await screen.findByText("Sign in to your account");

    releaseLoad([message(11, 1, "post-logout-leak")]);
    await new Promise((resolve) => setTimeout(resolve, 25));

    expect(screen.queryByText("post-logout-leak")).toBeNull();
    expect(screen.getByText("Sign in to your account")).toBeTruthy();
  });
});
