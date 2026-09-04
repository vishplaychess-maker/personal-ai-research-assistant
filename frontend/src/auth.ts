/**
 * Phase 6C / 7B / 7C — Authentication utilities.
 *
 * Phase 7B: access token in memory (via AuthContext), refresh token in localStorage.
 * Phase 7C: the refresh token lives ONLY in an HttpOnly cookie set by the backend
 * (`research_assistant_refresh_token`). It is never stored in localStorage and
 * never sent in headers or bodies. State-changing requests carry a double-submit
 * CSRF token (`X-CSRF-Token`) echoed from the non-HttpOnly CSRF cookie.
 */

import type { UserInfo, LoginRequest, RegisterRequest, LoginResponse, RefreshResponse, AuthSession } from "./types";
import { apiUrl } from "./apiBase";

// ── Storage keys ──────────────────────────────────────────

const USER_KEY = "research_assistant_user";
const CSRF_COOKIE_NAME = "research_assistant_csrf_token";
/** Legacy Phase 7B key removed during migration (one-time cleanup). */
const LEGACY_REFRESH_TOKEN_KEY = "research_assistant_refresh_token";

// ── In-memory access token ────────────────────────────────

/** The current access token is kept in memory, not localStorage.
 *  AuthContext manages this via setAccessToken(). */
let _accessToken: string | null = null;
/** In-flight refresh promise so concurrent callers share one request (serialized). */
let _refreshPromise: Promise<boolean> | null = null;
/** Callback invoked when the refresh token becomes invalid (→ logout). */
let _onInvalidRefresh: (() => void) | null = null;

export function setOnInvalidRefresh(cb: () => void): void {
  _onInvalidRefresh = cb;
}

/** Set the current access token (called by AuthContext after login/refresh). */
export function setAccessToken(token: string | null): void {
  _accessToken = token;
}

/** Get the current access token from memory. */
export function getAccessToken(): string | null {
  return _accessToken;
}

// ── CSRF token (Phase 7C) ─────────────────────────────────

/**
 * Read the CSRF token from the non-HttpOnly cookie set by the backend.
 * Returns null if the cookie is not present.
 */
export function getCsrfToken(): string | null {
  try {
    const match = document.cookie
      .split(";")
      .map((c) => c.trim())
      .find((c) => c.startsWith(`${CSRF_COOKIE_NAME}=`));
    if (!match) return null;
    return decodeURIComponent(match.slice(CSRF_COOKIE_NAME.length + 1));
  } catch {
    return null;
  }
}

/** Headers carrying the CSRF token (empty object if no token available). */
export function getCsrfHeaders(): Record<string, string> {
  const token = getCsrfToken();
  return token ? { "X-CSRF-Token": token } : {};
}

// ── Legacy migration (Phase 7C) ───────────────────────────

/**
 * Remove the Phase 7B localStorage refresh token during frontend startup.
 * After migration the refresh token only exists as an HttpOnly cookie; the
 * legacy value is discarded (never sent to the backend). Users without a
 * valid cookie must log in once after the migration.
 */
export function removeLegacyRefreshToken(): void {
  try {
    localStorage.removeItem(LEGACY_REFRESH_TOKEN_KEY);
  } catch {
    // localStorage may be unavailable
  }
}

// ── User storage ──────────────────────────────────────────

export function getStoredUser(): UserInfo | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function storeUser(user: UserInfo): void {
  try {
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  } catch {
    // ignore
  }
}

function clearUser(): void {
  try {
    localStorage.removeItem(USER_KEY);
  } catch {
    // ignore
  }
}

// ── Token expiry ──────────────────────────────────────────

export function isTokenExpired(token: string): boolean {
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.exp * 1000 < Date.now();
  } catch {
    return true;
  }
}

// ── Auth header helper ────────────────────────────────────

export function getAuthHeaders(): Record<string, string> {
  const token = _accessToken;
  if (!token || isTokenExpired(token)) {
    return {};
  }
  return { Authorization: `Bearer ${token}` };
}

/**
 * Async variant of getAuthHeaders — attempts a refresh when the current
 * access token is missing or expired. Used by raw-fetch callers
 * (streaming, model selector, system prompt editor, sidebar search) that
 * are outside api.ts's request() wrapper.
 */
export async function getAuthHeadersAsync(): Promise<Record<string, string>> {
  const token = getAccessToken();
  if (token && !isTokenExpired(token)) {
    return { Authorization: `Bearer ${token}` };
  }
  try {
    const ok = await refreshAccessToken();
    if (ok) {
      const fresh = getAccessToken();
      if (fresh && !isTokenExpired(fresh)) {
        return { Authorization: `Bearer ${fresh}` };
      }
    }
  } catch {
    // fall through to no auth headers
  }
  return {};
}

// ── Token refresh (Phase 7B / 7C) ─────────────────────────

let lastRefreshAttempt = 0;
const REFRESH_COOLDOWN_MS = 5000; // Prevent rapid retry loops

