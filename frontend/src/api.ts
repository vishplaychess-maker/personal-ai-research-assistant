/**
 * Phase 5C — API helper class extracted from App.tsx.
 * Phase 6C — Auto-attaches Authorization header and handles 401 → logout.
 *
 * Centralizes all backend API calls used across the frontend.
 */
import type {
  HealthStatus,
  Session,
  Message,
  ChatResponse,
  Document,
  Memory,
  MemorySetting,
} from "./types";
import { getStoredToken, clearAuth, isTokenExpired } from "./auth";

// Callback invoked when a 401 is received (used to trigger logout)
let _onUnauthorized: (() => void) | null = null;

export function setOnUnauthorized(cb: () => void): void {
  _onUnauthorized = cb;
}

function getAuthToken(): string | null {
  const token = getStoredToken();
  if (token && isTokenExpired(token)) {
    clearAuth();
    _onUnauthorized?.();
    return null;
  }
  return token;
}

/**
 * Generic request helper with JSON and FormData support.
 * Automatically attaches Authorization header if a token is stored.
 */
async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const isFormData = options?.body instanceof FormData;
  const headers: Record<string, string> = {};
  if (!isFormData) {
    headers["Content-Type"] = "application/json";
  }
  // Phase 6C: auto-attach auth token
  const token = getAuthToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(url, { headers, ...options });
  // Phase 6C: handle 401 — token expired or invalid
  if (res.status === 401) {
    clearAuth();
    _onUnauthorized?.();
    const data = await res.json().catch(() => ({ detail: "Unauthorized" }));
    throw new Error(data.detail || "Unauthorized");
  }
  if (!res.ok) {
    if (res.status === 404) {
      const detail = await res.json().catch(() => ({ detail: "Not found" }));
      throw new Error(detail.detail || "Not found");
    }
    const detail = await res
      .json()
      .catch(() => ({ detail: "Unknown error" }));
    throw new Error(detail.detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// ── Public API ────────────────────────────────────────────

export const API = {
  // Health
  getHealth() {
    return request<HealthStatus>("/api/health");
  },

  // Sessions
  listSessions() {
    return request<Session[]>("/api/sessions");
  },
  createSession(title = "New Research Session") {
    return request<Session>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title }),
    });
  },
  getSession(id: number) {
    return request<Session>(`/api/sessions/${id}`);
  },
  updateSession(id: number, title: string) {
    return request<Session>(`/api/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
  },
  deleteSession(id: number) {
    return request<void>(`/api/sessions/${id}`, { method: "DELETE" });
  },

  // Messages
  listMessages(sessionId: number) {
    return request<Message[]>(`/api/sessions/${sessionId}/messages`);
  },
  sendMessage(sessionId: number, message: string) {
    return request<ChatResponse>(`/api/sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
  },

  // Documents
  listDocuments(sessionId: number) {
    return request<Document[]>(`/api/sessions/${sessionId}/documents`);
  },
  uploadDocument(sessionId: number, file: File) {
    const form = new FormData();
    form.append("file", file);
    return request<{ document: Document; message: string }>(
      `/api/sessions/${sessionId}/documents`,
      { method: "POST", body: form }
    );
  },
  deleteDocument(id: number) {
    return request<void>(`/api/documents/${id}`, { method: "DELETE" });
  },

  // Memories
  listMemories() {
    return request<Memory[]>("/api/memories");
  },
  createMemory(content: string, category: string) {
    return request<Memory>("/api/memories", {
      method: "POST",
      body: JSON.stringify({ content, category }),
    });
  },
  updateMemory(id: number, content: string, category: string) {
    return request<Memory>(`/api/memories/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ content, category }),
    });
  },
  deleteMemory(id: number) {
    return request<void>(`/api/memories/${id}`, { method: "DELETE" });
  },
  clearAllMemories() {
    return request<void>("/api/memories", {
      method: "DELETE",
      body: JSON.stringify({ confirm: true }),
    });
  },
  getMemorySetting() {
    return request<MemorySetting>("/api/settings/memory");
  },
  setMemorySetting(enabled: boolean) {
    return request<MemorySetting>("/api/settings/memory", {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    });
  },
};
