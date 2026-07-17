/**
 * Phase 6C — Tests for the auth utility module.
 *
 * Covers token storage, auth headers, session restoration, logout,
 * and 401 handling.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  getStoredToken,
  getStoredUser,
  storeAuth,
  clearAuth,
  isTokenExpired,
  getAuthHeaders,
  loginUser,
  registerUser,
  logout,
  restoreSession,
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
  });

  afterEach(() => {
    localStorage.clear();
  });

  // ── Token storage ─────────────────────────────────────

  it("returns null for getStoredToken when no token exists", () => {
    expect(getStoredToken()).toBeNull();
  });

  it("returns null for getStoredUser when no user exists", () => {
    expect(getStoredUser()).toBeNull();
  });

  it("stores and retrieves token and user", () => {
    storeAuth("test-token", {
      id: 1,
      username: "testuser",
      email: "test@example.com",
      created_at: "2026-01-01T00:00:00Z",
    });
    expect(getStoredToken()).toBe("test-token");
    expect(getStoredUser()).toEqual({
      id: 1,
      username: "testuser",
      email: "test@example.com",
      created_at: "2026-01-01T00:00:00Z",
    });
  });

  it("clears stored auth data", () => {
    storeAuth("test-token", {
      id: 1,
      username: "testuser",
      email: "test@example.com",
      created_at: "2026-01-01T00:00:00Z",
    });
    clearAuth();
    expect(getStoredToken()).toBeNull();
    expect(getStoredUser()).toBeNull();
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

  it("returns empty headers when no token is stored", () => {
    expect(getAuthHeaders()).toEqual({});
  });

  it("returns auth headers with valid token", () => {
    const token = createJWT({ sub: "1", exp: Math.floor(Date.now() / 1000) + 3600 });
    storeAuth(token, {
      id: 1,
      username: "testuser",
      email: "test@example.com",
      created_at: "2026-01-01T00:00:00Z",
    });
    const headers = getAuthHeaders();
    expect(headers.Authorization).toBe(`Bearer ${token}`);
  });

  it("returns empty headers for expired token", () => {
    const token = createJWT({ sub: "1", exp: Math.floor(Date.now() / 1000) - 3600 });
    storeAuth(token, {
      id: 1,
      username: "testuser",
      email: "test@example.com",
      created_at: "2026-01-01T00:00:00Z",
    });
    expect(getAuthHeaders()).toEqual({});
  });

  // ── Login / Register ──────────────────────────────────

  it("loginUser stores token and user on success", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((url: string | URL | Request) => {
      const urlStr = url.toString();
      if (urlStr.includes("/api/auth/login")) {
        return Promise.resolve(
          new Response(JSON.stringify({ access_token: "login-token", token_type: "bearer" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
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
    expect(getStoredToken()).toBe("login-token");
    expect(getStoredUser()?.username).toBe("loginuser");
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

    await expect(registerUser({ username: "existing", email: "e@test.com", password: "password123" })).rejects.toThrow(
      "Username already taken"
    );
  });

  // ── Logout ────────────────────────────────────────────

  it("logout clears stored auth data", () => {
    storeAuth("test-token", {
      id: 1,
      username: "testuser",
      email: "test@example.com",
      created_at: "2026-01-01T00:00:00Z",
    });
    logout();
    expect(getStoredToken()).toBeNull();
    expect(getStoredUser()).toBeNull();
  });

  // ── Session restore ───────────────────────────────────

  it("restoreSession returns null when no token stored", async () => {
    const user = await restoreSession();
    expect(user).toBeNull();
  });

  it("restoreSession returns user when valid token stored", async () => {
    const token = createJWT({ sub: "2", exp: Math.floor(Date.now() / 1000) + 3600 });
    storeAuth(token, {
      id: 2,
      username: "existing",
      email: "existing@test.com",
      created_at: "2026-01-01T00:00:00Z",
    });

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ id: 2, username: "existing", email: "existing@test.com", created_at: "2026-01-01T00:00:00Z" }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    const user = await restoreSession();
    expect(user).not.toBeNull();
    expect(user!.username).toBe("existing");
  });

  it("restoreSession clears auth on 401", async () => {
    const token = createJWT({ sub: "2", exp: Math.floor(Date.now() / 1000) + 3600 });
    storeAuth(token, {
      id: 2,
      username: "existing",
      email: "existing@test.com",
      created_at: "2026-01-01T00:00:00Z",
    });

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Not authenticated" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      })
    );

    const user = await restoreSession();
    expect(user).toBeNull();
    expect(getStoredToken()).toBeNull();
  });

  // ── 401 handling in API ───────────────────────────────

  it("calls onUnauthorized callback on 401 from API requests", async () => {
    const onUnauthorized = vi.fn();
    setOnUnauthorized(onUnauthorized);

    // Keep the token so getAuthToken returns it
    const token = createJWT({ sub: "2", exp: Math.floor(Date.now() / 1000) + 3600 });
    storeAuth(token, {
      id: 2,
      username: "existing",
      email: "existing@test.com",
      created_at: "2026-01-01T00:00:00Z",
    });

    // Mock the API request function — we'll test through the API object
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Unauthorized" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      })
    );

    // Import the API module directly
    const { API } = await import("../api");
    await expect(API.getHealth()).rejects.toThrow("Unauthorized");
    expect(onUnauthorized).toHaveBeenCalled();
  });
});
