import { useState, useEffect, useCallback, useRef } from "react";
import { useStreaming } from "./useStreaming";
import { useAuth } from "./AuthContext";
import { AuthScreen } from "./AuthScreen";
import { SystemPromptEditor } from "./SystemPromptEditor";
import { Settings } from "./Settings";
import { Sidebar } from "./Sidebar";
import { ChatArea } from "./ChatArea";
import { CitationPopup } from "./CitationPopup";
import { DocumentPanel } from "./DocumentPanel";
import { MemoryPanel } from "./MemoryPanel";
import { API } from "./api";
import type { Session, Message, Citation, Document, Memory, HealthStatus } from "./types";
import "./App.css";

// ── App Component ─────────────────────────────────────────

function App() {
  // ═══════════════════════════════════════════════════════
  // ALL hooks must be declared BEFORE any conditional return
  // to satisfy React's Rules of Hooks.
  // ═══════════════════════════════════════════════════════

  // Phase 6C: Authentication gate
  const { user, isAuthenticated, isLoading: authLoading, logout } = useAuth();

  // Session state
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(true);

  // Chat state
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [retryTarget, setRetryTarget] = useState<{ message: string; errorDetail: string } | null>(null);

  // Citation popup state
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);

  // Per-message flags
  const [sourcesUsedIds, setSourcesUsedIds] = useState<Set<number>>(new Set());
  const [memoriesUsedIds, setMemoriesUsedIds] = useState<Set<number>>(new Set());

  // Document state
  const [documents, setDocuments] = useState<Document[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [showDocs, setShowDocs] = useState(false);

  // Model & system prompt state
  const [showSystemPromptEditor, setShowSystemPromptEditor] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

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

  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const sessionListRequestRef = useRef(0);

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
    const requestId = ++sessionListRequestRef.current;
    setLoadingSessions(true);
    try {
      const data = await API.listSessions();
      if (requestId !== sessionListRequestRef.current) return;
      setSessions(data);
      // Stale-session recovery: if the selected session no longer exists in
      // the fetched list, clear it so the UI never points at a dead session.
      setActiveSessionId((prev) => {
        if (prev !== null && !data.some((s) => s.id === prev)) {
          return null;
        }
        return prev;
      });
    } catch { /* ignore */ }
    finally {
      if (requestId === sessionListRequestRef.current) setLoadingSessions(false);
    }
  }, []);

  // Reload sessions whenever auth state changes (login/logout). On logout,
  // clear account-specific state so another account never inherits it.
  useEffect(() => {
    sessionListRequestRef.current += 1;
    setSessions([]);
    setActiveSessionId(null);
    setMessages([]);
    setDocuments([]);
    setChatError(null);
    setSourcesUsedIds(new Set());
    setMemoriesUsedIds(new Set());
    if (isAuthenticated) {
      loadSessions();
    } else {
      setLoadingSessions(false);
    }
  }, [isAuthenticated, user?.id, loadSessions]);

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

  const handleRenameSession = async (id: number, title: string) => {
    try {
      const updated = await API.updateSession(id, title);
      setSessions((prev) => prev.map((s) => (s.id === id ? updated : s)));
    } catch { /* ignore */ }
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
      .then((r) => { setMemoryEnabled(r.enabled); })
      .catch(() => { /* backend might not be ready yet */ })
      .finally(() => { setMemorySettingLoaded(true); });
  }, []);

  // ── Message loading ────────────────────────────────────

  // Tracks session IDs already recovered-from-404 so we refresh the list
  // exactly once (no infinite retry loop) when a session disappears.
  const recovered404Ref = useRef<Set<number>>(new Set());

  const loadMessages = useCallback(async (sessionId: number) => {
    try {
      setMessages(await API.listMessages(sessionId));
      setChatError(null);
      recovered404Ref.current.delete(sessionId);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "";
      if (/not found/i.test(msg)) {
        // Session is no longer available (deleted or not owned).
        setChatError("Session is no longer available");
        setActiveSessionId(null);
        setMessages([]);
        // Refresh the list once; guard against an infinite retry loop.
        if (!recovered404Ref.current.has(sessionId)) {
          recovered404Ref.current.add(sessionId);
          loadSessions();
        }
      } else {
        setChatError("Could not load messages");
      }
    }
  }, [loadSessions]);

  // Ensure the selected session is always present in the latest list; if the
  // user had nothing selected, select the first valid session when appropriate.
  useEffect(() => {
    if (activeSessionId === null && sessions.length > 0 && isAuthenticated) {
      setActiveSessionId(sessions[0].id);
    }
  }, [activeSessionId, sessions, isAuthenticated]);

  const handleSelectSession = (id: number) => {
    setActiveSessionId(id);
    setChatError(null);
    loadMessages(id);
    loadDocuments(id);
    setSourcesUsedIds(new Set());
    setMemoriesUsedIds(new Set());
  };

  const handleModelChange = (sessionId: number, model: string | null) => {
    // Keep the sessions list in sync so the saved model survives
    // reopens, without triggering a full reload that could reset it.
    setSessions((prev) =>
      prev.map((s) => (s.id === sessionId ? { ...s, model } : s))
    );
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
    }
  };

  const handleDeleteDocument = async (docId: number) => {
    try {
      await API.deleteDocument(docId);
      setDocuments((prev) => prev.filter((d) => d.id !== docId));
    } catch { /* ignore */ }
  };

  useEffect(() => {
    if (!activeSessionId) return;
    const hasProcessing = documents.some((d) => d.status === "processing");
    if (!hasProcessing) return;
    const id = setInterval(() => loadDocuments(activeSessionId), 2000);
    return () => clearInterval(id);
  }, [activeSessionId, documents]);

  // ── Memory CRUD ────────────────────────────────────────

  const handleToggleMemory = async () => {
    const newValue = !memoryEnabled;
    setTogglePending(true);
    try {
      const result = await API.setMemorySetting(newValue);
      setMemoryEnabled(result.enabled);
      if (!result.enabled) setMemoriesUsedIds(new Set());
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
      onStart: () => setGenerationStopped(false),
      onToken: () => {},
      onComplete: (result) => {
        setGenerationStopped(false);
        if (result.sourcesUsed && result.messageId) {
          setSourcesUsedIds((prev) => new Set(prev).add(result.messageId));
        }
        if (result.memoriesUsed && result.messageId) {
          setMemoriesUsedIds((prev) => new Set(prev).add(result.messageId));
        }
        loadMessages(activeSessionId);
        loadMemories();
        setSending(false);
      },
      onError: (error) => {
        setChatError(error.detail || "Failed to send message");
        setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id));
        setRetryTarget({ message: originalText, errorDetail: error.detail || "Failed to send message" });
        setSending(false);
        setGenerationStopped(false);
      },
      onCancelled: () => {
        setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id));
        setSending(false);
        setGenerationStopped(true);
        setTimeout(() => setGenerationStopped(false), 3000);
      },
    });
  };

  const handleRetry = useCallback(() => {
    if (!retryTarget || !activeSessionId || isStreaming) return;
    handleSend(retryTarget.message);
  }, [retryTarget, activeSessionId, isStreaming]);

  // ═══════════════════════════════════════════════════════
  // Auth gate — after all hooks, before derived state & render
  // ═══════════════════════════════════════════════════════

  if (authLoading) {
    return (
      <div className="auth-loading-screen">
        <div className="auth-loading-spinner" />
        <p>Restoring session…</p>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <AuthScreen />;
  }

  // ── Derived state ──────────────────────────────────────

  const activeSession = sessions.find((s) => s.id === activeSessionId) ?? null;
  const docCount = documents.length;
  const readyDocs = documents.filter((d) => d.status === "ready").length;

  // ── Render ─────────────────────────────────────────────

  return (
    <div className="app min-h-screen bg-background text-foreground transition-colors duration-300">
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        loadingSessions={loadingSessions}
        health={health}
        onSelectSession={handleSelectSession}
        onCreateSession={handleCreateSession}
        onDeleteSession={handleDeleteSession}
        onRenameSession={handleRenameSession}
        onOpenSettings={() => setShowSettings(true)}
        onLogout={logout}
      />

      <ChatArea
        activeSession={activeSession}
        messages={messages}
        sourcesUsedIds={sourcesUsedIds}
        memoriesUsedIds={memoriesUsedIds}
        onCitationClick={(citation: Citation) => setSelectedCitation(citation)}
        input={input}
        onInputChange={setInput}
        onSend={() => handleSend()}
        onKeyDown={(e: React.KeyboardEvent) => {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
        }}
        inputRef={inputRef}
        isStreaming={isStreaming}
        streamedContent={streamedContent}
        generationStopped={generationStopped}
        onCancelStream={cancelStream}
        chatError={chatError}
        retryTarget={retryTarget}
        onRetry={handleRetry}
        sessionModel={activeSession?.model ?? null}
        onModelChange={(model) => {
          if (activeSession) handleModelChange(activeSession.id, model);
        }}
        onOpenSystemPrompt={() => setShowSystemPromptEditor(true)}
        showDocs={showDocs}
        docCount={docCount}
        onToggleDocs={() => setShowDocs(!showDocs)}
        memorySettingLoaded={memorySettingLoaded}
        memoryEnabled={memoryEnabled}
        togglePending={togglePending}
        memoriesLength={memories.length}
        showMemories={showMemories}
        onToggleMemory={handleToggleMemory}
        onToggleMemories={() => { setShowMemories(!showMemories); loadMemories(); }}
      />

      {/* Document panel */}
      {showDocs && activeSession && (
        <DocumentPanel
          documents={documents}
          uploading={uploading}
          uploadError={uploadError}
          readyDocs={readyDocs}
          onFileSelect={handleFileSelect}
          onDeleteDocument={handleDeleteDocument}
        />
      )}

      {/* Memory panel */}
      {showMemories && (
        <MemoryPanel
          memories={memories}
          memoryEnabled={memoryEnabled}
          memorySettingLoaded={memorySettingLoaded}
          memoryError={memoryError}
          togglePending={togglePending}
          addingMemory={addingMemory}
          newMemoryContent={newMemoryContent}
          newMemoryCategory={newMemoryCategory}
          editingMemoryId={editingMemoryId}
          editMemoryContent={editMemoryContent}
          editMemoryCategory={editMemoryCategory}
          showClearConfirm={showClearConfirm}
          onToggleMemory={handleToggleMemory}
          onSetAddingMemory={setAddingMemory}
          onSetNewMemoryContent={setNewMemoryContent}
          onSetNewMemoryCategory={setNewMemoryCategory}
          onAddMemory={handleAddMemory}
          onEditMemory={handleEditMemory}
          onSetEditingMemoryId={setEditingMemoryId}
          onSetEditMemoryContent={setEditMemoryContent}
          onSetEditMemoryCategory={setEditMemoryCategory}
          onDeleteMemory={handleDeleteMemory}
          onClearAllMemories={handleClearAllMemories}
          onSetShowClearConfirm={setShowClearConfirm}
        />
      )}

      {/* Citation popup */}
      {selectedCitation && (
        <CitationPopup citation={selectedCitation} onClose={() => setSelectedCitation(null)} />
      )}

      {/* System prompt editor modal */}
      {showSystemPromptEditor && activeSession && (
        <SystemPromptEditor sessionId={activeSession.id} onClose={() => setShowSystemPromptEditor(false)} />
      )}

      {/* Settings modal */}
      {showSettings && (
        <Settings onClose={() => setShowSettings(false)} />
      )}
    </div>
  );
}

export default App;
