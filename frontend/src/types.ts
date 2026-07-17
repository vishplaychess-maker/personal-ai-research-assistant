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

// ── Utility Types ─────────────────────────────────────────

export interface RetryTarget {
  message: string;
  errorDetail: string;
}
