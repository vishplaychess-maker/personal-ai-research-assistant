/**
 * Phase 6C / 7B — Authentication context.
 *
 * Phase 7B: access token is stored in memory (via setAccessToken/getAccessToken),
 * refresh token is stored in localStorage. The provider restores the session
 * from the refresh token on mount.
 */

import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react";
import type { UserInfo, AuthState } from "./types";
import {
  loginUser,
  registerUser,
  logout as authLogout,
  restoreSession,
  getStoredUserSync,
  getAccessToken,
  setAccessToken,
  setOnInvalidRefresh,
} from "./auth";
import { setOnUnauthorized } from "./api";

// ── Context type ──────────────────────────────────────────

interface AuthContextType extends AuthState {
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, email: string, password: string) => Promise<void>;
  logout: () => void;
  authError: string | null;
  clearError: () => void;
}

const AuthContext = createContext<AuthContextType | null>(null);

// ── Provider ──────────────────────────────────────────────

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserInfo | null>(getStoredUserSync);
  const [token, setToken] = useState<string | null>(getAccessToken);
  const [isLoading, setIsLoading] = useState(true);
  const [authError, setAuthError] = useState<string | null>(null);

  const isAuthenticated = !!user && !!token;

  // Restore session on mount
  useEffect(() => {
    restoreSession()
      .then((u) => {
        if (u) {
          setUser(u);
          setToken(getAccessToken());
        } else {
          setUser(null);
          setToken(null);
        }
      })
      .catch(() => {
        setUser(null);
        setToken(null);
      })
      .finally(() => setIsLoading(false));
  }, []);

  // Register handlers for refresh failure and 401
  useEffect(() => {
    setOnUnauthorized(() => {
      setUser(null);
      setToken(null);
    });
    setOnInvalidRefresh(() => {
      setUser(null);
      setToken(null);
    });
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    setAuthError(null);
    try {
      const result = await loginUser({ username, password });
      setUser(result.user);
      setToken(result.token);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Login failed";
      setAuthError(msg);
      throw err;
    }
  }, []);

  const register = useCallback(async (username: string, email: string, password: string) => {
    setAuthError(null);
    try {
      await registerUser({ username, email, password });
      // After successful registration, log in automatically
      await login(username, password);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Registration failed";
      setAuthError(msg);
      throw err;
    }
  }, [login]);

  const logout = useCallback(async () => {
    await authLogout();
    setUser(null);
    setToken(null);
    setAuthError(null);
  }, []);

  const clearError = useCallback(() => setAuthError(null), []);

  return (
    <AuthContext.Provider
      value={{ user, token, isLoading, isAuthenticated, login, register, logout, authError, clearError }}
    >
      {children}
    </AuthContext.Provider>
  );
}

// ── Hook ──────────────────────────────────────────────────

export function useAuth(): AuthContextType {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
