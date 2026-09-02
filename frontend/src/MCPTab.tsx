import { useState, useEffect, useCallback } from "react";
import { API } from "./api";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import { cn } from "./lib/utils";
import { Plug, Trash2, Search, Power, ChevronDown, ChevronUp, Wrench, AlertCircle, CheckCircle, Loader2, Plus } from "lucide-react";

interface MCPServer {
  id: number;
  name: string;
  command: string;
  args: string[];
  env?: Record<string,string> | null;
  enabled: boolean;
  tool_allowlist?: string[] | null;
  tools: Array<{name:string; description:string; inputSchema:any}>;
  last_discovered_at?: string | null;
  last_error?: string | null;
  created_at: string;
}

const selectClass = "flex h-9 w-full rounded-md border bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

export function MCPTab() {
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string|null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [discovering, setDiscovering] = useState<number|null>(null);
  const [showForm, setShowForm] = useState(true);

  // form
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"stdio"|"sse">("stdio");
  const [command, setCommand] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async()=>{
    setLoading(true);
    try {
      const res = await fetch("/api/mcp/servers", {credentials:"include", headers: {Authorization: `Bearer ${localStorage.getItem("access_token")||""}`}});
      // use API helper for auth/csrf
      const data = await API.listMcpServers();
      setServers(data as any);
    } catch(e:any){ setError(e.message); }
    finally{ setLoading(false); }
  },[]);

  useEffect(()=>{ load(); },[load]);

  const handleCreate = async()=>{
    if(!name.trim() || !command.trim()){ setError("Name and Server URL/Command required"); return; }
    setSaving(true); setError(null);
    try{
      // transport handling: for sse, store uri in command; for stdio, split command
      let payload: any;
      if(transport==="sse"){
        payload = {name: name.trim(), command: command.trim(), args: [], enabled:true};
      } else {
        const parts = command.trim().split(/\s+/);
        payload = {name: name.trim(), command: parts[0], args: parts.slice(1), enabled:true};
      }
      await API.createMcpServer(payload);
      setName(""); setCommand("");
      await load();
    }catch(e:any){ setError(e.message); }
    finally{ setSaving(false); }
  };

  const toggleEnabled = async(s: MCPServer)=>{
    try{ await API.updateMcpServer(s.id, {enabled: !s.enabled}); await load(); }catch(e:any){ setError(e.message); }
  };
  const handleDelete = async(id:number)=>{
    if(!confirm("Delete MCP server?")) return;
    try{ await API.deleteMcpServer(id); await load(); }catch(e:any){ setError(e.message); }
  };
  const handleDiscover = async(id:number)=>{
    setDiscovering(id); setError(null);
    try{ await API.discoverMcpServer(id); await load(); }catch(e:any){ setError(e.message); }
    finally{ setDiscovering(null); }
  };

  return (
    <div className="flex flex-col gap-4 overflow-y-visible">
      <div className="rounded-xl border border-white/10 bg-white/[0.02] p-4">
        <button onClick={()=>setShowForm(!showForm)} className="w-full flex items-center justify-between">
          <h3 className="font-medium text-sm flex items-center gap-2"><Plug className="h-4 w-4 text-primary"/> Register MCP Server</h3>
          {showForm ? <ChevronUp className="h-4 w-4 text-muted-foreground"/> : <ChevronDown className="h-4 w-4 text-muted-foreground"/>}
        </button>
        {showForm && (
        <div className="grid gap-3 mt-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label>Server Name</Label>
              <Input placeholder="my_tools" value={name} onChange={e=>setName(e.target.value)} />
              <p className="text-[10px] text-muted-foreground">lowercase, digits, underscores, max 40</p>
            </div>
            <div className="space-y-1">
              <Label>Transport Type</Label>
              <select value={transport} onChange={e=>setTransport(e.target.value as any)} className={selectClass}>
                <option value="stdio">stdio / command</option>
                <option value="sse">SSE</option>
              </select>
            </div>
          </div>
          <div className="space-y-1">
            <Label>{transport==="sse" ? "Server URL" : "Server Command"}</Label>
            <Input placeholder={transport==="sse" ? "http://localhost:3000/sse" : "python -m mcp_server_fetch"} value={command} onChange={e=>setCommand(e.target.value)} />
          </div>
          <Button onClick={handleCreate} disabled={saving} className="w-full">
            {saving ? <Loader2 className="h-4 w-4 animate-spin"/> : <Plus className="h-4 w-4"/>} Add Server
          </Button>
        </div>
        )}
      </div>

      {error && <div className="rounded-lg bg-destructive/10 border border-destructive/20 p-2 text-xs text-destructive flex items-center gap-2"><AlertCircle className="h-4 w-4"/>{error}</div>}

      <div className="space-y-3 max-h-[360px] overflow-y-auto pr-1 scrollbar-thin scrollbar-thumb-white/10 scrollbar-track-transparent hover:scrollbar-thumb-white/20">
        {loading ? <p className="py-6 text-center text-sm text-muted-foreground">Loading...</p> : servers.length===0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">No MCP servers yet. Add one above.</p>
        ) : servers.map(s=>(
          <div key={s.id} className="rounded-xl border border-white/10 bg-white/[0.02] p-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-sm truncate">{s.name}</span>
                  <span className={cn("px-1.5 py-0.5 rounded-full text-[10px] font-medium", s.enabled ? "bg-green-500/15 text-green-400" : "bg-white/10 text-muted-foreground")}>{s.enabled ? "Enabled" : "Disabled"}</span>
                  {s.last_error ? (
                    <span className="inline-flex items-center gap-1 text-xs text-destructive"><AlertCircle className="h-3 w-3"/>Error</span>
                  ) : s.tools?.length ? (
                    <span className="inline-flex items-center gap-1 text-xs text-green-400"><CheckCircle className="h-3 w-3"/>Connected</span>
                  ) : (
                    <span className="text-xs text-muted-foreground">Not discovered</span>
                  )}
                </div>
                <div className="text-xs text-muted-foreground truncate mt-1">{s.command} {s.args?.join(" ")}</div>
                <div className="text-[11px] text-muted-foreground mt-1 flex items-center gap-3">
                  {s.tools?.length ? <span>Discovered {s.tools.length} tools</span> : null}
                  {s.last_discovered_at && <span>• {new Date(s.last_discovered_at).toLocaleString()}</span>}
                </div>
                {s.last_error && <div className="mt-1 text-xs text-destructive bg-destructive/10 rounded px-2 py-1 truncate">{s.last_error}</div>}
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <Button size="sm" variant="outline" onClick={()=>handleDiscover(s.id)} disabled={!!discovering}>
                  {discovering===s.id ? <Loader2 className="h-3 w-3 animate-spin"/> : <Search className="h-3 w-3"/>} Discover Tools
                </Button>
                <Button size="icon" variant="ghost" onClick={()=>toggleEnabled(s)} title={s.enabled?"Disable":"Enable"}><Power className={cn("h-4 w-4", s.enabled?"text-green-400":"text-muted-foreground")}/></Button>
                <Button size="icon" variant="ghost" onClick={()=>handleDelete(s.id)}><Trash2 className="h-4 w-4"/></Button>
                <Button size="icon" variant="ghost" onClick={()=>setExpanded(prev=>{const n=new Set(prev); if(n.has(s.id)) n.delete(s.id); else n.add(s.id); return n;})}>
                  {expanded.has(s.id) ? <ChevronUp className="h-4 w-4"/> : <ChevronDown className="h-4 w-4"/>}
                </Button>
              </div>
            </div>
            {expanded.has(s.id) && (
              <div className="mt-3 border-t border-white/10 pt-3 space-y-2">
                {s.tools?.length ? s.tools.map((t:any)=>(
                  <div key={t.name} className="rounded-lg bg-white/5 border border-white/5 p-2">
                    <div className="flex items-center gap-2"><Wrench className="h-3 w-3 text-primary"/><span className="font-mono text-xs font-medium">{t.name}</span></div>
                    {t.description && <p className="text-xs text-muted-foreground mt-1">{t.description}</p>}
                    <pre className="mt-1 text-[10px] bg-black/30 rounded p-1.5 overflow-x-auto">{JSON.stringify(t.inputSchema, null, 2)}</pre>
                  </div>
                )) : <p className="text-xs text-muted-foreground">No tools discovered. Click Discover Tools.</p>}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
