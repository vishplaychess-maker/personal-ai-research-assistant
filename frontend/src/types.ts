/**
 * Phase 5C — Shared TypeScript interfaces extracted from App.tsx.
 *
 * Centralizes all data types used across the frontend components.
 */

// ── API Responses ─────────────────────────────────────────

export interface HealthStatus {
  backend: string;
  chromadb: string;
  ollama: string;
}

export interface Session {
  id: number;
  title: string;
  user_id: number;
  model: string | null;
  system_prompt: string | null;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: number;
  session_id: number;
  role: string;
  content: string;
  image_url?: string | null;  // Base64-encoded image data URL for multimodal messages
  citations?: string | null;
  created_at: string;
}

export interface Citation {
  marker: string;
  document_id: number;
  filename: string;
  page_number: number | null;
  chunk_id: number;
  snippet: string;
}

export interface ChatResponse {
  user_message: Message;
  assistant_message: Message;
  citations: Citation[];
  sources_used: boolean;
  memories_used: boolean;
}

export interface Document {
  id: number;
  session_id: number;
  filename: string;
  content_type: string | null;
  file_size: number | null;
  status: string;
  chunk_count: number;
  error_message: string | null;
  created_at: string;
}

export interface Memory {
  id: number;
  user_id: number;
  session_id: number | null;
  content: string;
  category: string;
  created_at: string;
  last_used_at: string;
}

export interface MemorySetting {
  enabled: boolean;
}

export interface UserSettings {
  llm_provider: string;
  api_key: string;
  model: string;
}

// ── Multiple Providers Manager ───────────────────────────

export interface ProviderConfig {
  id: number;
  provider_name: string;
  api_key: string;
  default_model: string;
  is_active: boolean;
  created_at: string;
}

export interface ProviderModelGroup {
  provider: string;
  provider_label: string;
  provider_id: number;
  models: ModelInfo[];
}

// ── Model Types ───────────────────────────────────────────

export interface ModelInfo {
  name: string;
  size: string | null;
  modified_at: string | null;
  is_free?: boolean | null;
}

export interface ModelListResponse {
  models: ModelInfo[];
  error: string | null;
}

export interface SessionModelResponse {
  id: number;
  model: string | null;
}

// ── Streaming Types ───────────────────────────────────────

export interface StreamResult {
  messageId: number;
  content: string;
  citations: Array<Record<string, unknown>>;
  sourcesUsed: boolean;
  memoriesUsed: boolean;
}

export interface StreamError {
  code: string;
  detail: string;
}

export interface StreamCallbacks {
  onStart?: () => void;
  onToken?: (token: string) => void;
  onPlan?: (steps: Array<Record<string, unknown>>) => void;
  onComplete?: (result: StreamResult) => void;
  onError?: (error: StreamError) => void;
  onCancelled?: () => void;
}

// ── Search Types ──────────────────────────────────────────

export interface SearchResult {
  session_id: number;
  session_title: string;
  message_id: number;
  role: string;
  content: string;
  snippet: string;
  created_at: string;
}

// ── Auth Types (Phase 6C / 7B) ────────────────────────────

/** Phase 7C: login response — the refresh token is delivered via HttpOnly cookie. */
export interface LoginResponse {
  access_token: string;
  token_type: string;
}

/** Phase 7C: refresh endpoint response — new refresh token is set as a cookie. */
export interface RefreshResponse {
  access_token: string;
  token_type: string;
}

export interface UserInfo {
  id: number;
  username: string;
  email: string | null;
  created_at: string;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface RegisterRequest {
  username: string;
  email: string;
  password: string;
}

export interface AuthState {
  user: UserInfo | null;
  token: string | null;
  isLoading: boolean;
  isAuthenticated: boolean;
}

/** Phase 7B hardening: safe metadata for a refresh session (GET /api/auth/sessions). */
export interface AuthSession {
  id: number;
  created_at: string;
  last_used_at: string;
  expires_at: string;
  revoked_at: string | null;
  device_info: string | null;
  is_current: boolean;
}

// ── Utility Types ─────────────────────────────────────────

export interface RetryTarget {
  message: string;
  errorDetail: string;
  image_url?: string;
}

// ── Scheduler Types ────────────────────────────────────────

export interface ScheduledTask {
  id: number;
  user_id: number;
  session_id: number;
  prompt: string;
  cron_expression: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  last_run_at: string | null;
  next_run_at: string | null;
}

export interface ScheduledTaskWithSession extends ScheduledTask {
  session_title: string;
}

export interface ScheduledTaskCreate {
  session_id: number;
  prompt: string;
  cron_expression: string;
}

export interface ScheduledTaskUpdate {
  prompt?: string;
  cron_expression?: string;
  is_active?: boolean;
}

// ── Shareable Agents (F5) ──────────────────────────────────

export interface SessionExport {
  thunder_ai_export: {
    version: string;
    exported_at: string;
    session: {
      title: string;
      model: string | null;
      system_prompt: string | null;
    };
    schedule: {
      cron_expression: string | null;
      prompt: string | null;
      is_active: boolean;
    };
    memory: {
      enabled: boolean;
    };
  };
}

export interface ImportResult {
  session_id: number;
  title: string;
  schedule_created: boolean;
}

// ── Shareable Agent Card (F6) ──────────────────────────────

export interface ShareCreateResult {
  share_id: string;
  share_url: string;
}

export interface PublicSharedAgent {
  share_id: string;
  title: string;
  model: string | null;
  system_prompt: string | null;
  preview_message: string | null;
  tool_count: number;
  has_schedule: boolean;
  cover_image_url: string | null;
  views: number;
}
