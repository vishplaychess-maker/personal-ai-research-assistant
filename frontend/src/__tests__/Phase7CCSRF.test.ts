/**
 * Phase 7C — HttpOnly Refresh Cookie & CSRF Protection (frontend).
 *
 * Covers:
 * - No refresh token stored in localStorage
 * - Login uses credentials: include
 * - Refresh uses credentials: include
 * - restoreSession uses cookie-based refresh (no localStorage token)
 * - State-changing request sends X-CSRF-Token (double-submit)
 * - GET request does not send X-CSRF-Token
 * - Logout uses credentials and clears frontend state
 * - Expired refresh clears authentication state
 * - Current session list no longer sends X-Refresh-Token
 * - Concurrent refresh remains serialized
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  getAccessToken,
  setAccessToken,
  getStoredUser,
  refreshAccessToken,
  restoreSession,
  logout,
  listAuthSessions,
  getCsrfToken,
  resetRefreshState,
} from "../auth";
import { API } from "../api";

const USER_KEY = "research_assistant_user";
const LEGACY_REFRESH_KEY = "research_assistant_refresh_token";
const CSRF_COOKIE = "research_assistant_csrf_token";

function createJWT(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  const sig = btoa("fake-signature");
  return `${header}.${body}.${sig}`;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setDocumentCookie(name: string, value: string): void {
  document.cookie = `${name}=${value}; path=/`;
}

const testUser = {
  id: 1,
  username: "csrf_user",
  email: "csrf@test.com",
  created_at: "2026-01-01T00:00:00Z",
};

describe("Phase 7C — CSRF double-submit header", () => {
  beforeEach(() => {
    localStorage.clear();
    setAccessToken(null);
    resetRefreshState();
    // Clear document cookies between tests
    document.cookie = `${CSRF_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  });

  afterEach(() => {
    localStorage.clear();
    setAccessToken(null);
    resetRefreshState();
    document.cookie = `${CSRF_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    vi.restoreAllMocks();
  });

  it("getCsrfToken reads the CSRF cookie", () => {
    setDocumentCookie(CSRF_COOKIE, "csrf-token-abc");
    expect(getCsrfToken()).toBe("csrf-token-abc");
  });

  it("getCsrfToken returns null when the cookie is absent", () => {
    expect(getCsrfToken()).toBeNull();
  });

  it("state-changing request sends X-CSRF-Token", async () => {
    const validJwt = createJWT({
      sub: "1",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    setAccessToken(validJwt);
    setDocumentCookie(CSRF_COOKIE, "csrf-token-abc");

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        id: 1,
        title: "New",
        user_id: 1,
        model: null,
        system_prompt: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      })
    );

    await API.createSession("CSRF test session");

    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe("/api/sessions");
    expect(init?.method).toBe("POST");
    const headers = (init?.headers as Record<string, string>) ?? {};
    expect(headers["X-CSRF-Token"]).toBe("csrf-token-abc");
    expect(init?.credentials).toBe("include");
  });

  it("GET request does not send X-CSRF-Token", async () => {
    const validJwt = createJWT({
      sub: "1",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    setAccessToken(validJwt);
    setDocumentCookie(CSRF_COOKIE, "csrf-token-abc");

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ backend: "ok", chromadb: "ok", ollama: "ok" })
    );

    await API.getHealth();

    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe("/api/health");
    const headers = (init?.headers as Record<string, string>) ?? {};
    expect(headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("no X-CSRF-Token header when the CSRF cookie is missing", async () => {
    const validJwt = createJWT({
      sub: "1",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    setAccessToken(validJwt);

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        id: 1,
        title: "New",
        user_id: 1,
        model: null,
        system_prompt: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      })
    );

    await API.createSession("no-csrf-cookie");
    const [, init] = fetchSpy.mock.calls[0];
    const headers = (init?.headers as Record<string, string>) ?? {};
    expect(headers["X-CSRF-Token"]).toBeUndefined();
  });
});

describe("Phase 7C — cookie-based auth flow", () => {
  beforeEach(() => {
    localStorage.clear();
    setAccessToken(null);
    resetRefreshState();
    document.cookie = `${CSRF_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  });

  afterEach(() => {
    localStorage.clear();
    setAccessToken(null);
    resetRefreshState();
    document.cookie = `${CSRF_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    vi.restoreAllMocks();
  });

  it("no refresh token is ever written to localStorage", async () => {
    setAccessToken("access-123");
    localStorage.setItem(USER_KEY, JSON.stringify(testUser));

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: string | URL | Request) => {
        const u = url.toString();
        if (u.includes("/api/auth/refresh")) {
          return Promise.resolve(
            jsonResponse({ access_token: "fresh", token_type: "bearer" })
          );
        }
        if (u.includes("/api/auth/me")) {
          return Promise.resolve(jsonResponse(testUser));
        }
        return Promise.resolve(jsonResponse({}));
      }
    );

    await restoreSession();
    expect(localStorage.getItem(LEGACY_REFRESH_KEY)).toBeNull();
    expect(Object.keys(localStorage).some((k) => k.toLowerCase().includes("refresh"))).toBe(false);
    // No X-Refresh-Token header and no refresh token in the body either.
    for (const [, init] of fetchSpy.mock.calls) {
      const headers = (init?.headers as Record<string, string>) ?? {};
      expect(headers["X-Refresh-Token"]).toBeUndefined();
      expect(String(init?.body ?? "")).not.toContain("refresh_token");
    }
  });

  it("restoreSession relies on the HttpOnly cookie (credentials include, no token sent)", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: string | URL | Request) => {
        const u = url.toString();
        if (u.includes("/api/auth/refresh")) {
          return Promise.resolve(
            jsonResponse({ access_token: "fresh", token_type: "bearer" })
          );
        }
        if (u.includes("/api/auth/me")) {
          return Promise.resolve(jsonResponse(testUser));
        }
        return Promise.resolve(jsonResponse({}));
      }
    );

    const user = await restoreSession();
    expect(user?.username).toBe("csrf_user");
    const refreshCall = fetchSpy.mock.calls.find(([url]) =>
      String(url).includes("/api/auth/refresh")
    );
    expect(refreshCall).toBeDefined();
    expect(refreshCall![1]?.credentials).toBe("include");
    // Body empty — refresh token comes from the cookie.
    expect(JSON.parse(String(refreshCall![1]?.body))).toEqual({});
  });

  it("concurrent refresh requests remain serialized", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ access_token: "new-access", token_type: "bearer" })
    );

    const [r1, r2] = await Promise.all([
      refreshAccessToken(),
      refreshAccessToken(),
    ]);
    expect(r1).toBe(true);
    expect(r2).toBe(true);
    const refreshCalls = fetchSpy.mock.calls.filter(([url]) =>
      String(url).includes("/api/auth/refresh")
    );
    expect(refreshCalls).toHaveLength(1);
  });

  it("expired refresh clears authentication state", async () => {
    localStorage.setItem(USER_KEY, JSON.stringify(testUser));
    setAccessToken("stale");

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "Invalid refresh token" }, 401)
    );

    const ok = await refreshAccessToken();
    expect(ok).toBe(false);
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem(USER_KEY)).toBeNull();
    expect(getStoredUser()).toBeNull();
  });

  it("logout uses credentials include and clears frontend state", async () => {
    setAccessToken("access-123");
    localStorage.setItem(USER_KEY, JSON.stringify(testUser));

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "Logged out successfully" })
    );

    await logout();
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem(USER_KEY)).toBeNull();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe("/api/auth/logout");
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("include");
  });

  it("session list no longer sends X-Refresh-Token", async () => {
    const validJwt = createJWT({
      sub: "1",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    setAccessToken(validJwt);

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ sessions: [], total: 0, active_count: 0 })
    );

    await listAuthSessions();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe("/api/auth/sessions");
    const headers = (init?.headers as Record<string, string>) ?? {};
    expect(headers["X-Refresh-Token"]).toBeUndefined();
    expect(init?.credentials).toBe("include");
  });
});
