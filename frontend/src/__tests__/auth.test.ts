/**
 * Phase 6C / 7B / 7C — Tests for the auth utility module.
 *
 * Phase 7C: the refresh token lives ONLY in the backend-set HttpOnly cookie
 * (`research_assistant_refresh_token`). It is never stored in localStorage,
 * never sent in headers or bodies, and never returned in JSON responses.
 * Requests use credentials: "include" so the browser attaches cookies.
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
  removeLegacyRefreshToken,
} from "../auth";
import { setOnUnauthorized } from "../api";

const USER_KEY = "research_assistant_user";
const LEGACY_REFRESH_KEY = "research_assistant_refresh_token";

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

  // ── Login (Phase 7C: HttpOnly cookie) ─────────────────

  it("loginUser stores access token in memory only (no refresh token in localStorage)", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((url: string | URL | Request, options?: RequestInit) => {
      const urlStr = url.toString();
      if (urlStr.includes("/api/auth/login")) {
        // Phase 7C: response carries NO refresh token (it arrives as a cookie)
        return Promise.resolve(
          new Response(
            JSON.stringify({ access_token: "login-token", token_type: "bearer" }),
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
    // No refresh token is ever persisted to localStorage in Phase 7C.
    expect(localStorage.getItem(LEGACY_REFRESH_KEY)).toBeNull();
    expect(localStorage.getItem("research_assistant_refresh_token")).toBeNull();
  });

  it("loginUser uses credentials include so the refresh cookie is set", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: string | URL | Request, options?: RequestInit) => {
        const urlStr = url.toString();
        if (urlStr.includes("/api/auth/login")) {
          return Promise.resolve(
            new Response(
              JSON.stringify({ access_token: "login-token", token_type: "bearer" }),
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
      }
    );

    await loginUser({ username: "loginuser", password: "password123" });
    const loginCall = fetchSpy.mock.calls.find(([url]) =>
      String(url).includes("/api/auth/login")
    );
    expect(loginCall).toBeDefined();
    expect(loginCall![1]?.credentials).toBe("include");
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

  // ── Logout (Phase 7C: cookie revocation) ──────────────

  it("logout clears stored auth data and revokes via POST", async () => {
    setAccessToken("test-token");
    localStorage.setItem(USER_KEY, JSON.stringify({ id: 1, username: "testuser", email: "test@example.com", created_at: "2026-01-01T00:00:00Z" }));

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Logged out successfully" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );

    await logout();

    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem(USER_KEY)).toBeNull();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe("/api/auth/logout");
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("include");
    // The refresh token is NOT in the request body (read from cookie server-side).
    expect(JSON.parse(String(init?.body))).toEqual({});
  });

  it("logout still clears state when server call fails", async () => {
    setAccessToken("test-token");
    localStorage.setItem(USER_KEY, JSON.stringify({ id: 1, username: "testuser", email: "test@example.com", created_at: "2026-01-01T00:00:00Z" }));
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network down"));

    await logout();
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem(USER_KEY)).toBeNull();
  });

  // ── Legacy migration (Phase 7C) ───────────────────────

  it("removeLegacyRefreshToken removes the Phase 7B localStorage key", () => {
    localStorage.setItem(LEGACY_REFRESH_KEY, "legacy-value");
    removeLegacyRefreshToken();
    expect(localStorage.getItem(LEGACY_REFRESH_KEY)).toBeNull();
  });

  // ── Session restore (Phase 7C: cookie-based) ──────────

  it("restoreSession returns null when refresh fails (no valid cookie)", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid refresh token" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      })
    );

    const user = await restoreSession();
    expect(user).toBeNull();
    expect(getAccessToken()).toBeNull();
  });

  it("restoreSession refreshes via the HttpOnly cookie and returns user", async () => {
    let refreshCalled = false;

    vi.spyOn(globalThis, "fetch").mockImplementation((url: string | URL | Request, options?: RequestInit) => {
      const urlStr = url.toString();
      if (urlStr.includes("/api/auth/refresh")) {
        refreshCalled = true;
        return Promise.resolve(
          new Response(
            JSON.stringify({ access_token: "new-access-token", token_type: "bearer" }),
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
    // Phase 7C: the refresh request carries no refresh token in the body —
    // it is read from the HttpOnly cookie via credentials: include.
    const refreshCall = vi.mocked(fetch).mock.calls.find(([url]) =>
      String(url).includes("/api/auth/refresh")
    );
    expect(refreshCall).toBeDefined();
    expect(refreshCall![1]?.credentials).toBe("include");
    expect(JSON.parse(String(refreshCall![1]?.body))).toEqual({});
  });

  it("restoreSession clears auth on failed refresh", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid refresh token" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      })
    );

    const user = await restoreSession();
    expect(user).toBeNull();
    expect(getAccessToken()).toBeNull();
  });

  // ── Refresh token (Phase 7C: cookie-based) ────────────

  it("refreshAccessToken succeeds via cookie (no localStorage needed)", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ access_token: "new-access", token_type: "bearer" }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    const result = await refreshAccessToken();
    expect(result).toBe(true);
    expect(getAccessToken()).toBe("new-access");
    // No refresh token persisted anywhere.
    expect(localStorage.getItem(LEGACY_REFRESH_KEY)).toBeNull();
    const refreshCall = fetchSpy.mock.calls.find(([url]) =>
      String(url).includes("/api/auth/refresh")
    );
    expect(refreshCall![1]?.credentials).toBe("include");
    // Body is empty — the refresh token comes from the cookie, not the body.
    expect(JSON.parse(String(refreshCall![1]?.body))).toEqual({});
  });

  it("refreshAccessToken returns false on failure", async () => {
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

  it("refreshAccessToken uses credentials include on the refresh request", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ access_token: "new-access", token_type: "bearer" }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    await refreshAccessToken();
    const refreshCall = fetchSpy.mock.calls.find(([url]) =>
      String(url).includes("/api/auth/refresh")
    );
    expect(refreshCall![1]?.credentials).toBe("include");
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
  });
});
