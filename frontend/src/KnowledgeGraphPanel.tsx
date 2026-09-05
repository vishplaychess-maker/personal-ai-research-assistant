/**
 * KnowledgeGraphPanel — full-screen overlay visualising the knowledge graph.
 *
 * Self-contained: fetches /api/knowledge-graph on open, owns its loading /
 * error state. Rendered by App.tsx when `showGraph` is true.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { X } from "lucide-react";
import { API, type KnowledgeGraphData } from "./api";

interface Props {
  onClose: () => void;
}

export function KnowledgeGraphPanel({ onClose }: Props) {
  const [data, setData] = useState<KnowledgeGraphData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });

  useEffect(() => {
    let alive = true;
    setLoading(true);
    API.getKnowledgeGraph()
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e?.message ?? "Failed to load graph"))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    const measure = () => {
      if (wrapRef.current) {
        setSize({
          w: wrapRef.current.clientWidth,
          h: wrapRef.current.clientHeight,
        });
      }
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [loading]);

  // react-force-graph mutates the objects it's given (adds x/y/vx/vy); hand it
  // a fresh deep-ish copy so a re-render doesn't fight the simulation.
  const graphData = useMemo(
    () => ({
      nodes: (data?.nodes ?? []).map((n) => ({ ...n })),
      links: (data?.links ?? []).map((l) => ({ ...l })),
    }),
    [data]
  );

  const isEmpty = !loading && !error && graphData.nodes.length === 0;

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-background/95 backdrop-blur">
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <div className="flex items-baseline gap-3">
          <span className="text-sm font-semibold">🕸️ Knowledge Graph</span>
          {data && (
            <span className="text-xs text-muted-foreground">
              {graphData.nodes.length} entities · {graphData.links.length} relations
            </span>
          )}
        </div>
        <button
          onClick={onClose}
          title="Close"
          className="rounded-lg p-1.5 transition-colors hover:bg-white/10"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div ref={wrapRef} className="relative flex-1 overflow-hidden">
        {loading && (
          <p className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
            Loading graph…
          </p>
        )}
        {error && (
          <p className="absolute inset-0 grid place-items-center text-sm text-destructive">
            {error}
          </p>
        )}
        {isEmpty && (
          <p className="absolute inset-0 grid place-items-center px-8 text-center text-sm text-muted-foreground">
            The graph is empty. It fills in as you upload documents, run deep
            research, or ask the agent to extract entities.
          </p>
        )}
        {!loading && !error && graphData.nodes.length > 0 && (
          <ForceGraph2D
            graphData={graphData}
            width={size.w}
            height={size.h}
            nodeLabel={(n: any) => `${n.name} (${n.type})`}
            nodeAutoColorBy="type"
            linkLabel={(l: any) => l.relation}
            linkDirectionalArrowLength={3}
            linkDirectionalArrowRelPos={1}
            linkWidth={(l: any) => Math.min(1 + (l.weight ?? 1) * 0.4, 4)}
            cooldownTicks={120}
            nodeCanvasObjectMode={() => "after"}
            nodeCanvasObject={(node: any, ctx, globalScale) => {
              const label = node.name as string;
              const fontSize = 12 / globalScale;
              ctx.font = `${fontSize}px sans-serif`;
              ctx.textAlign = "center";
              ctx.textBaseline = "top";
              ctx.fillStyle = "rgba(200,200,200,0.9)";
              ctx.fillText(label, node.x, node.y + 5);
            }}
          />
        )}
      </div>
    </div>
  );
}
