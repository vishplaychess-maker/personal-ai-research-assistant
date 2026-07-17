import { useState, useEffect, useCallback, useRef } from "react";
import { useStreaming } from "./useStreaming";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ModelSelector } from "./ModelSelector";
import { SystemPromptEditor } from "./SystemPromptEditor";
import { API } from "./api";
import type { Session, Message, Citation, Document, Memory, HealthStatus } from "./types";
import "./App.css";

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
  const [retryTarget, setRetryTarget] = useState<{
    message: string;
    errorDetail: string;
  } | null>(null);

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

  // Model & system prompt state
  const [sessionModel, setSessionModel] = useState<string | null>(null);
  const [showSystemPromptEditor, setShowSystemPromptEditor] = useState(false);

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

  // Streaming state
  const { isStreaming, streamedContent, startStream, cancelStream } = useStreaming();
  const [generationStopped, setGenerationStopped] = useState(false);
  const streamingEndRef = useRef<HTMLDivElement>(null);

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
    // Load model from session data
    const sess = sessions.find((s) => s.id === id);
    if (sess) setSessionModel(sess.model);
  };

  // Update session model when user selects from dropdown
  const handleModelChange = (model: string | null) => {
    setSessionModel(model);
    // Reload sessions to get updated data
    loadSessions();
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

  // ── Send message (streaming) ───────────────────────────

  const handleSend = (overrideText?: string) => {
    const text = (overrideText ?? input).trim();
    if (!text || !activeSessionId || isStreaming || togglePending) return;

    const originalText = text;

    setSending(true);
    setChatError(null);
    setRetryTarget(null);

    const tempUserMsg: Message = {
      id: -Date.now(),
      session_id: activeSessionId,
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, tempUserMsg]);
    if (!overrideText) setInput("");

    startStream(activeSessionId, text, {
      onStart: () => {
        setGenerationStopped(false);
      },
      onToken: (_token: string) => {
        // Content updated via streamedContent state in the hook
      },
      onComplete: (result) => {
        setGenerationStopped(false);
        // Set badges from the complete event metadata
        if (result.sourcesUsed && result.messageId) {
          setSourcesUsedIds((prev) => new Set(prev).add(result.messageId));
        }
        if (result.memoriesUsed && result.messageId) {
          setMemoriesUsedIds((prev) => new Set(prev).add(result.messageId));
        }
        // Refresh messages from server to get proper persisted IDs
        loadMessages(activeSessionId);
        loadMemories();
        setSending(false);
      },
      onError: (error) => {
        setChatError(error.detail || "Failed to send message");
        setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id));
        setRetryTarget({
          message: originalText,
          errorDetail: error.detail || "Failed to send message",
        });
        setSending(false);
        setGenerationStopped(false);
      },
      onCancelled: () => {
        // Remove the temp user message — backend did not persist anything
        setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id));
        setSending(false);
        setGenerationStopped(true);
        // Auto-dismiss the stopped message after 3 seconds
        setTimeout(() => setGenerationStopped(false), 3000);
      },
    });
  };

  const handleRetry = useCallback(() => {
    if (!retryTarget || !activeSessionId || isStreaming) return;
    handleSend(retryTarget.message);
  }, [retryTarget, activeSessionId, isStreaming]);

  // Scroll on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Scroll during streaming (when new tokens arrive)
  useEffect(() => {
    if (isStreaming && streamedContent) {
      streamingEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [streamedContent, isStreaming]);

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

  // Process message content to render with Markdown and citations
  const renderContent = (msg: Message) => {
    if (msg.role !== "assistant") {
      return <div className="message-content">{msg.content}</div>;
    }

    let parsedCitations: Citation[] = [];
    try {
      parsedCitations = msg.citations ? JSON.parse(msg.citations) : [];
    } catch {
      parsedCitations = [];
    }

    return (
      <div className="message-content">
        <MarkdownRenderer
          content={msg.content}
          citations={parsedCitations}
          onCitationClick={(citation) => setSelectedCitation(citation)}
        />
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
              <ModelSelector
                sessionId={activeSession.id}
                currentModel={sessionModel}
                onModelChange={handleModelChange}
              />

              <button
                className={`sp-toggle-btn ${showSystemPromptEditor ? "active" : ""}`}
                onClick={() => setShowSystemPromptEditor(true)}
                title="Edit system prompt"
              >
                ⚙
              </button>

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

              {isStreaming && (
                <div className="message-wrapper assistant">
                  <div className="message-bubble streaming">
                    <div className="message-content streaming-text">
                      {streamedContent ? (
                        <>{streamedContent}<span className="streaming-cursor">▊</span></>
                      ) : (
                        <span className="streaming-cursor">▊</span>
                      )}
                    </div>
                    <div className="message-time">
                      <span className="streaming-badge">⏳ Generating…</span>
                    </div>
                  </div>
                </div>
              )}
              {generationStopped && (
                <div className="generation-stopped">
                  ⏹ Generation stopped — partial response was not saved
                </div>
              )}

              {chatError && (
                <div className="error-message">
                  <div className="error-message-content">
                    <span className="error-message-icon">⚠️</span>
                    <span>{chatError}</span>
                  </div>
                  {retryTarget && !isStreaming && (
                    <button
                      className="retry-btn"
                      onClick={() => handleRetry()}
                      title="Retry with the same message"
                    >
                      🔄 Retry
                    </button>
                  )}
                </div>
              )}

              <div ref={messagesEndRef} />
              <div ref={streamingEndRef} />
            </div>

            {/* Input */}
            <div className="input-area">
              <div className="input-row">
                <textarea
                  ref={inputRef}
                  className="input-field"
                  placeholder={isStreaming ? "Generating response…" : "Type your message…"}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  disabled={isStreaming}
                  rows={1}
                />
                {isStreaming ? (
                  <button className="stop-btn" onClick={cancelStream} title="Stop generation">
                    ⏹ Stop
                  </button>
                ) : (
                  <button className="send-btn" onClick={() => handleSend()} disabled={!input.trim() || isStreaming} title="Send message">
                    ➤
                  </button>
                )}
              </div>
              {isStreaming && (
                <div className="streaming-hint">
                  Click Stop to cancel — partial response will not be saved
                </div>
              )}
            </div>
          </>
        )}

        {/* Citation popup */}
        {selectedCitation && (
          <CitationPopup citation={selectedCitation} onClose={() => setSelectedCitation(null)} />
        )}

        {/* System prompt editor modal */}
        {showSystemPromptEditor && activeSession && (
          <SystemPromptEditor
            sessionId={activeSession.id}
            onClose={() => setShowSystemPromptEditor(false)}
          />
        )}
      </main>
    </div>
  );
}

export default App;
