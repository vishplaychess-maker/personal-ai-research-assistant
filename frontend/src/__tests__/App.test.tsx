/**
 * App smoke test — verifies the top-level component renders without crashing.
 *
 * Phase 5C — Search and Frontend Refactoring
 * Phase 6C — Auth gating (mock pre-authenticated for backward-compat tests)
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import App from "../App";

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
    vi.restoreAllMocks();
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

    const { container } = render(<App />);
    expect(container).toBeTruthy();
  });

  it("renders the sidebar with Research Sessions title", () => {
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

    render(<App />);
    expect(screen.getByText("Research Sessions")).toBeTruthy();
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

    render(<App />);
    expect(screen.getByText("Select or create a session")).toBeTruthy();
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

    render(<App />);
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

    render(<App />);
    expect(screen.getByText("✚ New")).toBeTruthy();
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

    render(<App />);
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

    render(<App />);
    expect(screen.getByText("Sign in to your account")).toBeTruthy();
  });
});
