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
  OAuthStartResponse,
  OAuthStatusResponse,
  OAuthAccountResponse,
  OAuthLogoutResponse,
  DesktopStatus,
  DesktopTreeResponse,
  DesktopReadResponse,
  DesktopWriteResponse,
  DesktopRunResult,
  DesktopScreenshotResult,
  AgentDirective,
  UserSkill,
  UserSkillDetail,
  ScheduledTask,
  ScheduledTaskCreate,
  ScheduledTaskUpdate,
  SessionExport,
  ImportResult,
  ShareCreateResult,
  MediaProvidersResponse,
  MediaGenerateResponse,
  VideoPollResponse,
  MediaProviderInfo,
  ApprovalActionPreview,
  ApprovalDetail,
  ApprovalDecision,
  RunCancelResult,
  BuiltinSkillCatalog,
  Capability,
  OfficialMcpServer,
  OpenbotTaskStatus,
} from "./types";
import { getAccessToken, isTokenExpired, refreshAccessToken, getCsrfToken } from "./auth";
import { apiUrl } from "./apiBase";

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
  let res = await fetch(apiUrl(url), { headers, credentials: "include", ...options });
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
        res = await fetch(apiUrl(url), { headers, credentials: "include", ...options });
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
  exportSession(id: number) {
    return request<SessionExport>(`/api/sessions/${id}/export`);
  },
  importSession(payload: SessionExport) {
    return request<ImportResult>("/api/sessions/import", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  createShare(sessionId: number) {
    return request<ShareCreateResult>(`/api/sessions/${sessionId}/share`, {
      method: "POST",
    });
  },

  // Messages
  listMessages(sessionId: number) {
    return request<Message[]>(`/api/sessions/${sessionId}/messages`);
  },

  // Phase 1B.2R MP-3C3C — durable run + MCP approval (bearer + CSRF via request()).
  // The approval detail is the ONLY sanctioned source of tool info; the browser
  // never reconstructs it from marker text and never executes MCP.
  getApproval(approvalId: string) {
    return request<ApprovalDetail>(`/api/approvals/${encodeURIComponent(approvalId)}`);
  },
  // MP-4C — the EXACT python_sandbox code for a requested approval (owner only).
  getApprovalAction(approvalId: string) {
    return request<ApprovalActionPreview>(
      `/api/approvals/${encodeURIComponent(approvalId)}/action`,
    );
  },
  approveRun(approvalId: string) {
    return request<ApprovalDecision>(
      `/api/approvals/${encodeURIComponent(approvalId)}/approve`,
      { method: "POST", body: "{}" },
    );
  },
  denyRun(approvalId: string) {
    return request<ApprovalDecision>(
      `/api/approvals/${encodeURIComponent(approvalId)}/deny`,
      { method: "POST", body: "{}" },
    );
  },
  cancelRun(runId: string) {
    return request<RunCancelResult>(
      `/api/runs/${encodeURIComponent(runId)}/cancel`,
      { method: "POST", body: "{}" },
    );
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
  // Local Vision (ZERO API cost) — describe an image with a LOCAL Ollama model.
  // Returns the bounded VisionResult: { status, description, notice, context }.
  describeImage(file: File) {
    const form = new FormData();
    form.append("file", file);
    return request<{
      status: string;
      description: string | null;
      notice: string;
      context: string | null;
    }>("/api/vision/describe", { method: "POST", body: form });
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

  // ── Subscription sign-in (Grok / xAI device code) ────────
  startGrokSignIn() {
    return request<OAuthStartResponse>("/api/oauth/grok/start", { method: "POST" });
  },
  grokSignInStatus() {
    return request<OAuthStatusResponse>("/api/oauth/grok/status");
  },
  refreshGrokTokens() {
    return request<{ provider: string; status: string; access_expires_at: string | null; message: string }>(
      "/api/oauth/grok/refresh",
      { method: "POST" }
    );
  },
  signOutGrok() {
    return request<OAuthLogoutResponse>("/api/oauth/grok", { method: "DELETE" });
  },
  grokAccount() {
    return request<OAuthAccountResponse>("/api/oauth/grok/account");
  },

  // ── Desktop automation (workspace-sandboxed, opt-in) ─────
  desktopStatus() {
    return request<DesktopStatus>("/api/desktop/status");
  },
  desktopTree() {
    return request<DesktopTreeResponse>("/api/desktop/tree");
  },
  desktopRead(path: string) {
    return request<DesktopReadResponse>("/api/desktop/read", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
  },
  desktopWrite(path: string, content: string) {
    return request<DesktopWriteResponse>("/api/desktop/write", {
      method: "POST",
      body: JSON.stringify({ path, content }),
    });
  },
  desktopRun(command: string, args: string[], timeout_s?: number) {
    return request<DesktopRunResult>("/api/desktop/run", {
      method: "POST",
      body: JSON.stringify({ command, args, timeout_s }),
    });
  },
  desktopScreenshot() {
    return request<DesktopScreenshotResult>("/api/desktop/screenshot", {
      method: "POST",
    });
  },

  // ── Agent Directives (F6 Cap 3 "Lessons Learned") ────────
  listDirectives() {
    return request<AgentDirective[]>("/api/directives");
  },
  toggleDirective(id: number, isActive: boolean) {
    return request<AgentDirective>(`/api/directives/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ is_active: isActive }),
    });
  },
  deleteDirective(id: number) {
    return request<void>(`/api/directives/${id}`, { method: "DELETE" });
  },

  // ── User-Defined Skills (custom skill creator) ───────────
  listSkills() {
    return request<UserSkill[]>("/api/skills");
  },

  // Built-in (filesystem) skill catalog — read-only
  listBuiltinSkills() {
    return request<BuiltinSkillCatalog>("/api/skills/catalog");
  },
  getSkill(id: number) {
    return request<UserSkillDetail>(`/api/skills/${id}`);
  },
  createSkill(data: {
    name: string;
    description: string;
    body: string;
    trigger_keywords?: string;
  }) {
    return request<UserSkillDetail>("/api/skills", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  updateSkill(
    id: number,
    data: Partial<{
      name: string;
      description: string;
      body: string;
      trigger_keywords: string;
    }>
  ) {
    return request<UserSkillDetail>(`/api/skills/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },
  deleteSkill(id: number) {
    return request<void>(`/api/skills/${id}`, { method: "DELETE" });
  },
  toggleSkill(id: number) {
    return request<{ id: number; enabled: boolean }>(
      `/api/skills/${id}/toggle`,
      { method: "POST" }
    );
  },

  // ── Executable Capabilities ──────────────────────────────
  getCapabilities() {
    return request<Capability[]>("/api/capabilities");
  },
  updateCapability(
    id: string,
    data: { enabled: boolean; config?: Record<string, unknown> }
  ) {
    return request<Capability>(`/api/capabilities/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
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

  // ── MCP Servers ────────────────────────────────────────────
  listMcpServers() {
    return request<any[]>("/api/mcp/servers");
  },
  createMcpServer(data: { name: string; command: string; args: string[]; env?: any; enabled?: boolean; tool_allowlist?: string[] | null }) {
    return request<any>("/api/mcp/servers", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  updateMcpServer(id: number, data: any) {
    return request<any>(`/api/mcp/servers/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },
  deleteMcpServer(id: number) {
    return request<void>(`/api/mcp/servers/${id}`, { method: "DELETE" });
  },
  discoverMcpServer(id: number) {
    return request<{ tools: any[]; error: string | null }>(`/api/mcp/servers/${id}/discover`, {
      method: "POST",
    });
  },

  // ── OpenBot Autonomous Research Agent (Phase 1 task queue) ──
  // Non-blocking: POST returns the task id immediately; poll GET until the
  // status is "completed" / "failed".
  startOpenbotResearch(query: string) {
    return request<{ task_id: string }>("/api/agents/openbot/research", {
      method: "POST",
      body: JSON.stringify({ query }),
    });
  },
  getOpenbotTask(taskId: string) {
    return request<OpenbotTaskStatus>(
      `/api/agents/openbot/tasks/${encodeURIComponent(taskId)}`
    );
  },

  // ── Official MCP registry ───────────────────────────────────
  listOfficialMcpServers() {
    return request<OfficialMcpServer[]>("/api/mcp/registry");
  },
  updateOfficialMcpServer(
    id: string,
    data: { enabled: boolean; config?: Record<string, string> }
  ) {
    return request<OfficialMcpServer>(
      `/api/mcp/registry/${encodeURIComponent(id)}/config`,
      { method: "PATCH", body: JSON.stringify(data) }
    );
  },

  // ── Knowledge Graph ───────────────────────────────────────
  getKnowledgeGraph() {
    return request<KnowledgeGraphData>("/api/knowledge-graph");
  },

  // ── Media Generation (Image/Video) ───────────────────────
  listMediaProviders() {
    return request<MediaProvidersResponse>("/api/media/providers");
  },
  generateImage(data: {
    prompt: string;
    provider?: string;
    model?: string;
    width?: number;
    height?: number;
  }) {
    return request<MediaGenerateResponse>("/api/media/generate-image", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  generateVideo(data: {
    prompt: string;
    provider?: string;
    model?: string;
    duration?: string;
  }) {
    return request<MediaGenerateResponse>("/api/media/generate-video", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  pollVideo(taskId: string, provider: string, wait = false) {
    return request<VideoPollResponse>(
      `/api/media/video/${encodeURIComponent(taskId)}?provider=${encodeURIComponent(provider)}&wait=${wait}`
    );
  },
};

export interface KnowledgeGraphData {
  nodes: Array<{ id: number; name: string; type: string }>;
  links: Array<{ source: number; target: number; relation: string; weight: number }>;
}
