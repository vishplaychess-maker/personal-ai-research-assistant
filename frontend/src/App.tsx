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
  created_at: string;
}

interface ChatResponse {
  user_message: Message;
  assistant_message: Message;
}

// ── API helper ────────────────────────────────────────────

const API = {
  async request<T>(url: string, options?: RequestInit): Promise<T> {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
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

  getHealth() {
    return this.request<HealthStatus>("/api/health");
  },

  listSessions() {
    return this.request<Session[]>("/api/sessions");
  },

  createSession(title = "New Research Session") {
    return this.request<Session>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title }),
    });
  },

  updateSession(id: number, title: string) {
    return this.request<Session>(`/api/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
  },

  deleteSession(id: number) {
    return this.request<void>(`/api/sessions/${id}`, { method: "DELETE" });
  },

  listMessages(sessionId: number) {
    return this.request<Message[]>(`/api/sessions/${sessionId}/messages`);
  },

  sendMessage(sessionId: number, message: string) {
    return this.request<ChatResponse>(`/api/sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
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

  // Rename state
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");

  // Health state
  const [health, setHealth] = useState<HealthStatus | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // ── Health polling ─────────────────────────────────────

  const checkHealth = useCallback(async () => {
    try {
      const data = await API.getHealth();
      setHealth(data);
    } catch {
      setHealth(null);
    }
  }, []);

  useEffect(() => {
    checkHealth();
    const id = setInterval(checkHealth, 15_000);
    return () => clearInterval(id);
  }, [checkHealth]);

  // ── Session CRUD ───────────────────────────────────────

  const loadSessions = useCallback(async () => {
    setLoadingSessions(true);
    try {
      const list = await API.listSessions();
      setSessions(list);
    } catch (err) {
      console.error("Failed to load sessions:", err);
    } finally {
      setLoadingSessions(false);
    }
  }, []);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const handleCreateSession = async () => {
    try {
      const session = await API.createSession();
      setSessions((prev) => [session, ...prev]);
      setActiveSessionId(session.id);
      setMessages([]);
      setChatError(null);
    } catch (err) {
      console.error("Failed to create session:", err);
    }
  };

  const handleRenameStart = (session: Session) => {
    setRenamingId(session.id);
    setRenameValue(session.title);
  };

  const handleRenameSubmit = async (id: number) => {
    const title = renameValue.trim() || "Untitled";
    try {
      const updated = await API.updateSession(id, title);
      setSessions((prev) =>
        prev.map((s) => (s.id === id ? updated : s))
      );
    } catch (err) {
      console.error("Failed to rename:", err);
    }
    setRenamingId(null);
  };

  const handleDeleteSession = async (id: number) => {
    try {
      await API.deleteSession(id);
      setSessions((prev) => prev.filter((s) => s.id !== id));
      if (activeSessionId === id) {
        setActiveSessionId(null);
        setMessages([]);
      }
    } catch (err) {
      console.error("Failed to delete:", err);
    }
  };

  // ── Message loading ────────────────────────────────────

  const loadMessages = useCallback(async (sessionId: number) => {
    try {
      const msgs = await API.listMessages(sessionId);
      setMessages(msgs);
      setChatError(null);
    } catch (err) {
      console.error("Failed to load messages:", err);
      setChatError("Could not load messages");
    }
  }, []);

  const handleSelectSession = (id: number) => {
    setActiveSessionId(id);
    setChatError(null);
    loadMessages(id);
  };

  // ── Send message ───────────────────────────────────────

  const handleSend = async () => {
    const text = input.trim();
    if (!text || !activeSessionId || sending) return;

    setSending(true);
    setChatError(null);

    // Optimistically add user message
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
      // Replace the temp message with the real one, and add assistant response
      setMessages((prev) => {
        const filtered = prev.filter((m) => m.id !== tempUserMsg.id);
        return [...filtered, response.user_message, response.assistant_message];
      });
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : "Failed to send message";
      setChatError(errMsg);
      // Remove the optimistic message
      setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id));
    } finally {
      setSending(false);
    }
  };

  // Auto-scroll on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Auto-resize textarea
  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = "auto";
      inputRef.current.style.height =
        Math.min(inputRef.current.scrollHeight, 120) + "px";
    }
  }, [input]);

  // Keyboard shortcut
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // ── Derived state ──────────────────────────────────────

  const activeSession = sessions.find((s) => s.id === activeSessionId) ?? null;
  const healthOk = health
    ? Object.values(health).filter((v) => v === "ok").length
    : 0;

  // ── Render ─────────────────────────────────────────────

  return (
    <div className="app">
      {/* ── Sidebar ─────────────────────────────────── */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <span className="sidebar-logo">🧠</span>
          <span className="sidebar-title">Research Sessions</span>
          <button className="new-session-btn" onClick={handleCreateSession}>
            ✚ New
          </button>
        </div>

        <div className="sidebar-list">
          {loadingSessions ? (
            <div className="sidebar-empty">Loading…</div>
          ) : sessions.length === 0 ? (
            <div className="sidebar-empty">
              <div style={{ marginBottom: "0.5rem", fontSize: "1.2rem" }}>📂</div>
              No sessions yet
            </div>
          ) : (
            sessions.map((session) => (
              <div
                key={session.id}
                className={`sidebar-item ${
                  session.id === activeSessionId ? "active" : ""
                }`}
                onClick={() => handleSelectSession(session.id)}
              >
                <span className="sidebar-item-icon">💬</span>

                {renamingId === session.id ? (
                  <input
                    className="rename-input"
                    value={renameValue}
                    onChange={(e) => setRenameValue(e.target.value)}
                    onBlur={() => handleRenameSubmit(session.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleRenameSubmit(session.id);
                      if (e.key === "Escape") setRenamingId(null);
                    }}
                    autoFocus
                    onClick={(e) => e.stopPropagation()}
                  />
                ) : (
                  <>
                    <span className="sidebar-item-title">{session.title}</span>
                    <span style={{ fontSize: "0.65rem", color: "var(--text-secondary)", flexShrink: 0 }}>
                      {formatDate(session.updated_at)}
                    </span>
                    <div
                      className="sidebar-item-actions"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <button
                        className="sidebar-action-btn"
                        title="Rename"
                        onClick={() => handleRenameStart(session)}
                      >
                        ✎
                      </button>
                      <button
                        className="sidebar-action-btn delete"
                        title="Delete"
                        onClick={() => handleDeleteSession(session.id)}
                      >
                        ✕
                      </button>
                    </div>
                  </>
                )}
              </div>
            ))
          )}
        </div>

        {/* Small health indicator */}
        <div className="sidebar-footer">
          <span
            className={`health-indicator ${health?.backend === "ok" ? "ok" : "unavailable"}`}
            title={`Backend: ${health?.backend ?? "?"}`}
          >
            <span className="health-dot" />
            API
          </span>
          <span
            className={`health-indicator ${health?.ollama === "ok" ? "ok" : "unavailable"}`}
            title={`Ollama: ${health?.ollama ?? "?"}`}
          >
            <span className="health-dot" />
            LLM
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
              <div className="messages-empty-hint">
                Click "✚ New" to start a new research conversation
              </div>
            </div>
          </div>
        ) : (
          <>
            {/* Chat header */}
            <div className="chat-header">
              <span className="chat-header-icon">💬</span>
              <span className="chat-header-title">{activeSession.title}</span>
              <span className="chat-header-count">
                {messages.length} msg{messages.length !== 1 ? "s" : ""}
              </span>
            </div>

            {/* Messages */}
            <div className="messages-container">
              {messages.length === 0 ? (
                <div className="messages-empty">
                  <div className="messages-empty-icon" style={{ fontSize: "2rem" }}>
                    🤖
                  </div>
                  <div className="messages-empty-text">Start a conversation</div>
                  <div className="messages-empty-hint">
                    Ask anything about your research topic
                  </div>
                </div>
              ) : (
                messages.map((msg) => (
                  <div
                    key={msg.id}
                    className={`message-wrapper ${msg.role}`}
                  >
                    <div className="message-bubble">
                      <div>{msg.content}</div>
                      <div className="message-time">{formatTime(msg.created_at)}</div>
                    </div>
                  </div>
                ))
              )}

              {/* Typing indicator */}
              {sending && (
                <div className="typing-indicator">
                  <div className="typing-dots">
                    <div className="typing-dot" />
                    <div className="typing-dot" />
                    <div className="typing-dot" />
                  </div>
                  <span className="typing-text">AI is thinking…</span>
                </div>
              )}

              {/* Error message */}
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
                  placeholder={
                    sending ? "Waiting for response…" : "Type your message…"
                  }
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  disabled={sending}
                  rows={1}
                />
                <button
                  className="send-btn"
                  onClick={handleSend}
                  disabled={!input.trim() || sending}
                  title="Send message"
                >
                  {sending ? "…" : "➤"}
                </button>
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}

export default App;
