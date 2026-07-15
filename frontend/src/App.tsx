import { useState, useEffect, useCallback, useRef } from "react";
import "./App.css";

// ── Types ─────────────────────────────────────────────────

interface HealthStatus {
  backend: string;
  chromadb: string;
  ollama: string;
}

interface Session {
  id: number;
  title: string;
  user_id: number;
  created_at: string;
  updated_at: string;
}

interface Message {
  id: number;
  session_id: number;
  role: string;
  content: string;
  citations?: string | null;
  created_at: string;
}

interface Citation {
  marker: string;
  document_id: number;
  filename: string;
  page_number: number | null;
  chunk_id: number;
  snippet: string;
}

interface ChatResponse {
  user_message: Message;
  assistant_message: Message;
  citations: Citation[];
  sources_used: boolean;
}

interface Document {
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

// ── API helper ────────────────────────────────────────────

const API = {
  async request<T>(url: string, options?: RequestInit): Promise<T> {
    const isFormData = options?.body instanceof FormData;
    const headers: Record<string, string> = {};
    if (!isFormData) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(url, { headers, ...options });
    if (!res.ok) {
      if (res.status === 404) {
        const detail = await res.json().catch(() => ({ detail: "Not found" }));
        throw new Error(detail.detail || "Not found");
      }
      const text = await res.text().catch(() => "Unknown error");
      throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
    }
    if (res.status === 204) return undefined as T;
    return res.json();
  },

  getHealth() { return this.request<HealthStatus>("/api/health"); },
  listSessions() { return this.request<Session[]>("/api/sessions"); },
  createSession(title = "New Research Session") {
    return this.request<Session>("/api/sessions", { method: "POST", body: JSON.stringify({ title }) });
  },
  updateSession(id: number, title: string) {
    return this.request<Session>(`/api/sessions/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });
  },
  deleteSession(id: number) { return this.request<void>(`/api/sessions/${id}`, { method: "DELETE" }); },
  listMessages(sessionId: number) { return this.request<Message[]>(`/api/sessions/${sessionId}/messages`); },
  sendMessage(sessionId: number, message: string) {
    return this.request<ChatResponse>(`/api/sessions/${sessionId}/messages`, { method: "POST", body: JSON.stringify({ message }) });
  },
  listDocuments(sessionId: number) { return this.request<Document[]>(`/api/sessions/${sessionId}/documents`); },
  uploadDocument(sessionId: number, file: File) {
    const form = new FormData();
    form.append("file", file);
    return this.request<{ document: Document; message: string }>(`/api/sessions/${sessionId}/documents`, { method: "POST", body: form });
  },
  deleteDocument(id: number) { return this.request<void>(`/api/documents/${id}`, { method: "DELETE" }); },
};

// ── Helpers ───────────────────────────────────────────────

function formatTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatDate(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  if (diff < 86_400_000) return formatTime(iso);
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function formatFileSize(bytes: number | null): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ── Citation popup component ─────────────────────────────

interface CitationPopupProps {
  citation: Citation;
  onClose: () => void;
}

function CitationPopup({ citation, onClose }: CitationPopupProps) {
  return (
    <div className="citation-overlay" onClick={onClose}>
      <div className="citation-popup" onClick={(e) => e.stopPropagation()}>
        <div className="citation-popup-header">
          <span className="citation-popup-marker">{citation.marker}</span>
          <span className="citation-popup-file">{citation.filename}</span>
          <button className="citation-popup-close" onClick={onClose}>✕</button>
        </div>
        {citation.page_number && (
          <div className="citation-popup-meta">Page {citation.page_number}</div>
        )}
        <div className="citation-popup-snippet">"{citation.snippet}"</div>
      </div>
    </div>
  );
}

// ── App Component ─────────────────────────────────────────

function App() {
  // Session state
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(true);

  // Chat state
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);

  // Citation popup state
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [sourcesUsed, setSourcesUsed] = useState(false);

  // Rename state
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");

  // Document state
  const [documents, setDocuments] = useState<Document[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [showDocs, setShowDocs] = useState(false);

  // Health state
  const [health, setHealth] = useState<HealthStatus | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── Health polling ─────────────────────────────────────

  const checkHealth = useCallback(async () => {
    try { setHealth(await API.getHealth()); } catch { setHealth(null); }
  }, []);

  useEffect(() => {
    checkHealth();
    const id = setInterval(checkHealth, 15_000);
    return () => clearInterval(id);
  }, [checkHealth]);

  // ── Session CRUD ───────────────────────────────────────

  const loadSessions = useCallback(async () => {
    setLoadingSessions(true);
    try { setSessions(await API.listSessions()); } catch { /* ignore */ }
    finally { setLoadingSessions(false); }
  }, []);

  useEffect(() => { loadSessions(); }, [loadSessions]);

  const handleCreateSession = async () => {
    try {
      const session = await API.createSession();
      setSessions((prev) => [session, ...prev]);
      setActiveSessionId(session.id);
      setMessages([]);
      setChatError(null);
      setDocuments([]);
    } catch { /* ignore */ }
  };

  const handleRenameStart = (session: Session) => {
    setRenamingId(session.id);
    setRenameValue(session.title);
  };

  const handleRenameSubmit = async (id: number) => {
    const title = renameValue.trim() || "Untitled";
    try {
      const updated = await API.updateSession(id, title);
      setSessions((prev) => prev.map((s) => (s.id === id ? updated : s)));
    } catch { /* ignore */ }
    setRenamingId(null);
  };

  const handleDeleteSession = async (id: number) => {
    try {
      await API.deleteSession(id);
      setSessions((prev) => prev.filter((s) => s.id !== id));
      if (activeSessionId === id) { setActiveSessionId(null); setMessages([]); }
    } catch { /* ignore */ }
  };

  // ── Document loading ───────────────────────────────────

  const loadDocuments = useCallback(async (sessionId: number) => {
    try { setDocuments(await API.listDocuments(sessionId)); } catch { /* ignore */ }
  }, []);

  // ── Message loading ────────────────────────────────────

  const loadMessages = useCallback(async (sessionId: number) => {
    try {
      const msgs = await API.listMessages(sessionId);
      setMessages(msgs);
      setChatError(null);
    } catch { setChatError("Could not load messages"); }
  }, []);

  const handleSelectSession = (id: number) => {
    setActiveSessionId(id);
    setChatError(null);
    loadMessages(id);
    loadDocuments(id);
    setSourcesUsed(false);
  };

  // ── Document upload ────────────────────────────────────

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !activeSessionId) return;
    setUploading(true);
    setUploadError(null);
    try {
      const result = await API.uploadDocument(activeSessionId, file);
      loadDocuments(activeSessionId);
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleDeleteDocument = async (docId: number) => {
    try {
      await API.deleteDocument(docId);
      setDocuments((prev) => prev.filter((d) => d.id !== docId));
    } catch { /* ignore */ }
  };

  // Auto-refresh documents when they're processing
  useEffect(() => {
    if (!activeSessionId) return;
    const hasProcessing = documents.some((d) => d.status === "processing");
    if (!hasProcessing) return;
    const id = setInterval(() => loadDocuments(activeSessionId), 2000);
    return () => clearInterval(id);
  }, [activeSessionId, documents]);

  // ── Send message ───────────────────────────────────────

  const handleSend = async () => {
    const text = input.trim();
    if (!text || !activeSessionId || sending) return;

    setSending(true);
    setChatError(null);

    const tempUserMsg: Message = {
      id: -Date.now(),
      session_id: activeSessionId,
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, tempUserMsg]);
    setInput("");

    try {
      const response = await API.sendMessage(activeSessionId, text);
      setMessages((prev) => {
        const filtered = prev.filter((m) => m.id !== tempUserMsg.id);
        return [...filtered, response.user_message, response.assistant_message];
      });
      setSourcesUsed(response.sources_used);
    } catch (err) {
      setChatError(err instanceof Error ? err.message : "Failed to send message");
      setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id));
    } finally {
      setSending(false);
    }
  };

  // Scroll on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Auto-resize textarea
  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = "auto";
      inputRef.current.style.height = Math.min(inputRef.current.scrollHeight, 120) + "px";
    }
  }, [input]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
  };

  // Process message content to render citations as clickable
  const renderContent = (msg: Message) => {
    if (msg.role !== "assistant" || !msg.citations) {
      return <div>{msg.content}</div>;
    }

    let parsedCitations: Citation[] = [];
    try { parsedCitations = JSON.parse(msg.citations); } catch { return <div>{msg.content}</div>; }
    if (!parsedCitations.length) return <div>{msg.content}</div>;

    // Split content by citation markers and build React fragments
    const parts: (string | { marker: string; citation: Citation })[] = [];
    const pattern = /\[(\d+)\]/g;
    let lastIndex = 0;
    let match: RegExpExecArray | null;

    while ((match = pattern.exec(msg.content)) !== null) {
      if (match.index > lastIndex) {
        parts.push(msg.content.slice(lastIndex, match.index));
      }
      const markerNum = parseInt(match[1], 10);
      const citation = parsedCitations.find((c) => c.marker === `[${markerNum}]`);
      if (citation) {
        parts.push({ marker: match[0], citation });
      } else {
        // Marker not in citations list — render as plain text
        parts.push(match[0]);
      }
      lastIndex = match.index + match[0].length;
    }

    if (lastIndex < msg.content.length) {
      parts.push(msg.content.slice(lastIndex));
    }

    return (
      <div>
        {parts.map((part, i) => {
          if (typeof part === "string") {
            return <span key={i}>{part}</span>;
          }
          return (
            <button
              key={i}
              className="citation-btn"
              onClick={() => setSelectedCitation(part.citation)}
              title={`View source: ${part.citation.filename}`}
            >
              {part.marker}
            </button>
          );
        })}
      </div>
    );
  };

  // ── Derived state ──────────────────────────────────────

  const activeSession = sessions.find((s) => s.id === activeSessionId) ?? null;
  const healthOk = health ? Object.values(health).filter((v) => v === "ok").length : 0;
  const readyDocs = documents.filter((d) => d.status === "ready").length;
  const docCount = documents.length;

  // ── Render ─────────────────────────────────────────────

  return (
    <div className="app">
      {/* ── Sidebar ─────────────────────────────────── */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <span className="sidebar-logo">🧠</span>
          <span className="sidebar-title">Research Sessions</span>
          <button className="new-session-btn" onClick={handleCreateSession}>✚ New</button>
        </div>

        <div className="sidebar-list">
          {loadingSessions ? <div className="sidebar-empty">Loading…</div>
          : sessions.length === 0 ? (
            <div className="sidebar-empty">
              <div style={{ marginBottom: "0.5rem", fontSize: "1.2rem" }}>📂</div>
              No sessions yet
            </div>
          ) : sessions.map((session) => (
            <div
              key={session.id}
              className={`sidebar-item ${session.id === activeSessionId ? "active" : ""}`}
              onClick={() => handleSelectSession(session.id)}
            >
              <span className="sidebar-item-icon">💬</span>
              {renamingId === session.id ? (
                <input className="rename-input" value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onBlur={() => handleRenameSubmit(session.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleRenameSubmit(session.id);
                    if (e.key === "Escape") setRenamingId(null);
                  }}
                  autoFocus onClick={(e) => e.stopPropagation()}
                />
              ) : (
                <>
                  <span className="sidebar-item-title">{session.title}</span>
                  <span style={{ fontSize: "0.65rem", color: "var(--text-secondary)", flexShrink: 0 }}>
                    {formatDate(session.updated_at)}
                  </span>
                  <div className="sidebar-item-actions" onClick={(e) => e.stopPropagation()}>
                    <button className="sidebar-action-btn" title="Rename" onClick={() => handleRenameStart(session)}>✎</button>
                    <button className="sidebar-action-btn delete" title="Delete" onClick={() => handleDeleteSession(session.id)}>✕</button>
                  </div>
                </>
              )}
            </div>
          ))}
        </div>

        <div className="sidebar-footer">
          <span className={`health-indicator ${health?.backend === "ok" ? "ok" : "unavailable"}`} title={`Backend: ${health?.backend ?? "?"}`}>
            <span className="health-dot" />API
          </span>
          <span className={`health-indicator ${health?.ollama === "ok" ? "ok" : "unavailable"}`} title={`Ollama: ${health?.ollama ?? "?"}`}>
            <span className="health-dot" />LLM
          </span>
          <span style={{ marginLeft: "auto", fontSize: "0.65rem", color: "var(--text-secondary)" }}>
            {healthOk === 3 ? "All OK" : `${healthOk}/3`}
          </span>
        </div>
      </aside>

      {/* ── Chat Area ────────────────────────────────── */}
      <main className="chat-area">
        {!activeSession ? (
          <div className="messages-container">
            <div className="messages-empty">
              <div className="messages-empty-icon">💬</div>
              <div className="messages-empty-text">Select or create a session</div>
              <div className="messages-empty-hint">Click "✚ New" to start a new research conversation</div>
            </div>
          </div>
        ) : (
          <>
            {/* Chat header */}
            <div className="chat-header">
              <span className="chat-header-icon">💬</span>
              <span className="chat-header-title">{activeSession.title}</span>
              <span className="chat-header-count">{messages.length} msg{messages.length !== 1 ? "s" : ""}</span>
              <button
                className={`doc-toggle-btn ${showDocs ? "active" : ""}`}
                onClick={() => setShowDocs(!showDocs)}
                title="Toggle document panel"
              >
                📄 {docCount > 0 && <span className="doc-count-badge">{docCount}</span>}
              </button>
            </div>

            {/* Document panel */}
            {showDocs && (
              <div className="doc-panel">
                <div className="doc-panel-header">
                  <span className="doc-panel-title">📄 Documents</span>
                  <span className="doc-panel-subtitle">{readyDocs} ready · {documents.filter(d => d.status === "processing").length} processing</span>
                </div>

                <div className="doc-upload-row">
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".pdf,.txt"
                    onChange={handleFileSelect}
                    style={{ display: "none" }}
                  />
                  <button
                    className="doc-upload-btn"
                    onClick={() => fileInputRef.current?.click()}
                    disabled={uploading}
                  >
                    {uploading ? "⏳ Uploading…" : "📤 Upload PDF/TXT"}
                  </button>
                  <span className="doc-upload-hint">Max 20 MB</span>
                </div>

                {uploadError && (
                  <div className="doc-error">{uploadError}</div>
                )}

                <div className="doc-list">
                  {documents.length === 0 ? (
                    <div className="doc-empty">No documents uploaded yet</div>
                  ) : documents.map((doc) => (
                    <div key={doc.id} className={`doc-item doc-${doc.status}`}>
                      <div className="doc-info">
                        <span className="doc-icon">
                          {doc.filename.endsWith(".pdf") ? "📕" : "📄"}
                        </span>
                        <div className="doc-details">
                          <span className="doc-name">{doc.filename}</span>
                          <span className="doc-meta">
                            {doc.status === "processing" && "⏳ Processing…"}
                            {doc.status === "ready" && `✅ ${doc.chunk_count} chunks`}
                            {doc.status === "failed" && `❌ Failed: ${doc.error_message || "Unknown error"}`}
                            {formatFileSize(doc.file_size) && ` · ${formatFileSize(doc.file_size)}`}
                          </span>
                        </div>
                      </div>
                      <button
                        className="doc-delete-btn"
                        onClick={() => handleDeleteDocument(doc.id)}
                        title="Delete document"
                        disabled={doc.status === "processing"}
                      >✕</button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Messages */}
            <div className="messages-container">
              {messages.length === 0 ? (
                <div className="messages-empty">
                  <div className="messages-empty-icon" style={{ fontSize: "2rem" }}>🤖</div>
                  <div className="messages-empty-text">Start a conversation</div>
                  <div className="messages-empty-hint">Ask anything about your research topic</div>
                </div>
              ) : (
                messages.map((msg) => (
                  <div key={msg.id} className={`message-wrapper ${msg.role}`}>
                    <div className="message-bubble">
                      {renderContent(msg)}
                      <div className="message-time">
                        {formatTime(msg.created_at)}
                        {msg.role === "assistant" && sourcesUsed && <span className="rag-badge">📄 RAG</span>}
                      </div>
                    </div>
                  </div>
                ))
              )}

              {sending && (
                <div className="typing-indicator">
                  <div className="typing-dots">
                    <div className="typing-dot" /><div className="typing-dot" /><div className="typing-dot" />
                  </div>
                  <span className="typing-text">AI is thinking…</span>
                </div>
              )}

              {chatError && (
                <div className="error-message">
                  <span className="error-message-icon">⚠️</span>
                  <span>{chatError}</span>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>

            {/* Input */}
            <div className="input-area">
              <div className="input-row">
                <textarea
                  ref={inputRef}
                  className="input-field"
                  placeholder={sending ? "Waiting for response…" : "Type your message…"}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  disabled={sending}
                  rows={1}
                />
                <button className="send-btn" onClick={handleSend} disabled={!input.trim() || sending} title="Send message">
                  {sending ? "…" : "➤"}
                </button>
              </div>
            </div>
          </>
        )}

        {/* Citation popup */}
        {selectedCitation && (
          <CitationPopup citation={selectedCitation} onClose={() => setSelectedCitation(null)} />
        )}
      </main>
    </div>
  );
}

export default App;
