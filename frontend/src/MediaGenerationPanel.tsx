/**
 * MediaGenerationPanel — full-screen overlay for AI image & video generation.
 *
 * Self-contained: fetches /api/media/providers on open, owns its form and
 * result state. Rendered by App.tsx when `showMediaGeneration` is true.
 * Results are handed back to the chat transcript via `onMediaGenerated`.
 *
 * Separate from the agent/skill flow — this is a manual generation panel.
 */

import { useEffect, useState } from "react";
import { X, Image as ImageIcon, Video as VideoIcon, Loader2, Copy, ExternalLink } from "lucide-react";
import { API } from "./api";
import type { MediaProviderInfo } from "./types";

interface Props {
  onClose: () => void;
  onMediaGenerated?: (type: "image" | "video", url: string) => void;
}

const PROVIDER_LABELS: Record<string, string> = {
  venice: "Venice AI",
  nexa: "NexaAPI",
  agnes: "Agnes AI",
};

export function MediaGenerationPanel({ onClose, onMediaGenerated }: Props) {
  const [activeTab, setActiveTab] = useState<"image" | "video">("image");
  const [providers, setProviders] = useState<MediaProviderInfo[]>([]);
  const [selectedProvider, setSelectedProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [width, setWidth] = useState(1024);
  const [height, setHeight] = useState(1024);
  const [duration, setDuration] = useState("5s");
  const [busy, setBusy] = useState(false);
  const [polling, setPolling] = useState(false);
  const [result, setResult] = useState<{ url: string; type: "image" | "video" } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Load providers once on open.
  useEffect(() => {
    let alive = true;
    API.listMediaProviders()
      .then((data) => {
        if (!alive) return;
        setProviders(data.providers);
        const primary = data.providers.find((p) => p.name === data.primary && p.has_api_key);
        const firstAvailable = data.providers.find((p) => p.has_api_key);
        const pick = primary || firstAvailable;
        if (pick) {
          setSelectedProvider(pick.name);
          setSelectedModel(pick.default_image_model);
        }
      })
      .catch((e) => alive && setLoadError(e instanceof Error ? e.message : "Failed to load providers"));
    return () => {
      alive = false;
    };
  }, []);

  const provider = providers.find((p) => p.name === selectedProvider);
  const models = provider
    ? activeTab === "image"
      ? provider.available_image_models
      : provider.available_video_models
    : [];
  const providerHasKey = provider?.has_api_key ?? false;

  const switchTab = (tab: "image" | "video") => {
    setActiveTab(tab);
    setResult(null);
    setError(null);
    if (provider) {
      setSelectedModel(tab === "image" ? provider.default_image_model : provider.default_video_model);
    }
  };

  const changeProvider = (name: string) => {
    setSelectedProvider(name);
    const p = providers.find((x) => x.name === name);
    if (p) setSelectedModel(activeTab === "image" ? p.default_image_model : p.default_video_model);
  };

  const generate = async () => {
    if (!prompt.trim() || !selectedProvider) {
      setError("Enter a prompt and pick a provider.");
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      if (activeTab === "image") {
        const res = await API.generateImage({
          prompt,
          provider: selectedProvider,
          model: selectedModel || undefined,
          width,
          height,
        });
        if (res.success && res.url) {
          setResult({ url: res.url, type: "image" });
          onMediaGenerated?.("image", res.url);
        } else {
          setError(res.error || "Image generation failed.");
        }
      } else {
        const res = await API.generateVideo({
          prompt,
          provider: selectedProvider,
          model: selectedModel || undefined,
          duration,
        });
        if (res.success && res.url) {
          // Rare: synchronous provider returned a URL directly.
          setResult({ url: res.url, type: "video" });
          onMediaGenerated?.("video", res.url);
        } else if (res.success && res.task_id) {
          setPolling(true);
          const poll = await API.pollVideo(res.task_id, res.provider || selectedProvider, true);
          if (poll.success && poll.url) {
            setResult({ url: poll.url, type: "video" });
            onMediaGenerated?.("video", poll.url);
          } else {
            setError(poll.error || "Video generation timed out. Try again.");
          }
        } else {
          setError(res.error || "Video generation failed.");
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Generation failed.");
    } finally {
      setBusy(false);
      setPolling(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-background/95 backdrop-blur">
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <span>Media Generation</span>
          <span className="text-xs font-normal text-muted-foreground">image &amp; video</span>
        </div>
        <button onClick={onClose} title="Close" className="rounded-lg p-1.5 hover:bg-white/10">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 overflow-auto p-5">
        <div className="mx-auto flex w-full max-w-lg flex-col gap-4">
          {/* Tabs */}
          <div className="flex gap-2">
            <button
              onClick={() => switchTab("image")}
              className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm ${
                activeTab === "image" ? "bg-white/10 font-medium" : "hover:bg-white/5"
              }`}
            >
              <ImageIcon className="h-4 w-4" /> Image
            </button>
            <button
              onClick={() => switchTab("video")}
              className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm ${
                activeTab === "video" ? "bg-white/10 font-medium" : "hover:bg-white/5"
              }`}
            >
              <VideoIcon className="h-4 w-4" /> Video
            </button>
          </div>

          {loadError && (
            <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
              {loadError}
            </div>
          )}

          {/* Provider + model */}
          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Provider
              <select
                value={selectedProvider}
                onChange={(e) => changeProvider(e.target.value)}
                disabled={busy}
                className="rounded-lg border border-border bg-transparent px-2 py-1.5 text-sm text-foreground"
              >
                {providers.length === 0 && <option value="">Loading…</option>}
                {providers.map((p) => (
                  <option key={p.name} value={p.name} disabled={!p.has_api_key}>
                    {PROVIDER_LABELS[p.name] || p.name}
                    {p.has_api_key ? "" : " (no API key)"}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Model
              <select
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
                disabled={busy}
                className="rounded-lg border border-border bg-transparent px-2 py-1.5 text-sm text-foreground"
              >
                {models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {!providerHasKey && selectedProvider && (
            <div className="text-xs text-amber-500">
              ⚠ No API key configured for this provider — set its *_API_KEY env var.
            </div>
          )}

          {/* Prompt */}
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Prompt
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={3}
              disabled={busy}
              placeholder={
                activeTab === "image"
                  ? "A futuristic cityscape at sunset, cyberpunk, highly detailed"
                  : "A drone shot flying over snowy mountains at dawn"
              }
              className="resize-y rounded-lg border border-border bg-transparent px-3 py-2 text-sm text-foreground"
            />
          </label>

          {/* Per-type options */}
          {activeTab === "image" ? (
            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                Width
                <input
                  type="number"
                  value={width}
                  min={256}
                  max={2048}
                  step={64}
                  disabled={busy}
                  onChange={(e) => setWidth(Number(e.target.value))}
                  className="rounded-lg border border-border bg-transparent px-2 py-1.5 text-sm text-foreground"
                />
              </label>
              <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                Height
                <input
                  type="number"
                  value={height}
                  min={256}
                  max={2048}
                  step={64}
                  disabled={busy}
                  onChange={(e) => setHeight(Number(e.target.value))}
                  className="rounded-lg border border-border bg-transparent px-2 py-1.5 text-sm text-foreground"
                />
              </label>
            </div>
          ) : (
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Duration
              <select
                value={duration}
                onChange={(e) => setDuration(e.target.value)}
                disabled={busy}
                className="w-32 rounded-lg border border-border bg-transparent px-2 py-1.5 text-sm text-foreground"
              >
                <option value="3s">3 seconds</option>
                <option value="5s">5 seconds</option>
                <option value="10s">10 seconds</option>
              </select>
            </label>
          )}

          <button
            onClick={generate}
            disabled={busy || !prompt.trim() || !providerHasKey}
            className="flex items-center justify-center gap-2 rounded-lg bg-white/10 px-4 py-2 text-sm font-medium hover:bg-white/15 disabled:opacity-40"
          >
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            {busy
              ? polling
                ? "Rendering video…"
                : "Generating…"
              : activeTab === "image"
                ? "Generate image"
                : "Generate video"}
          </button>

          {error && (
            <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
              {error}
            </div>
          )}

          {result && (
            <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
              {result.type === "image" ? (
                <img src={result.url} alt="Generated" className="rounded-lg" />
              ) : (
                <video src={result.url} controls className="rounded-lg" />
              )}
              <div className="flex gap-3 text-xs">
                <button
                  onClick={() => navigator.clipboard.writeText(result.url)}
                  className="flex items-center gap-1 hover:underline"
                >
                  <Copy className="h-3.5 w-3.5" /> Copy URL
                </button>
                <a
                  href={result.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-1 hover:underline"
                >
                  <ExternalLink className="h-3.5 w-3.5" /> Open in new tab
                </a>
              </div>
              <div className="text-xs text-muted-foreground">Added to the chat transcript.</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
