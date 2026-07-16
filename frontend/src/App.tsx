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
  memories_used: boolean;
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

interface Memory {
  id: number;
  user_id: number;
  session_id: number | null;
  content: string;
  category: string;
  created_at: string;
  last_used_at: string;
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
      const detail = await res.json().catch(() => ({ detail: "Unknown error" }));
      throw new Error(detail.detail || `HTTP ${res.status}`);
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
  listMemories() { return this.request<Memory[]>("/api/memories"); },
  createMemory(content: string, category: string) {
    return this.request<Memory>("/api/memories", { method: "POST", body: JSON.stringify({ content, category }) });
  },
  updateMemory(id: number, content: string, category: string) {
    return this.request<Memory>(`/api/memories/${id}`, { method: "PATCH", body: JSON.stringify({ content, category }) });
  },
  deleteMemory(id: number) { return this.request<void>(`/api/memories/${id}`, { method: "DELETE" }); },
  clearAllMemories() { return this.request<void>("/api/memories", { method: "DELETE", body: JSON.stringify({ confirm: true }) }); },
  getMemorySetting() { return this.request<{ enabled: boolean }>("/api/settings/memory"); },
  setMemorySetting(enabled: boolean) {
    return this.request<{ enabled: boolean }>("/api/settings/memory", { method: "PATCH", body: JSON.stringify({ enabled }) });
  },
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

  // Per-message flags: which message IDs have sources/memories associated
  const [sourcesUsedIds, setSourcesUsedIds] = useState<Set<number>>(new Set());
  const [memoriesUsedIds, setMemoriesUsedIds] = useState<Set<number>>(new Set());

  // Rename state
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");

  // Document state
  const [documents, setDocuments] = useState<Document[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [showDocs, setShowDocs] = useState(false);

  // Memory state
  const [memories, setMemories] = useState<Memory[]>([]);
  const [showMemories, setShowMemories] = useState(false);
  const [memoryEnabled, setMemoryEnabled] = useState(true);
  const [memorySettingLoaded, setMemorySettingLoaded] = useState(false);
  const [togglePending, setTogglePending] = useState(false);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [addingMemory, setAddingMemory] = useState(false);
  const [newMemoryContent, setNewMemoryContent] = useState("");
  const [newMemoryCategory, setNewMemoryCategory] = useState("fact");
  const [editingMemoryId, setEditingMemoryId] = useState<number | null>(null);
  const [editMemoryContent, setEditMemoryContent] = useState("");
  const [editMemoryCategory, setEditMemoryCategory] = useState("fact");
  const [showClearConfirm, setShowClearConfirm] = useState(false);

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
      setSourcesUsedIds(new Set());
      setMemoriesUsedIds(new Set());
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

  // ── Memory loading ─────────────────────────────────────

  const loadMemories = useCallback(async () => {
    try { setMemories(await API.listMemories()); } catch { /* ignore */ }
  }, []);

  // ── Load memory setting from backend on mount ─────────
  useEffect(() => {
    API.getMemorySetting()
      .then((r) => {
        setMemoryEnabled(r.enabled);
      })
      .catch(() => {
        /* backend might not be ready yet */
      })
      .finally(() => {
        setMemorySettingLoaded(true);
      });
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
    setSourcesUsedIds(new Set());
    setMemoriesUsedIds(new Set());
  };

  // ── Document upload ────────────────────────────────────

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !activeSessionId) return;
    setUploading(true);
    setUploadError(null);
    try {
      await API.uploadDocument(activeSessionId, file);
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

  // ── Memory CRUD ────────────────────────────────────────

  // Toggle memory on/off — persists to backend immediately
  const handleToggleMemory = async () => {
    const newValue = !memoryEnabled;
    setTogglePending(true);
    try {
      const result = await API.setMemorySetting(newValue);
      setMemoryEnabled(result.enabled);
      // When disabling memory, clear any stale memory badges
      if (!result.enabled) {
        setMemoriesUsedIds(new Set());
      }
    } catch {
      setMemoryError("Failed to update memory setting — toggle was not saved");
    } finally {
      setTogglePending(false);
    }
  };

  const handleAddMemory = async () => {
    const text = newMemoryContent.trim();
    if (!text) return;
    try {
      await API.createMemory(text, newMemoryCategory);
      setNewMemoryContent("");
      setAddingMemory(false);
      loadMemories();
      setMemoryError(null);
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : "Failed to add memory");
    }
  };

  const handleEditMemory = async (id: number) => {
    const text = editMemoryContent.trim();
    if (!text) return;
    try {
      await API.updateMemory(id, text, editMemoryCategory);
      setEditingMemoryId(null);
      loadMemories();
      setMemoryError(null);
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : "Failed to update memory");
    }
  };

