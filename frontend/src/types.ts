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
  confidence?: number | null;  // F6 Cap 2: 0-100 advisory score (None = n/a)
  confidence_reason?: string | null;  // F6 Cap 2: one-line why
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
  /** MP-3C3D3B — advisory per-mode availability; optional so existing PUT
   *  payloads that omit it (unaffected by this phase) still type-check. */
  chat_modes?: ChatModeCapability[];
}

// ── Mode selector (Phase 1B.2R MP-3C3D3B) ─────────────────
//
// "auto" is a frontend-only choice: it means "omit `mode` from the request"
// and preserves whatever the server's existing omitted-mode behavior is
// (global-flag/heuristic routing on legacy, plain chat on durable). It is
// never sent on the wire and never appears in `BackendChatMode`.

/** Canonical wire values — must match `submission._ALLOWED_MODES` exactly. */
export type BackendChatMode = "chat" | "deep_research" | "multi_agent";

/** UI selection state: the three backend modes plus the frontend-only "auto". */
export type UIChatMode = "auto" | BackendChatMode;

/** Advisory capability info for one backend mode, from `GET /api/settings`. */
export interface ChatModeCapability {
  mode: BackendChatMode;
  available: boolean;
  reason?: string | null;
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

// ── Desktop automation (opt-in, workspace-sandboxed) ───────

export interface DesktopStatus {
  enabled: boolean;
  workspace: string;
  commands: string[];
  internal_commands: string[];
  blocked_commands: string[];
  timeout_s: number;
}

export interface DesktopTreeEntry {
  path: string;
  name: string;
  is_dir: boolean;
  size: number;
  depth: number;
}

export interface DesktopTreeResponse {
  root: string;
  entries: DesktopTreeEntry[];
}

export interface DesktopReadResponse {
  path: string;
  content: string;
}

export interface DesktopWriteResponse {
  path: string;
  ok: boolean;
  bytes_written: number;
}

export interface DesktopRunResult {
  command: string;
  returncode: number;
  stdout: string;
  stderr: string;
  truncated: boolean;
  duration_ms: number;
  timed_out: boolean;
}

export interface DesktopScreenshotResult {
  status: string;
  description: string | null;
  notice: string;
  image_bytes: number;
}

// ── Subscription sign-in (OAuth device code) ───────────────

export type OAuthLoginStatus =
  | "none"
  | "pending"
  | "connected"
  | "expired"
  | "error";

export interface OAuthStartResponse {
  provider: string;
  user_code: string;
  verification_uri: string;
  expires_in: number;
  poll_interval_s: number;
  status: OAuthLoginStatus;
}

export interface OAuthStatusResponse {
  provider: string;
  status: OAuthLoginStatus;
  user_code: string;
  verification_uri: string;
  account_name: string;
  account_email: string;
  scopes: string;
  access_expires_at: string | null;
  device_expires_at: string | null;
  poll_interval_s: number;
  next_poll_at: string | null;
  last_error: string;
  message: string;
}

export interface OAuthAccountResponse {
  provider: string;
  status: OAuthLoginStatus;
  account_name: string;
  account_email: string;
  scopes: string;
  models: string[];
  entitlement_known: boolean;
  message: string;
}

export interface OAuthLogoutResponse {
  provider: string;
  status: string;
  revoked: boolean;
  message: string;
}

// ── Agent Directives (F6 Cap 3 "Lessons Learned") ─────────

export interface AgentDirective {
  id: number;
  content: string;
  is_active: boolean;
  created_at: string;
}

// ── User-Defined Skills (custom skill creator) ────────────

// Built-in (filesystem SKILL.md) skills surfaced read-only in Settings > Skills
export interface BuiltinSkill {
  name: string;
  description: string;
  pinned?: boolean;
}

export interface BuiltinSkillCatalog {
  enabled: boolean;
  skills: BuiltinSkill[];
}

export interface UserSkill {
  id: number;
  name: string;
  description: string;
  trigger_keywords: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface UserSkillDetail extends UserSkill {
  body: string;
}

// Executable capability toggle (GET/PATCH /api/capabilities)
// A manifest field of a custom skill's inputs_schema.
export interface CapabilityInputField {
  type?: "string" | "text" | "number" | "boolean" | "password";
  label?: string;
  required?: boolean;
  placeholder?: string;
}

export interface Capability {
  id: string;
  name: string;
  description: string;
  icon: string;
  enabled: boolean;
  config: Record<string, unknown>;
  kind?: "capability" | "mcp_server" | "custom";
  required_config_keys?: string[];
  // Custom skills only — describes the static config fields (e.g. an API key)
  // the user can save to SkillPreference.config_json.
  inputs_schema?: Record<string, CapabilityInputField>;
}

// POST /api/skills/custom/{skill_id} — run a user-defined skill
export interface CustomSkillRunResult {
  skill_id: string;
  result: Record<string, unknown>;
}

// OpenBot Autonomous Research Agent (Phase 1 task queue)
export type OpenbotTaskStatusValue =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface OpenbotTaskStatus {
  task_id: string;
  status: OpenbotTaskStatusValue;
  result_text: string | null;
  created_at: string | null;
}

// Live orchestrator frame from GET /api/agents/openbot/tasks/{id}/events (SSE)
export interface AgentEvent {
  seq: number;
  type: "thinking" | "plan" | "tool" | "output" | "synthesis" | "warning" | "status";
  agent: string;
  message: string;
  tool: string | null;
  output: string | null;
  at: number;
}

// Built-in browser (V1) — backend header probe for iframe framability
export interface BrowserEmbeddability {
  embeddable: boolean;
  reason: string | null;
}

// Official MCP server from GET /api/mcp/registry (credentials never included)
export interface OfficialMcpServer {
  id: string;
  name: string;
  description: string;
  required_config_keys: string[];
  enabled: boolean;
  status: "connected" | "disconnected" | "not_configured";
  configured_keys: string[];
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

// ── Media Generation Types ─────────────────────────────────

export interface MediaProviderInfo {
  name: string;
  label: string;
  has_api_key: boolean;
  default_image_model: string;
  default_video_model: string;
  available_image_models: string[];
  available_video_models: string[];
}

export interface MediaProvidersResponse {
  providers: MediaProviderInfo[];
  primary: string;
  fallback_order: string[];
}

export interface MediaGenerateResponse {
  success: boolean;
  url?: string | null;
  task_id?: string | null;
  error?: string | null;
  provider?: string | null;
  model?: string | null;
}

export interface VideoPollResponse {
  success: boolean;
  url?: string | null;
  task_id?: string | null;
  error?: string | null;
  provider?: string | null;
  status: "processing" | "completed" | "failed";
}

// ── Phase 1B.2R MP-3C3C — durable run + MCP approval ──────

/** Approval detail from GET /api/approvals/{id}. The UI renders ONLY the safe
 *  subset (tool_id, state, risk_level, display_summary, purpose_redacted,
 *  created_at, expires_at). action_hash / session-family ids are never shown. */
export interface ApprovalDetail {
  id: string;
  state: "requested" | "approved" | "denied" | "expired" | "consumed" | "revoked" | string;
  tool_id: string;
  source_type?: string;
  risk_level?: string;
  run_id?: string | null;
  display_summary?: Record<string, unknown>;
  purpose_redacted?: string;
  created_at?: string | null;
  decided_at?: string | null;
  expires_at?: string | null;
  /** non-secret binding hash — used ONLY to match the exact-action preview;
   *  never rendered. */
  action_hash?: string;
}

/** MP-4C — the exact python_sandbox action from
 *  GET /api/approvals/{id}/action (owner + requested only, read-only). */
export interface ApprovalActionPreview {
  approval_id: string;
  tool_id: string;
  /** python_sandbox only — the exact code that would run. */
  code?: string;
  code_bytes?: number;
  /** MP-4C decision 2 — memory_write / directive_write only. The exact content
   *  that would be persisted, shown in full (the server refuses rather than
   *  truncating, so what is rendered is exactly what would be written). */
  kind?: "memory" | "directive";
  content?: string;
  content_chars?: number;
  args_hash: string;
  action_hash: string;
  purpose?: string;
  expires_at?: string | null;
}

export interface ApprovalDecision {
  id: string;
  state: string;
  idempotent?: boolean;
}

export interface RunCancelResult {
  run_id: string;
  status: string;
  cancellation_requested: boolean;
  already_terminal: boolean;
}
