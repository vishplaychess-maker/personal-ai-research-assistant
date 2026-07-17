/**
 * Phase 6C — Tests for the Authentication Screen component.
 *
 * Covers login form, register form, validation, password visibility toggle,
 * mode switch, loading state, and error display.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { AuthScreen } from "../AuthScreen";
import { AuthProvider } from "../AuthContext";

/**
 * Renders the AuthScreen wrapped in AuthProvider and waits for the
 * initial restoreSession() call to resolve so the form is interactive.
 *
 * AuthProvider calls restoreSession() on mount. Even with empty localStorage,
 * the async function takes 1 microtask, so we must await it before interacting.
 */
async function renderAuthScreen() {
  let resolveRender: () => void;
  render(
    <AuthProvider>
      <AuthScreen />
    </AuthProvider>
  );
  // Wait for the AuthProvider initial restoreSession() to settle
  await act(async () => {
    await new Promise((r) => setTimeout(r, 10));
  });
}

describe("AuthScreen", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  afterEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  // ── Rendering ─────────────────────────────────────────

  it("renders the login form by default", async () => {
    await renderAuthScreen();
    expect(screen.getByText("Sign in to your account")).toBeTruthy();
    expect(screen.getByText("Sign In")).toBeTruthy();
    expect(screen.getByPlaceholderText("your username")).toBeTruthy();
    expect(screen.getByPlaceholderText("your password")).toBeTruthy();
  });

  it("renders the AI Research Assistant title", async () => {
    await renderAuthScreen();
    expect(screen.getByText("AI Research Assistant")).toBeTruthy();
  });

  it("shows switch to register link", async () => {
    await renderAuthScreen();
    expect(screen.getByText("Don't have an account?")).toBeTruthy();
    expect(screen.getByText("Create one")).toBeTruthy();
  });

  // ── Mode switch ───────────────────────────────────────

  it("switches to register form when Create one is clicked", async () => {
    await renderAuthScreen();
    fireEvent.click(screen.getByText("Create one"));
    expect(screen.getByText("Create a new account")).toBeTruthy();
    expect(screen.getByText("Create Account")).toBeTruthy();
    expect(screen.getByPlaceholderText("your@email.com")).toBeTruthy();
    expect(screen.getByPlaceholderText("at least 8 characters")).toBeTruthy();
    expect(screen.getByPlaceholderText("repeat your password")).toBeTruthy();
  });

  it("switches back to login form when Sign in is clicked", async () => {
    await renderAuthScreen();
    fireEvent.click(screen.getByText("Create one"));
    fireEvent.click(screen.getByText("Sign in"));
    expect(screen.getByText("Sign in to your account")).toBeTruthy();
  });

  // ── Client-side validation ────────────────────────────

  it("shows validation error for empty username", async () => {
    await renderAuthScreen();
    fireEvent.click(screen.getByText("Sign In"));
    expect(screen.getByText("Username is required")).toBeTruthy();
  });

  it("shows validation error for short username", async () => {
    await renderAuthScreen();
    fireEvent.change(screen.getByPlaceholderText("your username"), { target: { value: "ab" } });
    fireEvent.click(screen.getByText("Sign In"));
    expect(screen.getByText("Username must be at least 3 characters")).toBeTruthy();
  });

  it("shows validation error for empty password", async () => {
    await renderAuthScreen();
    fireEvent.change(screen.getByPlaceholderText("your username"), { target: { value: "testuser" } });
    fireEvent.click(screen.getByText("Sign In"));
    expect(screen.getByText("Password is required")).toBeTruthy();
  });

  it("shows validation error for short password", async () => {
    await renderAuthScreen();
    fireEvent.change(screen.getByPlaceholderText("your username"), { target: { value: "testuser" } });
    fireEvent.change(screen.getByPlaceholderText("your password"), { target: { value: "short" } });
    fireEvent.click(screen.getByText("Sign In"));
    expect(screen.getByText("Password must be at least 8 characters")).toBeTruthy();
  });

  // ── Register validation ───────────────────────────────

  it("shows validation error for empty email in register mode", async () => {
    await renderAuthScreen();
    fireEvent.click(screen.getByText("Create one"));
    fireEvent.change(screen.getByPlaceholderText("your username"), { target: { value: "newuser" } });
    fireEvent.click(screen.getByText("Create Account"));
    expect(screen.getByText("Email is required")).toBeTruthy();
  });

  it("shows validation error for invalid email in register mode", async () => {
    await renderAuthScreen();
    fireEvent.click(screen.getByText("Create one"));
    fireEvent.change(screen.getByPlaceholderText("your username"), { target: { value: "newuser" } });
    fireEvent.change(screen.getByPlaceholderText("your@email.com"), { target: { value: "not-an-email" } });
    fireEvent.change(screen.getByPlaceholderText("at least 8 characters"), { target: { value: "password123" } });
    fireEvent.click(screen.getByText("Create Account"));
    expect(screen.getByText("Please enter a valid email address")).toBeTruthy();
  });

  it("shows validation error when passwords do not match in register mode", async () => {
    await renderAuthScreen();
    fireEvent.click(screen.getByText("Create one"));
    fireEvent.change(screen.getByPlaceholderText("your username"), { target: { value: "newuser" } });
    fireEvent.change(screen.getByPlaceholderText("your@email.com"), { target: { value: "new@test.com" } });
    fireEvent.change(screen.getByPlaceholderText("at least 8 characters"), { target: { value: "password123" } });
    fireEvent.change(screen.getByPlaceholderText("repeat your password"), { target: { value: "different456" } });
    fireEvent.click(screen.getByText("Create Account"));
    expect(screen.getByText("Passwords do not match")).toBeTruthy();
  });

  // ── Password visibility toggle ────────────────────────

  it("toggles password visibility", async () => {
    await renderAuthScreen();
    const passwordInput = screen.getByPlaceholderText("your password") as HTMLInputElement;
    expect(passwordInput.type).toBe("password");

    const toggleBtn = screen.getByLabelText("Show password");
    fireEvent.click(toggleBtn);
    expect(passwordInput.type).toBe("text");

    fireEvent.click(screen.getByLabelText("Hide password"));
    expect(passwordInput.type).toBe("password");
  });

  // ── Error display ─────────────────────────────────────

  it("displays auth error from context", async () => {
    // Mock fetch to handle restoreSession (returns null since no token)
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid credentials" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      })
    );

    await renderAuthScreen();
    fireEvent.change(screen.getByPlaceholderText("your username"), { target: { value: "testuser" } });
    fireEvent.change(screen.getByPlaceholderText("your password"), { target: { value: "password123" } });
    fireEvent.click(screen.getByText("Sign In"));

    await waitFor(() => {
      expect(screen.getByText("Invalid credentials")).toBeTruthy();
    });
  });

  // ── Loading state ─────────────────────────────────────

  it("shows loading state during submission", async () => {
    // Mock restoreSession (returns null immediately since no token)
    vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise(() => {}));

    await renderAuthScreen();
    fireEvent.change(screen.getByPlaceholderText("your username"), { target: { value: "testuser" } });
    fireEvent.change(screen.getByPlaceholderText("your password"), { target: { value: "password123" } });
    fireEvent.click(screen.getByText("Sign In"));

    await waitFor(() => {
      expect(screen.getByText("Signing in…")).toBeTruthy();
    });
  });

  // ── Successful login ──────────────────────────────────

  it("performs successful login flow", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((url: string | URL | Request) => {
      const urlStr = url.toString();
      if (urlStr.includes("/api/auth/login")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ access_token: "success-token", token_type: "bearer" }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      if (urlStr.includes("/api/auth/me")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ id: 2, username: "testuser", email: "test@example.com", created_at: "2026-01-01T00:00:00Z" }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    await renderAuthScreen();
    fireEvent.change(screen.getByPlaceholderText("your username"), { target: { value: "testuser" } });
    fireEvent.change(screen.getByPlaceholderText("your password"), { target: { value: "password123" } });
    fireEvent.click(screen.getByText("Sign In"));

    await waitFor(() => {
      expect(screen.queryByText("Signing in…")).toBeNull();
    });
  });

  // ── Successful registration ───────────────────────────

  it("performs registration and auto-login", async () => {
    let loginCalled = false;
    vi.spyOn(globalThis, "fetch").mockImplementation((url: string | URL | Request, options?: RequestInit) => {
      const urlStr = url.toString();

      if (urlStr.includes("/api/auth/register")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ id: 3, username: "newuser", email: "new@test.com", created_at: "2026-01-01T00:00:00Z" }),
            { status: 201, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      if (urlStr.includes("/api/auth/login")) {
        loginCalled = true;
        return Promise.resolve(
          new Response(
            JSON.stringify({ access_token: "new-token", token_type: "bearer" }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      if (urlStr.includes("/api/auth/me")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ id: 3, username: "newuser", email: "new@test.com", created_at: "2026-01-01T00:00:00Z" }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    await renderAuthScreen();
    // Switch to register
    fireEvent.click(screen.getByText("Create one"));

    fireEvent.change(screen.getByPlaceholderText("your username"), { target: { value: "newuser" } });
    fireEvent.change(screen.getByPlaceholderText("your@email.com"), { target: { value: "new@test.com" } });
    fireEvent.change(screen.getByPlaceholderText("at least 8 characters"), { target: { value: "password123" } });
    fireEvent.change(screen.getByPlaceholderText("repeat your password"), { target: { value: "password123" } });
    fireEvent.click(screen.getByText("Create Account"));

    await waitFor(() => {
      expect(loginCalled).toBe(true);
    });
  });
});
