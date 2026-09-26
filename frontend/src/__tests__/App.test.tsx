/**
 * App smoke test — verifies the top-level component renders without crashing.
 *
 * Phase 5C — Search and Frontend Refactoring
 * Phase 6C — Auth gating (mock pre-authenticated for backward-compat tests)
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import App from "../App";
import { ThemeProvider } from "../components/ThemeProvider";

// D6C-J4 harness repair (test-only, no production change): `main.tsx` always mounts
// `ThemeProvider` around `App`, and `Sidebar` calls `useTheme()`, so rendering
// `<App />` bare threw "useTheme must be used within a ThemeProvider" and no
// assertion in this file could run. Same wrapper ModePropagation.test.tsx uses.
const renderApp = () => render(<ThemeProvider><App /></ThemeProvider>);

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
  listChatModels: vi.fn(),
  listAllProviderModels: vi.fn(),
  updateProvider: vi.fn(),
  updateSessionModel: vi.fn(),
}));

vi.mock("../api", () => ({ API: mockAPI }));

vi.mock("../MarkdownRenderer", () => ({
  MarkdownRenderer: ({ content }: { content: string }) => <div>{content}</div>,
}));

// Mock useAuth at the module level to control auth state in tests
const mockUseAuth = vi.fn();
vi.mock("../AuthContext", () => ({
  useAuth: () => mockUseAuth(),
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
}));

// JSDOM does not implement scrollIntoView
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

describe("App", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockAPI.getHealth.mockResolvedValue({ backend: "ok", chromadb: "ok", ollama: "ok" });
    mockAPI.listSessions.mockResolvedValue([]);
    mockAPI.listMessages.mockResolvedValue([]);
    mockAPI.listDocuments.mockResolvedValue([]);
    mockAPI.listMemories.mockResolvedValue([]);
    mockAPI.getMemorySetting.mockResolvedValue({ enabled: true });
    mockAPI.listChatModels.mockResolvedValue({ models: [], error: null });
    // Current ModelSelector contract: provider-group array from
    // GET /api/providers/models (API.listAllProviderModels).
    mockAPI.listAllProviderModels.mockResolvedValue([]);
    mockAPI.updateProvider.mockResolvedValue({ id: 1, is_active: true });
    // Mock fetch for health and API endpoints
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ backend: "ok", chromadb: "ok", ollama: "ok" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
    );
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders without crashing", () => {
    mockUseAuth.mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      logout: vi.fn(),
      user: { id: 1, username: "test", email: "test@test.com", created_at: "2026-01-01T00:00:00Z" },
      token: "mock-token",
      login: vi.fn(),
      register: vi.fn(),
      authError: null,
      clearError: vi.fn(),
    });

    const { container } = renderApp();
    expect(container).toBeTruthy();
  });

  it("renders the sidebar landmark and brand", () => {
    mockUseAuth.mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      logout: vi.fn(),
      user: { id: 1, username: "test", email: "test@test.com", created_at: "2026-01-01T00:00:00Z" },
      token: "mock-token",
      login: vi.fn(),
      register: vi.fn(),
      authError: null,
      clearError: vi.fn(),
    });

    renderApp();
    // The "Research Sessions" header was removed; the current sidebar contract is
    // its <aside> landmark plus the brand label.
    expect(screen.getByRole("complementary")).toBeTruthy();
    expect(screen.getByText("Thunder AI")).toBeTruthy();
  });

  it("renders the chat area with select session prompt", () => {
    mockUseAuth.mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      logout: vi.fn(),
      user: { id: 1, username: "test", email: "test@test.com", created_at: "2026-01-01T00:00:00Z" },
      token: "mock-token",
      login: vi.fn(),
      register: vi.fn(),
      authError: null,
      clearError: vi.fn(),
    });

    renderApp();
    expect(
      screen.getByText("Select a session from the sidebar or create a new one to begin.")
    ).toBeTruthy();
  });

  it("renders health indicators", () => {
    mockUseAuth.mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      logout: vi.fn(),
      user: { id: 1, username: "test", email: "test@test.com", created_at: "2026-01-01T00:00:00Z" },
      token: "mock-token",
      login: vi.fn(),
      register: vi.fn(),
      authError: null,
      clearError: vi.fn(),
    });

    renderApp();
    expect(screen.getByText("API")).toBeTruthy();
    expect(screen.getByText("LLM")).toBeTruthy();
  });

  it("renders the new session button", () => {
    mockUseAuth.mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      logout: vi.fn(),
      user: { id: 1, username: "test", email: "test@test.com", created_at: "2026-01-01T00:00:00Z" },
      token: "mock-token",
      login: vi.fn(),
      register: vi.fn(),
      authError: null,
      clearError: vi.fn(),
    });

    renderApp();
    expect(screen.getByTitle("New session")).toBeTruthy();
  });

  it("shows loading screen when auth is loading", () => {
    mockUseAuth.mockReturnValue({
      isAuthenticated: false,
      isLoading: true,
      logout: vi.fn(),
      user: null,
      token: null,
      login: vi.fn(),
      register: vi.fn(),
      authError: null,
      clearError: vi.fn(),
    });

    renderApp();
    expect(screen.getByText("Restoring session…")).toBeTruthy();
  });

  it("shows auth screen when not authenticated and not loading", () => {
    mockUseAuth.mockReturnValue({
      isAuthenticated: false,
      isLoading: false,
      logout: vi.fn(),
      user: null,
      token: null,
      login: vi.fn(),
      register: vi.fn(),
      authError: null,
      clearError: vi.fn(),
    });

    renderApp();
    expect(screen.getByText("Sign in to your account")).toBeTruthy();
  });

  const session = (id: number, title: string, model: string | null = null) => ({
    id,
    title,
    user_id: 1,
    model,
    system_prompt: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  });

  const authState = (userId: number) => ({
    isAuthenticated: true,
    isLoading: false,
    logout: vi.fn(),
    user: { id: userId, username: `user-${userId}`, email: null, created_at: "2026-01-01T00:00:00Z" },
    token: `token-${userId}`,
    login: vi.fn(),
    register: vi.fn(),
    authError: null,
    clearError: vi.fn(),
  });

  it("clears a stale selected session and refreshes the list only once after a 404", async () => {
    mockUseAuth.mockReturnValue(authState(1));
    mockAPI.listSessions
      .mockResolvedValueOnce([session(1, "Session One"), session(2, "Session Two")])
      .mockResolvedValueOnce([session(1, "Session One")]);
    // D6C-J4: the mount now hydrates the auto-selected session as well, so the 404
    // must be keyed to the SESSION rather than to call order (call-order stubbing
    // would otherwise fail session 1's hydration instead of session 2's click).
    mockAPI.listMessages.mockImplementation((id: number) =>
      id === 2 ? Promise.reject(new Error("Session 2 not found")) : Promise.resolve([]));

    renderApp();
    await screen.findByText("Session Two");
    fireEvent.click(screen.getByText("Session Two"));

    // SEMANTIC CHANGE: the 404 handler sets App.chatError = "Session is no
    // longer available" AND clears the active session; the no-active-session
    // empty state does not render chatError, and the recovery hydration of
    // Session One then clears it. That text is therefore no longer user-visible,
    // so this asserts the observable outcome instead.
    await waitFor(() => expect(screen.queryByText("Session Two")).toBeNull());
    // Current active-session contract: the conversation header (<main> h1).
    await waitFor(() => expect(document.querySelector("main h1")?.textContent).toBe("Session One"));
    await new Promise((resolve) => setTimeout(resolve, 20));
    // the LIST still refreshes exactly once (initial load + the single 404 recovery)
    expect(mockAPI.listSessions).toHaveBeenCalledTimes(2);
    // one hydration for the auto-selected session 1, one click-driven load for 2
    expect(mockAPI.listMessages).toHaveBeenCalledTimes(2);
  });

  it("clears account-specific state and reloads sessions when the authenticated user changes", async () => {
    let currentAuth = authState(1);
    mockUseAuth.mockImplementation(() => currentAuth);
    mockAPI.listSessions
      .mockResolvedValueOnce([session(11, "User One Session")])
      .mockResolvedValueOnce([session(22, "User Two Session")]);

    const { rerender } = renderApp();
    expect((await screen.findAllByText("User One Session")).length).toBeGreaterThan(0);

    currentAuth = authState(2);
    rerender(<ThemeProvider><App /></ThemeProvider>);

    expect((await screen.findAllByText("User Two Session")).length).toBeGreaterThan(0);
    expect(screen.queryByText("User One Session")).toBeNull();
    expect(mockAPI.listSessions).toHaveBeenCalledTimes(2);
  });

  it("uses the backend-returned ID and resets a new session to Default model", async () => {
    mockUseAuth.mockReturnValue(authState(1));
    mockAPI.listSessions.mockResolvedValue([session(7, "Existing Session", "mistral:7b")]);
    mockAPI.createSession.mockResolvedValue(session(42, "Backend Session", null));

    renderApp();
    await screen.findByText("mistral:7b");
    fireEvent.click(screen.getByTitle("New session"));

    await waitFor(() => expect(document.querySelector("main h1")?.textContent).toBe("Backend Session"));
    expect(screen.getByText("Default model")).toBeTruthy();
  });

  it("does not let a delayed model save overwrite the session selected afterward", async () => {
    mockUseAuth.mockReturnValue(authState(1));
    mockAPI.listSessions.mockResolvedValue([session(1, "Session One"), session(2, "Session Two")]);
    mockAPI.listAllProviderModels.mockResolvedValue([
      {
        provider: "ollama",
        provider_label: "Ollama",
        provider_id: 1,
        models: [{ name: "mistral:7b", size: null, modified_at: null, is_free: true }],
      },
    ]);
    let finishSave!: () => void;
    mockAPI.updateSessionModel.mockReturnValue(new Promise<void>((resolve) => { finishSave = resolve; }));

    renderApp();
    await screen.findByText("Default model");
    fireEvent.click(screen.getByText("Default model"));
    const modelOption = await screen.findByRole("option", { name: /mistral:7b/i });
    fireEvent.click(modelOption);
    await screen.findByText("Saving…");

    fireEvent.click(screen.getByText("Session Two"));
    finishSave();

    await waitFor(() => expect(screen.getByText("Default model")).toBeTruthy());
    expect(screen.getAllByText("Session Two").length).toBeGreaterThan(0);
  });
});