/**
 * Attempt to refresh the access token using the HttpOnly refresh cookie.
 * Returns true if the refresh succeeded, false otherwise.
 *
 * Phase 7C: the refresh token is read automatically from the cookie by the
 * browser (credentials: "include"); no token is sent in the body. The
 * CSRF token is echoed from the CSRF cookie (double-submit).
 *
 * Phase 7B hardening: concurrent refresh requests are SERIALIZED — if a
 * refresh is already in-flight, callers await the same shared promise
 * instead of issuing a second request or racing to clear auth state.
 */
export async function refreshAccessToken(): Promise<boolean> {
  // Serialize concurrent refresh requests: share the in-flight request
  if (_refreshPromise) return _refreshPromise;

  // Prevent rapid retry loops
  const now = Date.now();
  if (now - lastRefreshAttempt < REFRESH_COOLDOWN_MS) return false;
  lastRefreshAttempt = now;

  _refreshPromise = (async () => {
    try {
      const res = await fetch(apiUrl("/api/auth/refresh"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getCsrfHeaders() },
        credentials: "include",
        body: "{}",
      });

      if (!res.ok) {
        // Refresh failed — clear everything
        clearUser();
        _accessToken = null;
        _onInvalidRefresh?.();
        return false;
      }

      const data: RefreshResponse = await res.json();
      if (!data.access_token) {
        clearUser();
        _accessToken = null;
        _onInvalidRefresh?.();
        return false;
      }
      _accessToken = data.access_token;
      return true;
    } catch {
      clearUser();
      _accessToken = null;
      _onInvalidRefresh?.();
      return false;
    }
  })();

  try {
    return await _refreshPromise;
  } finally {
    _refreshPromise = null;
  }
}

// ── Auth API calls ────────────────────────────────────────

async function authRequest<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(apiUrl(url), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function loginUser(
  credentials: LoginRequest
): Promise<{ token: string; user: UserInfo }> {
  const data = await authRequest<LoginResponse>("/api/auth/login", credentials);
  // Phase 7C: the refresh token is delivered as an HttpOnly cookie; the
  // response contains only the access token.
  _accessToken = data.access_token;
  // Fetch user info with the new access token
  const res = await fetch(apiUrl("/api/auth/me"), {
    headers: { Authorization: `Bearer ${data.access_token}` },
    credentials: "include",
  });
  if (!res.ok) throw new Error("Failed to load user profile");
  const user: UserInfo = await res.json();
  storeUser(user);
  return { token: data.access_token, user };
}

export async function registerUser(
  credentials: RegisterRequest
): Promise<UserInfo> {
  return await authRequest<UserInfo>("/api/auth/register", credentials);
}

export async function logout(): Promise<void> {
  // Attempt to revoke the refresh session server-side. The refresh token is
  // read from the HttpOnly cookie; the CSRF token is echoed in the header.
  const accessToken = _accessToken;
  try {
    if (accessToken) {
      await fetch(apiUrl("/api/auth/logout"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${accessToken}`,
          ...getCsrfHeaders(),
        },
        credentials: "include",
        body: "{}",
      });
    }
  } catch {
    // Best-effort; clear client state regardless
  }
  clearUser();
  _accessToken = null;
}

// ── Restore session from HttpOnly refresh cookie ──────────

export async function restoreSession(): Promise<UserInfo | null> {
  // Try to get a fresh access token from the HttpOnly refresh cookie
  const refreshed = await refreshAccessToken();
  if (!refreshed || !_accessToken) {
    return null;
  }
  // Fetch user info
  try {
    const res = await fetch(apiUrl("/api/auth/me"), {
      headers: { Authorization: `Bearer ${_accessToken}` },
      credentials: "include",
    });
    if (!res.ok) {
      clearUser();
      _accessToken = null;
      return null;
    }
    const user: UserInfo = await res.json();
    storeUser(user);
    return user;
  } catch {
    clearUser();
    _accessToken = null;
    return null;
  }
}

// ── Session list (Phase 7B / 7C) ──────────────────────────

/**
 * Fetch the current user's refresh sessions from GET /api/auth/sessions.
 *
 * Phase 7C: no X-Refresh-Token header is sent. The backend identifies the
 * current session from the HttpOnly refresh cookie. The sessions endpoint
 * requires a valid access token, so we refresh first if needed.
 */
export async function listAuthSessions(): Promise<AuthSession[]> {
  const token = getAccessToken();
  if (!token || isTokenExpired(token)) {
    await refreshAccessToken();
  }
  const headers: Record<string, string> = getAuthHeaders();

  const res = await fetch(apiUrl("/api/auth/sessions"), {
    headers,
    credentials: "include",
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  const data = await res.json();
  return data.sessions ?? [];
}

// ── Get stored user without network (for initial UI render) ─

export function getStoredUserSync(): UserInfo | null {
  return getStoredUser();
}

// ── Test helper: reset module-level refresh state ─────────

export function resetRefreshState(): void {
  _refreshPromise = null;
  lastRefreshAttempt = 0;
}
