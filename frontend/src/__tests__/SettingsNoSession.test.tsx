/**
 * B-1 regression: Settings must be usable BEFORE a session exists.
 *
 * The defect was the render guard in App.tsx (`showSettings && activeSession`),
 * which made the sidebar Settings button appear dead until a conversation was
 * active - blocking PROVIDER CONFIGURATION, which lives inside Settings.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import App from "../App";
import { ThemeProvider } from "../components/ThemeProvider";

const mockAPI = vi.hoisted(() => ({
  getHealth: vi.fn(), listSessions: vi.fn(), createSession: vi.fn(),
  updateSession: vi.fn(), deleteSession: vi.fn(), listMessages: vi.fn(),
  listDocuments: vi.fn(), listMemories: vi.fn(), getMemorySetting: vi.fn(),
  getCapabilities: vi.fn(),
  listChatModels: vi.fn(), listAllProviderModels: vi.fn(),
  updateProvider: vi.fn(), updateSessionModel: vi.fn(),
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

beforeEach(() => {
  vi.resetAllMocks();
  mockAPI.getHealth.mockResolvedValue({ backend: "ok", chromadb: "ok", ollama: "ok" });
  mockAPI.listSessions.mockResolvedValue([]);   // <-- no session: none is active
  mockAPI.listMessages.mockResolvedValue([]);
  mockAPI.listDocuments.mockResolvedValue([]);
  mockAPI.listMemories.mockResolvedValue([]);
  mockAPI.getMemorySetting.mockResolvedValue({ enabled: true });
  mockAPI.getCapabilities.mockResolvedValue([]);
  mockAPI.listChatModels.mockResolvedValue({ models: [], error: null });
  mockAPI.listAllProviderModels.mockResolvedValue([]);
  mockAPI.updateProvider.mockResolvedValue({ id: 1, is_active: true });
  mockUseAuth.mockReturnValue({
    isAuthenticated: true,
    isLoading: false,
    user: { id: 1, username: "test", email: "test@test.com",
            created_at: "2026-01-01T00:00:00Z" },
    login: vi.fn(), logout: vi.fn(), register: vi.fn(), refreshUser: vi.fn(),
  });
  vi.spyOn(globalThis, "fetch").mockImplementation(() =>
    Promise.resolve(new Response(JSON.stringify({}), {
      status: 200, headers: { "Content-Type": "application/json" } })));
});

describe("Settings with no active session", () => {
  it("opens from the sidebar click and exposes provider configuration", async () => {
    render(<ThemeProvider><App /></ThemeProvider>);
    const button = await screen.findByRole("button", { name: /settings/i });
    fireEvent.click(button);
    // The modal itself must appear: the Providers tab and its actionable control.
    expect(await screen.findByRole("tab", { name: /providers/i })).toBeTruthy();
    expect(screen.getByText(/Add Provider/i)).toBeTruthy();
  });
});