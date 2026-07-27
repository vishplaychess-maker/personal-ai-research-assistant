/**
 * Phase 6C / 7B — Authentication utilities.
 *
 * Phase 7B: access token in memory (via AuthContext), refresh token in localStorage.
 * Auto-refresh on 401 attempts once; prevents infinite refresh loops.
 */

import type { UserInfo, LoginRequest, RegisterRequest, LoginResponse, RefreshResponse } from "./types";

// ── Storage keys ──────────────────────────────────────────

/** Key for the refresh token in localStorage. */
const REFRESH_TOKEN_KEY = "research_assistant_refresh_token";
const USER_KEY = "research_assistant_user";

// ── In-memory access token (Phase 7B) ─────────────────────

/** The current access token is kept in memory, not localStorage.
 *  AuthContext manages this via setAccessToken(). */
let _accessToken: string | null = null;
/** Flag to prevent infinite refresh loops. */
let _isRefreshing = false;
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

// ── Refresh token storage (localStorage) ──────────────────

function getStoredRefreshToken(): string | null {
  try {
    return localStorage.getItem(REFRESH_TOKEN_KEY);
  } catch {
    return null;
  }
}

function storeRefreshToken(token: string): void {
  try {
    localStorage.setItem(REFRESH_TOKEN_KEY, token);
  } catch {
    // localStorage may be unavailable
  }
}

function clearRefreshToken(): void {
  try {
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  } catch {
    // ignore
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

// ── Token refresh (Phase 7B) ──────────────────────────────

let lastRefreshAttempt = 0;
const REFRESH_COOLDOWN_MS = 5000; // Prevent rapid retry loops

/**
 * Attempt to refresh the access token using the stored refresh token.
 * Returns true if the refresh succeeded, false otherwise.
 */
export async function refreshAccessToken(): Promise<boolean> {
  if (_isRefreshing) return false;

  const refreshToken = getStoredRefreshToken();
  if (!refreshToken) return false;

  // Prevent rapid retry loops
  const now = Date.now();
  if (now - lastRefreshAttempt < REFRESH_COOLDOWN_MS) return false;
  lastRefreshAttempt = now;

  _isRefreshing = true;
  try {
    const res = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });

    if (!res.ok) {
      // Refresh failed — clear everything
      clearRefreshToken();
      clearUser();
      _accessToken = null;
      _onInvalidRefresh?.();
      return false;
    }

    const data: RefreshResponse = await res.json();
    _accessToken = data.access_token;
    storeRefreshToken(data.refresh_token);
    return true;
  } catch {
    clearRefreshToken();
    clearUser();
    _accessToken = null;
    _onInvalidRefresh?.();
    return false;
  } finally {
    _isRefreshing = false;
  }
}

// ── Auth API calls ────────────────────────────────────────

const API_BASE = "";

async function authRequest<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${url}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
  // Store the refresh token
  storeRefreshToken(data.refresh_token);
  _accessToken = data.access_token;
  // Fetch user info with the new access token
  const res = await fetch("/api/auth/me", {
    headers: { Authorization: `Bearer ${data.access_token}` },
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
  // Attempt to revoke the refresh token server-side
  const refreshToken = getStoredRefreshToken();
  const accessToken = _accessToken;
  try {
    if (refreshToken && accessToken) {
      await fetch("/api/auth/logout", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
    }
  } catch {
    // Best-effort; clear client state regardless
  }
  clearRefreshToken();
  clearUser();
  _accessToken = null;
}

// ── Restore session from stored refresh token ─────────────

export async function restoreSession(): Promise<UserInfo | null> {
  const refreshToken = getStoredRefreshToken();
  if (!refreshToken) {
    return null;
  }
  // Try to get a fresh access token from the stored refresh token
  const refreshed = await refreshAccessToken();
  if (!refreshed || !_accessToken) {
    return null;
  }
  // Fetch user info
  try {
    const res = await fetch("/api/auth/me", {
      headers: { Authorization: `Bearer ${_accessToken}` },
    });
    if (!res.ok) {
      clearRefreshToken();
      clearUser();
      _accessToken = null;
      return null;
    }
    const user: UserInfo = await res.json();
    storeUser(user);
    return user;
  } catch {
    clearRefreshToken();
    clearUser();
    _accessToken = null;
    return null;
  }
}

// ── Get stored user without network (for initial UI render) ─

export function getStoredUserSync(): UserInfo | null {
  return getStoredUser();
}

// ── Test helper: reset module-level refresh state ─────────

export function resetRefreshState(): void {
  _isRefreshing = false;
  lastRefreshAttempt = 0;
}
