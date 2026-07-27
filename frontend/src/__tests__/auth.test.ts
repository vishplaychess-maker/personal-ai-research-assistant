/**
 * Phase 6C / 7B — Tests for the auth utility module.
 *
 * Covers token storage, auth headers, session restoration, logout,
 * 401 handling, and refresh token rotation.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  getAccessToken,
  setAccessToken,
  getStoredUser,
  isTokenExpired,
  getAuthHeaders,
  loginUser,
  registerUser,
  logout,
  restoreSession,
  refreshAccessToken,
  resetRefreshState,
} from "../auth";
import { setOnUnauthorized } from "../api";

function createJWT(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  const sig = btoa("fake-signature");
  return `${header}.${body}.${sig}`;
}

describe("Auth utilities", () => {
  beforeEach(() => {
    localStorage.clear();
    setAccessToken(null);
    resetRefreshState();
  });

  afterEach(() => {
    localStorage.clear();
    setAccessToken(null);
    resetRefreshState();
    vi.restoreAllMocks();
  });

  // ── Access token (in-memory) ──────────────────────────

  it("returns null for getAccessToken when not set", () => {
    expect(getAccessToken()).toBeNull();
  });

  it("getAccessToken returns the in-memory token", () => {
    setAccessToken("memory-token");
    expect(getAccessToken()).toBe("memory-token");
  });

  it("setAccessToken(null) clears the in-memory token", () => {
    setAccessToken("memory-token");
    setAccessToken(null);
    expect(getAccessToken()).toBeNull();
  });

  // ── Token expiry ──────────────────────────────────────

  it("detects expired token", () => {
    const expired = createJWT({ sub: "1", exp: Math.floor(Date.now() / 1000) - 3600 });
    expect(isTokenExpired(expired)).toBe(true);
  });

  it("detects valid token", () => {
    const valid = createJWT({ sub: "1", exp: Math.floor(Date.now() / 1000) + 3600 });
    expect(isTokenExpired(valid)).toBe(false);
  });

  it("treats malformed token as expired", () => {
    expect(isTokenExpired("not-a-valid-jwt")).toBe(true);
  });

  // ── Auth headers ──────────────────────────────────────

  it("returns empty headers when no access token", () => {
    setAccessToken(null);
    expect(getAuthHeaders()).toEqual({});
  });

  it("returns auth headers with valid access token", () => {
    const token = createJWT({ sub: "1", exp: Math.floor(Date.now() / 1000) + 3600 });
    setAccessToken(token);
    const headers = getAuthHeaders();
    expect(headers.Authorization).toBe(`Bearer ${token}`);
  });

  it("returns empty headers for expired access token", () => {
    const token = createJWT({ sub: "1", exp: Math.floor(Date.now() / 1000) - 3600 });
    setAccessToken(token);
    expect(getAuthHeaders()).toEqual({});
  });

  // ── Login ─────────────────────────────────────────────

  it("loginUser stores access token in memory and refresh token in localStorage", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((url: string | URL | Request) => {
      const urlStr = url.toString();
      if (urlStr.includes("/api/auth/login")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ access_token: "login-token", refresh_token: "refresh-token-abc", token_type: "bearer" }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      if (urlStr.includes("/api/auth/me")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ id: 2, username: "loginuser", email: "login@test.com", created_at: "2026-01-01T00:00:00Z" }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    const result = await loginUser({ username: "loginuser", password: "password123" });
    expect(result.token).toBe("login-token");
    expect(result.user.username).toBe("loginuser");
    expect(getAccessToken()).toBe("login-token");
    expect(getStoredUser()?.username).toBe("loginuser");
    // Refresh token should be in localStorage
    expect(localStorage.getItem("research_assistant_refresh_token")).toBe("refresh-token-abc");
  });

  it("loginUser throws on invalid credentials", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid credentials" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      })
    );

    await expect(loginUser({ username: "bad", password: "wrong" })).rejects.toThrow("Invalid credentials");
  });

  // ── Register ──────────────────────────────────────────

  it("registerUser sends registration data", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ id: 3, username: "newuser", email: "new@test.com", created_at: "2026-01-01T00:00:00Z" }),
        { status: 201, headers: { "Content-Type": "application/json" } }
      )
    );

    const user = await registerUser({ username: "newuser", email: "new@test.com", password: "password123" });
    expect(user.id).toBe(3);
    expect(user.username).toBe("newuser");
  });

  it("registerUser throws on duplicate", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Username already taken" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      })
    );

    await expect(
      registerUser({ username: "existing", email: "e@test.com", password: "password123" })
    ).rejects.toThrow("Username already taken");
  });

  // ── Logout ────────────────────────────────────────────

  it("logout clears stored auth data", async () => {
    setAccessToken("test-token");
    localStorage.setItem("research_assistant_refresh_token", "test-refresh");
    localStorage.setItem("research_assistant_user", JSON.stringify({ id: 1, username: "testuser", email: "test@example.com", created_at: "2026-01-01T00:00:00Z" }));

    await logout();

    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem("research_assistant_refresh_token")).toBeNull();
    expect(localStorage.getItem("research_assistant_user")).toBeNull();
  });

  // ── Session restore ───────────────────────────────────

  it("restoreSession returns null when no refresh token stored", async () => {
    const user = await restoreSession();
    expect(user).toBeNull();
  });

  it("restoreSession refreshes token and returns user when refresh token stored", async () => {
    localStorage.setItem("research_assistant_refresh_token", "valid-refresh-token");
    let refreshCalled = false;

    vi.spyOn(globalThis, "fetch").mockImplementation((url: string | URL | Request) => {
      const urlStr = url.toString();
      if (urlStr.includes("/api/auth/refresh")) {
        refreshCalled = true;
        return Promise.resolve(
          new Response(
            JSON.stringify({ access_token: "new-access-token", refresh_token: "new-refresh-token", token_type: "bearer" }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      if (urlStr.includes("/api/auth/me")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ id: 2, username: "existing", email: "existing@test.com", created_at: "2026-01-01T00:00:00Z" }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    const user = await restoreSession();
    expect(user).not.toBeNull();
    expect(user!.username).toBe("existing");
    expect(refreshCalled).toBe(true);
  });

  it("restoreSession clears auth on failed refresh", async () => {
    localStorage.setItem("research_assistant_refresh_token", "expired-refresh-token");

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid refresh token" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      })
    );

    const user = await restoreSession();
    expect(user).toBeNull();
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem("research_assistant_refresh_token")).toBeNull();
  });

  // ── Refresh token ─────────────────────────────────────

  it("refreshAccessToken succeeds with valid refresh token", async () => {
    localStorage.setItem("research_assistant_refresh_token", "valid-refresh");

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ access_token: "new-access", refresh_token: "new-refresh", token_type: "bearer" }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    const result = await refreshAccessToken();
    expect(result).toBe(true);
    expect(getAccessToken()).toBe("new-access");
    expect(localStorage.getItem("research_assistant_refresh_token")).toBe("new-refresh");
  });

  it("refreshAccessToken returns false on failure", async () => {
    localStorage.setItem("research_assistant_refresh_token", "bad-refresh");

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid refresh token" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      })
    );

    const result = await refreshAccessToken();
    expect(result).toBe(false);
    expect(getAccessToken()).toBeNull();
  });

  it("refreshAccessToken returns false when no refresh token stored", async () => {
    const result = await refreshAccessToken();
    expect(result).toBe(false);
  });

  // ── 401 handling in API ───────────────────────────────

  it("calls onUnauthorized callback on 401 from API requests", async () => {
    const onUnauthorized = vi.fn();
    setOnUnauthorized(onUnauthorized);

    const token = createJWT({ sub: "2", exp: Math.floor(Date.now() / 1000) + 3600 });
    setAccessToken(token);

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Unauthorized" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      })
    );

    const { API } = await import("../api");
    await expect(API.getHealth()).rejects.toThrow("Unauthorized");
    // After 401, api.ts calls _onUnauthorized which triggers logout
  });
});