  const handleDeleteMemory = async (id: number) => {
    try {
      await API.deleteMemory(id);
      setMemories((prev) => prev.filter((m) => m.id !== id));
    } catch { /* ignore */ }
  };

  const handleClearAllMemories = async () => {
    try {
      await API.clearAllMemories();
      setMemories([]);
      setShowClearConfirm(false);
    } catch { /* ignore */ }
  };

  // ── Send message ───────────────────────────────────────

  const handleSend = async () => {
    const text = input.trim();
    if (!text || !activeSessionId || sending || togglePending) return;

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
      if (response.sources_used) {
        setSourcesUsedIds((prev) => new Set(prev).add(response.assistant_message.id));
      }
      if (response.memories_used) {
        setMemoriesUsedIds((prev) => new Set(prev).add(response.assistant_message.id));
      }
      // Reload memories after message in case a new one was extracted
      loadMemories();
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
      return <div className="message-content">{msg.content}</div>;
    }

    let parsedCitations: Citation[] = [];
    try { parsedCitations = JSON.parse(msg.citations); } catch { return <div className="message-content">{msg.content}</div>; }
    if (!parsedCitations.length) return <div className="message-content">{msg.content}</div>;

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
        parts.push(match[0]);
      }
      lastIndex = match.index + match[0].length;
    }

    if (lastIndex < msg.content.length) {
      parts.push(msg.content.slice(lastIndex));
    }

    return (
      <div className="message-content">
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

  // ── Category helpers ───────────────────────────────────

  const categoryIcon: Record<string, string> = {
    fact: "💡",
    preference: "⭐",
    research_interest: "🔬",
    project_context: "📋",
  };

  const categoryLabel: Record<string, string> = {
    fact: "Fact",
    preference: "Preference",
    research_interest: "Interest",
    project_context: "Context",
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
              <div className="mem-toggle-group">
                <span className={`mem-status ${memorySettingLoaded ? (memoryEnabled ? "enabled" : "disabled") : "loading"}`}>
                  {!memorySettingLoaded ? "⏳ Sync…" : memoryEnabled ? "🧠 On" : "🧠 Off"}
                </span>
                <button
                  className="mem-power-btn"
                  onClick={handleToggleMemory}
                  disabled={togglePending || !memorySettingLoaded}
                  title={!memorySettingLoaded ? "Loading…" : memoryEnabled ? "Disable memory" : "Enable memory"}
                >
                  {togglePending ? "⏳" : !memorySettingLoaded ? "⋯" : memoryEnabled ? "⏻" : "⏼"}
                </button>
                <button
                  className={`mem-toggle-btn ${showMemories ? "active" : ""}`}
                  onClick={() => { setShowMemories(!showMemories); loadMemories(); }}
                  title="Open memory panel"
                  disabled={!memorySettingLoaded || !memoryEnabled}
                >
                  📝 {memories.length > 0 && <span className="mem-count-badge">{memories.length}</span>}
                </button>
              </div>
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

            {/* Memory panel */}
            {showMemories && (
              <div className="mem-panel">
                <div className="mem-panel-header">
              <span className="mem-panel-title">🧠 Long-Term Memory</span>
              <span className="mem-panel-subtitle">{memories.length} saved · Stored locally in SQLite</span>
              <span className={`mem-toggle-label ${memorySettingLoaded ? (memoryEnabled ? "enabled" : "disabled") : "loading"}`}>
                {!memorySettingLoaded ? "⏳ Syncing…" : memoryEnabled ? "🟢 Memory enabled" : "🔴 Memory disabled"}
              </span>
                </div>

                {memoryError && <div className="mem-error">{memoryError}</div>}

                {/* Add memory form */}
                {addingMemory ? (
                  <div className="mem-add-form">
                    <select
                      className="mem-category-select"
                      value={newMemoryCategory}
                      onChange={(e) => setNewMemoryCategory(e.target.value)}
                    >
                      <option value="fact">💡 Fact</option>
                      <option value="preference">⭐ Preference</option>
                      <option value="research_interest">🔬 Research Interest</option>
                      <option value="project_context">📋 Project Context</option>
                    </select>
                    <input
                      className="mem-add-input"
                      placeholder="What should I remember?"
                      value={newMemoryContent}
                      onChange={(e) => setNewMemoryContent(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleAddMemory(); }
                        if (e.key === "Escape") { setAddingMemory(false); setNewMemoryContent(""); }
                      }}
                      autoFocus
                    />
                    <div className="mem-add-actions">
                      <button className="mem-save-btn" onClick={handleAddMemory}>Save</button>
                      <button className="mem-cancel-btn" onClick={() => { setAddingMemory(false); setNewMemoryContent(""); }}>Cancel</button>
                    </div>
                  </div>
                ) : (
                  <div className="mem-add-row">
                    <button className="mem-add-btn" onClick={() => setAddingMemory(true)}>✚ Add Memory</button>
                    {memories.length > 0 && (
                      <>
                        {showClearConfirm ? (
                          <div className="mem-clear-confirm">
                            <span>Clear all memories?</span>
                            <button className="mem-confirm-yes" onClick={handleClearAllMemories}>Yes</button>
                            <button className="mem-confirm-no" onClick={() => setShowClearConfirm(false)}>No</button>
                          </div>
                        ) : (
                          <button className="mem-clear-btn" onClick={() => setShowClearConfirm(true)}>🗑 Clear All</button>
                        )}
                      </>
                    )}
                  </div>
                )}

                <div className="mem-list">
                  {memories.length === 0 ? (
                    <div className="mem-empty">No saved memories yet. Memories are automatically saved when you share durable facts or preferences.</div>
                  ) : memories.map((mem) => (
                    <div key={mem.id} className="mem-item">
                      {editingMemoryId === mem.id ? (
                        <div className="mem-edit-form">
                          <select
                            className="mem-category-select"
                            value={editMemoryCategory}
                            onChange={(e) => setEditMemoryCategory(e.target.value)}
                          >
                            <option value="fact">💡 Fact</option>
                            <option value="preference">⭐ Preference</option>
                            <option value="research_interest">🔬 Research Interest</option>
                            <option value="project_context">📋 Project Context</option>
                          </select>
                          <input
                            className="mem-edit-input"
                            value={editMemoryContent}
                            onChange={(e) => setEditMemoryContent(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleEditMemory(mem.id); }
                              if (e.key === "Escape") { setEditingMemoryId(null); }
                            }}
                            autoFocus
                          />
                          <div className="mem-edit-actions">
                            <button className="mem-save-btn" onClick={() => handleEditMemory(mem.id)}>Save</button>
                            <button className="mem-cancel-btn" onClick={() => setEditingMemoryId(null)}>Cancel</button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <div className="mem-icon">{categoryIcon[mem.category] || "💡"}</div>
                          <div className="mem-details">
                            <span className="mem-content">{mem.content}</span>
                            <span className="mem-meta">
                              <span className={`mem-category-tag mem-cat-${mem.category}`}>
                                {categoryLabel[mem.category] || mem.category}
                              </span>
                              <span> · Saved {formatDate(mem.created_at)}</span>
                            </span>
                          </div>
                          <div className="mem-actions">
                            <button
                              className="mem-action-btn"
                              title="Edit"
                              onClick={() => {
                                setEditingMemoryId(mem.id);
                                setEditMemoryContent(mem.content);
                                setEditMemoryCategory(mem.category);
                              }}
                            >✎</button>
                            <button
                              className="mem-action-btn delete"
                              title="Delete"
                              onClick={() => handleDeleteMemory(mem.id)}
                            >✕</button>
                          </div>
                        </>
                      )}
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
                        {msg.role === "assistant" && sourcesUsedIds.has(msg.id) && <span className="badge rag-badge">📄 RAG</span>}
                        {msg.role === "assistant" && memoriesUsedIds.has(msg.id) && <span className="badge mem-badge">🧠 Memory</span>}
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
