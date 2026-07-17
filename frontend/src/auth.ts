/**
 * Phase 6C — Authentication utilities.
 *
 * Provides token storage in localStorage, automatic Authorization header
 * management, expired-token detection, and logout functionality.
 */

import type { TokenResponse, UserInfo, LoginRequest, RegisterRequest } from "./types";

// ── Storage keys ──────────────────────────────────────────

const TOKEN_KEY = "research_assistant_token";
const USER_KEY = "research_assistant_user";

// ── Token storage ─────────────────────────────────────────

export function getStoredToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function getStoredUser(): UserInfo | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function storeAuth(token: string, user: UserInfo): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  } catch {
    // localStorage may be unavailable (private browsing, etc.)
  }
}

export function clearAuth(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  } catch {
    // ignore
  }
}

export function isTokenExpired(token: string): boolean {
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    // exp is in seconds since epoch
    return payload.exp * 1000 < Date.now();
  } catch {
    return true; // treat unparseable tokens as expired
  }
}

// ── Auth header helper ────────────────────────────────────

export function getAuthHeaders(): Record<string, string> {
  const token = getStoredToken();
  if (!token || isTokenExpired(token)) {
    clearAuth();
    return {};
  }
  return { Authorization: `Bearer ${token}` };
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
  const tokenData = await authRequest<TokenResponse>("/api/auth/login", credentials);
  // Fetch user info with the new token
  const res = await fetch("/api/auth/me", {
    headers: { Authorization: `Bearer ${tokenData.access_token}` },
  });
  if (!res.ok) throw new Error("Failed to load user profile");
  const user: UserInfo = await res.json();
  storeAuth(tokenData.access_token, user);
  return { token: tokenData.access_token, user };
}

export async function registerUser(
  credentials: RegisterRequest
): Promise<UserInfo> {
  return await authRequest<UserInfo>("/api/auth/register", credentials);
}

export function logout(): void {
  clearAuth();
}

// ── Restore session from stored token ─────────────────────

export async function restoreSession(): Promise<UserInfo | null> {
  const token = getStoredToken();
  if (!token || isTokenExpired(token)) {
    clearAuth();
    return null;
  }
  try {
    const res = await fetch("/api/auth/me", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      clearAuth();
      return null;
    }
    const user: UserInfo = await res.json();
    storeAuth(token, user);
    return user;
  } catch {
    clearAuth();
    return null;
  }
}
