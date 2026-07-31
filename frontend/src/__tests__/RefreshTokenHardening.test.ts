/**
 * Phase 7B Hardening — Refresh token hardening tests.
 *
 * Covers:
 * - Session-list request sends X-Refresh-Token
 * - Other API requests never send X-Refresh-Token
 * - Concurrent refresh requests are serialized
 * - Refresh cooldown blocks rapid repeated calls
 * - Expired refresh token clears local authentication state
 * - Logout request remains correct
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

const REFRESH_TOKEN_KEY = "research_assistant_refresh_token";
const USER_KEY = "research_assistant_user";

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

describe("Phase 7B hardening — X-Refresh-Token session listing", () => {
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

  it("session-list request sends X-Refresh-Token header", async () => {
    const validJwt = createJWT({
      sub: "1",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    setAccessToken(validJwt);
    localStorage.setItem(REFRESH_TOKEN_KEY, "refresh-token-abc");

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
    expect(url).toBe("/api/auth/sessions");
    const headers = init?.headers as Record<string, string>;
    expect(headers["X-Refresh-Token"]).toBe("refresh-token-abc");
    expect(headers["Authorization"]).toBe(`Bearer ${validJwt}`);
  });

  it("session-list works without access token but still sends X-Refresh-Token", async () => {
    localStorage.setItem(REFRESH_TOKEN_KEY, "refresh-token-def");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: string | URL | Request) => {
        const u = url.toString();
        if (u.includes("/api/auth/refresh")) {
          return Promise.resolve(
            jsonResponse({
              access_token: "fresh-access",
              refresh_token: "fresh-refresh",
              token_type: "bearer",
            })
          );
        }
        return Promise.resolve(jsonResponse({ sessions: [], total: 0, active_count: 0 }));
      }
    );
    const sessions = await listAuthSessions();
    expect(sessions).toEqual([]);
    // Find the sessions request (last call) — it carries X-Refresh-Token.
    const sessionsCall = fetchSpy.mock.calls.find(([url]) =>
      String(url).includes("/api/auth/sessions")
    );
    expect(sessionsCall).toBeDefined();
    const headers = sessionsCall![1]?.headers as Record<string, string>;
    expect(headers["X-Refresh-Token"]).toBe("refresh-token-def");
  });

  it("session-list never sends the refresh token via query params", async () => {
    localStorage.setItem(REFRESH_TOKEN_KEY, "refresh-token-ghi");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: string | URL | Request) => {
        const u = url.toString();
        if (u.includes("/api/auth/refresh")) {
          return Promise.resolve(
            jsonResponse({
              access_token: "fresh-access",
              refresh_token: "fresh-refresh",
              token_type: "bearer",
            })
          );
        }
        return Promise.resolve(jsonResponse({ sessions: [], total: 0, active_count: 0 }));
      }
    );
    await listAuthSessions();
    const sessionsCall = fetchSpy.mock.calls.find(([url]) =>
      String(url).includes("/api/auth/sessions")
    );
    expect(sessionsCall).toBeDefined();
    const url = String(sessionsCall![0]);
    expect(url).not.toContain("refresh_token");
    expect(url).not.toContain("?");
  });

  it("other API requests never send X-Refresh-Token", async () => {
    // A valid, non-expired access token plus a stored refresh token.
    const validJwt = createJWT({
      sub: "1",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    setAccessToken(validJwt);
    localStorage.setItem(REFRESH_TOKEN_KEY, "refresh-token-should-not-leak");

    // Exercise BOTH a GET (health) and a POST (create session) API request.
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

    // Verify NO call carries the X-Refresh-Token header.
    expect(fetchSpy.mock.calls.length).toBeGreaterThanOrEqual(2);
    for (const [url, init] of fetchSpy.mock.calls) {
      const headers = (init?.headers as Record<string, string>) ?? {};
      expect(headers["X-Refresh-Token"]).toBeUndefined();
      expect(headers["X-Refresh-Token"]).not.toBe("refresh-token-should-not-leak");
      // The POST createSession call must carry Bearer auth like the GET.
      expect(headers["Authorization"]).toBe(`Bearer ${validJwt}`);
      // And never send the refresh token as a query param either.
      expect(String(url)).not.toContain("refresh_token");
    }
  });
});

describe("Phase 7B hardening — refresh serialization & cooldown", () => {
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
    localStorage.setItem(REFRESH_TOKEN_KEY, "valid-refresh");

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        access_token: "new-access",
        refresh_token: "new-refresh",
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
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBe("new-refresh");
    // Exactly one network refresh should have been issued.
    const refreshCalls = fetchSpy.mock.calls.filter(([url]) =>
      String(url).includes("/api/auth/refresh")
    );
    expect(refreshCalls).toHaveLength(1);
  });

  it("refresh cooldown blocks rapid repeated calls", async () => {
    localStorage.setItem(REFRESH_TOKEN_KEY, "valid-refresh");

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        access_token: "new-access",
        refresh_token: "new-refresh",
        token_type: "bearer",
      })
    );

    // First refresh succeeds.
    expect(await refreshAccessToken()).toBe(true);

    // Immediately repeat — cooldown (5s) blocks it without a network call.
    expect(await refreshAccessToken()).toBe(false);
    const refreshCalls = fetchSpy.mock.calls.filter(([url]) =>
      String(url).includes("/api/auth/refresh")
    );
    expect(refreshCalls).toHaveLength(1);
  });

  it("expired refresh token clears local authentication state", async () => {
    const onInvalid = vi.fn();
    setOnInvalidRefresh(onInvalid);
    localStorage.setItem(REFRESH_TOKEN_KEY, "expired-refresh");
    localStorage.setItem(USER_KEY, JSON.stringify(testUser));
    setAccessToken("stale-access-token");

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "Invalid refresh token" }, 401)
    );

    const result = await refreshAccessToken();
    expect(result).toBe(false);
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
    expect(localStorage.getItem(USER_KEY)).toBeNull();
    expect(getStoredUser()).toBeNull();
    expect(onInvalid).toHaveBeenCalledTimes(1);
  });

  it("logout request remains correct (POST, bearer auth, refresh body)", async () => {
    setAccessToken("access-token-123");
    localStorage.setItem(REFRESH_TOKEN_KEY, "refresh-token-456");

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "Logged out successfully" })
    );

    await logout();

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/auth/logout");
    expect(init?.method).toBe("POST");
    const headers = init?.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer access-token-123");
    expect(JSON.parse(String(init?.body))).toEqual({
      refresh_token: "refresh-token-456",
    });
  });

  it("logout clears state even when no access token is available", async () => {
    localStorage.setItem(REFRESH_TOKEN_KEY, "refresh-token-789");
    localStorage.setItem(USER_KEY, JSON.stringify(testUser));
    setAccessToken(null);

    // No access token → logout skips the server call but still clears state.
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    await logout();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
    expect(localStorage.getItem(USER_KEY)).toBeNull();
  });

  it("page restore still works after refresh-token rotation", async () => {
    // Simulate a stored refresh token that was rotated on a previous refresh.
    localStorage.setItem(REFRESH_TOKEN_KEY, "rotated-refresh-token");

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: string | URL | Request) => {
        const u = url.toString();
        if (u.includes("/api/auth/refresh")) {
          return Promise.resolve(
            jsonResponse({
              access_token: "fresh-access",
              refresh_token: "newer-refresh-token",
              token_type: "bearer",
            })
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
    // The rotated refresh token replaced the old one in storage.
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBe("newer-refresh-token");
    expect(getAccessToken()).toBe("fresh-access");

    // A second restore round-trips through the same flow and still works.
    // Simulate a fresh page load: module refresh state (incl. cooldown) resets,
    // but the ROTATED refresh token persists in localStorage.
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
