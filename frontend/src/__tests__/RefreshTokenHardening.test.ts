/**
 * Phase 7B / 7C Hardening — Refresh-token hardening tests.
 *
 * Phase 7C: the refresh token lives ONLY in the backend HttpOnly cookie.
 * There is no X-Refresh-Token header and no localStorage persistence.
 * State-changing requests send X-CSRF-Token (double-submit) and use
 * credentials: "include".
 *
 * Covers:
 * - No refresh token stored in localStorage
 * - Session-list request sends no X-Refresh-Token (cookie identifies session)
 * - Other API requests never send refresh tokens in headers/query/body
 * - Concurrent refresh requests are serialized
 * - Refresh cooldown blocks rapid repeated calls
 * - Expired refresh token clears local authentication state
 * - Logout request remains correct (POST, credentials include, no token body)
 * - Page restore still works after refresh-token rotation
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
  resetRefreshState,
  setOnInvalidRefresh,
} from "../auth";
import { API } from "../api";

const USER_KEY = "research_assistant_user";
const LEGACY_REFRESH_KEY = "research_assistant_refresh_token";

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

const testUser = {
  id: 1,
  username: "hardening_user",
  email: "h@test.com",
  created_at: "2026-01-01T00:00:00Z",
};

describe("Phase 7C — no refresh token in localStorage / headers", () => {
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

  it("no refresh token is stored in localStorage at any point", async () => {
    const validJwt = createJWT({
      sub: "1",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    setAccessToken(validJwt);
    localStorage.setItem(USER_KEY, JSON.stringify(testUser));

    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((url: string | URL | Request) => {
        const u = url.toString();
        if (u.includes("/api/auth/refresh")) {
          return Promise.resolve(
            jsonResponse({ access_token: "fresh-access", token_type: "bearer" })
          );
        }
        if (u.includes("/api/auth/sessions")) {
          return Promise.resolve(
            jsonResponse({ sessions: [], total: 0, active_count: 0 })
          );
        }
        return Promise.resolve(
          jsonResponse({ backend: "ok", chromadb: "ok", ollama: "ok" })
        );
      });

    await API.getHealth();
    await listAuthSessions();

    // The legacy Phase 7B key must never be re-created.
    expect(localStorage.getItem(LEGACY_REFRESH_KEY)).toBeNull();
    // And no other key resembling a refresh token exists.
    const keys = Object.keys(localStorage);
    expect(keys.some((k) => k.toLowerCase().includes("refresh"))).toBe(false);
    // No fetch ever carries a refresh token in a header or query.
    for (const [url, init] of fetchSpy.mock.calls) {
      const headers = (init?.headers as Record<string, string>) ?? {};
      expect(headers["X-Refresh-Token"]).toBeUndefined();
      expect(String(url)).not.toContain("refresh_token");
      expect(String(url)).not.toContain("refresh=");
    }
  });

  it("session-list request sends NO X-Refresh-Token and uses credentials", async () => {
    const validJwt = createJWT({
      sub: "1",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    setAccessToken(validJwt);

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        sessions: [
          {
            id: 1,
            created_at: "2026-01-01T00:00:00Z",
            last_used_at: "2026-01-01T00:00:00Z",
            expires_at: "2026-02-01T00:00:00Z",
            revoked_at: null,
            device_info: "ip:127.0.0.1",
            is_current: true,
          },
        ],
        total: 1,
        active_count: 1,
      })
    );

    const sessions = await listAuthSessions();
    expect(sessions).toHaveLength(1);
    expect(sessions[0].is_current).toBe(true);

    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe("/api/auth/sessions");
    const headers = (init?.headers as Record<string, string>) ?? {};
    // Phase 7C: no X-Refresh-Token header — the HttpOnly cookie identifies
    // the current session on the backend.
    expect(headers["X-Refresh-Token"]).toBeUndefined();
    expect(init?.credentials).toBe("include");
    expect(headers["Authorization"]).toBe(`Bearer ${validJwt}`);
  });

  it("session-list works after auto-refresh when access token is expired", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: string | URL | Request) => {
        const u = url.toString();
        if (u.includes("/api/auth/refresh")) {
          return Promise.resolve(
            jsonResponse({ access_token: "fresh-access", token_type: "bearer" })
          );
        }
        if (u.includes("/api/auth/sessions")) {
          return Promise.resolve(
            jsonResponse({ sessions: [], total: 0, active_count: 0 })
          );
        }
        return Promise.resolve(jsonResponse({}));
      }
    );

    const sessions = await listAuthSessions();
    expect(sessions).toEqual([]);
    const sessionsCall = fetchSpy.mock.calls.find(([url]) =>
      String(url).includes("/api/auth/sessions")
    );
    expect(sessionsCall).toBeDefined();
    const headers = (sessionsCall![1]?.headers as Record<string, string>) ?? {};
    expect(headers["X-Refresh-Token"]).toBeUndefined();
    expect(sessionsCall![1]?.credentials).toBe("include");
  });

  it("session-list never sends refresh tokens via query params", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: string | URL | Request) => {
        const u = url.toString();
        if (u.includes("/api/auth/refresh")) {
          return Promise.resolve(
            jsonResponse({ access_token: "fresh-access", token_type: "bearer" })
          );
        }
        return Promise.resolve(jsonResponse({ sessions: [], total: 0, active_count: 0 }));
      }
    );
    await listAuthSessions();
    const sessionsCall = fetchSpy.mock.calls.find(([url]) =>
      String(url).includes("/api/auth/sessions")
    );
    const url = String(sessionsCall![0]);
    expect(url).not.toContain("refresh_token");
    expect(url).not.toContain("?");
  });

  it("other API requests never send X-Refresh-Token or refresh in body", async () => {
    const validJwt = createJWT({
      sub: "1",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    setAccessToken(validJwt);

    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((url: string | URL | Request) => {
        const u = url.toString();
        if (u.includes("/api/sessions")) {
          return Promise.resolve(
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
        }
        return Promise.resolve(
          jsonResponse({ backend: "ok", chromadb: "ok", ollama: "ok" })
        );
      });

    await API.getHealth();
    await API.createSession("Hardening test");

    expect(fetchSpy.mock.calls.length).toBeGreaterThanOrEqual(2);
    for (const [url, init] of fetchSpy.mock.calls) {
      const headers = (init?.headers as Record<string, string>) ?? {};
      expect(headers["X-Refresh-Token"]).toBeUndefined();
      expect(String(url)).not.toContain("refresh_token");
      const body = String(init?.body ?? "");
      expect(body).not.toContain("refresh_token");
      expect(init?.credentials).toBe("include");
    }
  });
});

describe("Phase 7B/7C — refresh serialization & cooldown", () => {
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

  it("concurrent refresh requests are serialized (single request)", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        access_token: "new-access",
        token_type: "bearer",
      })
    );

    const [r1, r2] = await Promise.all([
      refreshAccessToken(),
      refreshAccessToken(),
    ]);

    expect(r1).toBe(true);
    expect(r2).toBe(true);
    expect(getAccessToken()).toBe("new-access");
    // Exactly one network refresh should have been issued.
    const refreshCalls = fetchSpy.mock.calls.filter(([url]) =>
      String(url).includes("/api/auth/refresh")
    );
    expect(refreshCalls).toHaveLength(1);
  });

  it("refresh cooldown blocks rapid repeated calls", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        access_token: "new-access",
        token_type: "bearer",
      })
    );

    expect(await refreshAccessToken()).toBe(true);
    expect(await refreshAccessToken()).toBe(false);
    const refreshCalls = fetchSpy.mock.calls.filter(([url]) =>
      String(url).includes("/api/auth/refresh")
    );
    expect(refreshCalls).toHaveLength(1);
  });

  it("expired refresh token clears local authentication state", async () => {
    const onInvalid = vi.fn();
    setOnInvalidRefresh(onInvalid);
    localStorage.setItem(USER_KEY, JSON.stringify(testUser));
    setAccessToken("stale-access-token");

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "Invalid refresh token" }, 401)
    );

    const result = await refreshAccessToken();
    expect(result).toBe(false);
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem(USER_KEY)).toBeNull();
    expect(getStoredUser()).toBeNull();
    expect(onInvalid).toHaveBeenCalledTimes(1);
  });

  it("logout request remains correct (POST, credentials include, empty body)", async () => {
    setAccessToken("access-token-123");

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "Logged out successfully" })
    );

    await logout();

    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe("/api/auth/logout");
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("include");
    const headers = (init?.headers as Record<string, string>) ?? {};
    expect(headers["Authorization"]).toBe("Bearer access-token-123");
    // Phase 7C: refresh token is read from the HttpOnly cookie, not the body.
    expect(JSON.parse(String(init?.body))).toEqual({});
    expect(headers["X-Refresh-Token"]).toBeUndefined();
  });

  it("logout clears state even when no access token is available", async () => {
    localStorage.setItem(USER_KEY, JSON.stringify(testUser));
    setAccessToken(null);

    const fetchSpy = vi.spyOn(globalThis, "fetch");
    await logout();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem(USER_KEY)).toBeNull();
  });

  it("page restore still works after refresh-token rotation", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: string | URL | Request) => {
        const u = url.toString();
        if (u.includes("/api/auth/refresh")) {
          return Promise.resolve(
            jsonResponse({ access_token: "fresh-access", token_type: "bearer" })
          );
        }
        if (u.includes("/api/auth/me")) {
          return Promise.resolve(jsonResponse(testUser));
        }
        return Promise.resolve(jsonResponse({}));
      }
    );

    const user = await restoreSession();
    expect(user?.username).toBe("hardening_user");
    expect(getAccessToken()).toBe("fresh-access");

    // A second restore round-trips through the same flow and still works.
    fetchSpy.mockClear();
    resetRefreshState();
    setAccessToken(null);
    const user2 = await restoreSession();
    expect(user2?.username).toBe("hardening_user");
    expect(fetchSpy.mock.calls.some(([url]) =>
      String(url).includes("/api/auth/refresh")
    )).toBe(true);
  });
});
