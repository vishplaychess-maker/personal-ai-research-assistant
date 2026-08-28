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
  UserSettings,
  ModelListResponse,
  SessionModelResponse,
  ProviderConfig,
  ProviderModelGroup,
  ScheduledTask,
  ScheduledTaskCreate,
  ScheduledTaskUpdate,
} from "./types";
import { getAccessToken, isTokenExpired, refreshAccessToken, getCsrfToken } from "./auth";

// Callback invoked when a 401 is received (used to trigger logout)
let _onUnauthorized: (() => void) | null = null;

export function setOnUnauthorized(cb: () => void): void {
  _onUnauthorized = cb;
}

/**
 * Get the current auth token, attempting a refresh if expired.
 * Returns null if no valid token is available after refresh attempt.
 */
async function getValidToken(): Promise<string | null> {
  let token = getAccessToken();
  if (token && !isTokenExpired(token)) {
    return token;
  }
  // Token expired or missing: try to refresh
  const refreshed = await refreshAccessToken();
  if (refreshed) {
    return getAccessToken();
  }
  return null;
}

/**
 * Generic request helper with JSON and FormData support.
 * Automatically attaches Authorization header if a token is available.
 * Phase 7B: auto-refreshes expired access tokens once on 401.
 */
async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const isFormData = options?.body instanceof FormData;
  const headers: Record<string, string> = {};
  if (!isFormData) {
    headers["Content-Type"] = "application/json";
  }
  // Phase 6C / 7B: auto-attach auth token, refresh if expired
  const token = await getValidToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  // Phase 7C: attach the double-submit CSRF token to state-changing requests.
  // GET/HEAD/OPTIONS never need it.
  const method = (options?.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
    const csrf = getCsrfToken();
    if (csrf) {
      headers["X-CSRF-Token"] = csrf;
    }
  }
  let res = await fetch(url, { headers, credentials: "include", ...options });
  // Phase 7B: auto-refresh on 401 (try once)
  if (res.status === 401 && token) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      const newToken = getAccessToken();
      if (newToken) {
        headers["Authorization"] = `Bearer ${newToken}`;
        // Phase 7C: the refresh rotated the CSRF cookie, so re-read the CSRF
        // token before retrying a state-changing request — the old header
        // value would now mismatch the new cookie and trigger a 403.
        if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
          const csrf = getCsrfToken();
          if (csrf) {
            headers["X-CSRF-Token"] = csrf;
          }
        }
        res = await fetch(url, { headers, credentials: "include", ...options });
      }
    }
  }
  // Phase 6C: handle 401 — token expired or invalid
  if (res.status === 401) {
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
  listChatModels(provider?: string) {
    return request<ModelListResponse>(
      `/api/models${provider ? `?provider=${encodeURIComponent(provider)}` : ""}`
    );
  },
  updateSessionModel(id: number, model: string | null) {
    return request<SessionModelResponse>(`/api/sessions/${id}/model`, {
      method: "PATCH",
      body: JSON.stringify({ model }),
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
  getUserSettings() {
    return request<UserSettings>("/api/settings");
  },
  updateUserSettings(settings: UserSettings) {
    return request<UserSettings>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
  },

  // ── Multiple Providers Manager ──────────────
  listProviders() {
    return request<ProviderConfig[]>("/api/providers");
  },
  createProvider(data: {
    provider_name: string;
    api_key: string;
    default_model: string;
    is_active: boolean;
  }) {
    return request<ProviderConfig>("/api/providers", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  updateProvider(
    id: number,
    data: Partial<{
      provider_name: string;
      api_key: string;
      default_model: string;
      is_active: boolean;
    }>
  ) {
    return request<ProviderConfig>(`/api/providers/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },
  deleteProvider(id: number) {
    return request<void>(`/api/providers/${id}`, { method: "DELETE" });
  },
  listAllProviderModels() {
    return request<ProviderModelGroup[]>("/api/providers/models");
  },

  // ── Scheduler ────────────────────────────────────────────
  listScheduledTasks() {
    return request<ScheduledTask[]>("/api/scheduler");
  },
  getScheduledTask(id: number) {
    return request<ScheduledTask>(`/api/scheduler/${id}`);
  },
  createScheduledTask(data: ScheduledTaskCreate) {
    return request<ScheduledTask>("/api/scheduler", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  updateScheduledTask(id: number, data: ScheduledTaskUpdate) {
    return request<ScheduledTask>(`/api/scheduler/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },
  deleteScheduledTask(id: number) {
    return request<void>(`/api/scheduler/${id}`, { method: "DELETE" });
  },
  runScheduledTask(id: number) {
    return request<{ response: string }>(`/api/scheduler/${id}/run`, {
      method: "POST",
    });
  },
  getSchedulerHealth() {
    return request<{ running: boolean; jobs_count: number }>("/api/scheduler/health");
  },
};
