import { useState, useEffect, useCallback, useRef } from "react";
import { useStreaming } from "./useStreaming";
import { SystemPromptEditor } from "./SystemPromptEditor";
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

  const inputRef = useRef<HTMLTextAreaElement | null>(null);

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

  const loadMessages = useCallback(async (sessionId: number) => {
    try {
      setMessages(await API.listMessages(sessionId));
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
    const sess = sessions.find((s) => s.id === id);
    if (sess) setSessionModel(sess.model);
  };

  const handleModelChange = (model: string | null) => {
    setSessionModel(model);
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

  // ── Derived state ──────────────────────────────────────

  const activeSession = sessions.find((s) => s.id === activeSessionId) ?? null;
  const docCount = documents.length;
  const readyDocs = documents.filter((d) => d.status === "ready").length;

  // ── Render ─────────────────────────────────────────────

  return (
    <div className="app">
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        loadingSessions={loadingSessions}
        health={health}
        onSelectSession={handleSelectSession}
        onCreateSession={handleCreateSession}
        onDeleteSession={handleDeleteSession}
        onRenameSession={handleRenameSession}
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
        sessionModel={sessionModel}
        onModelChange={handleModelChange}
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
    </div>
  );
}

export default App;
