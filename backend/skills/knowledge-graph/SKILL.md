---
name: knowledge-graph
description: Query the knowledge graph for entities and relationships related to a topic.
pinned: false
---
# Knowledge Graph

## When to use
When the user asks what is related to something, how two things connect, or
for the entities/relationships known about a topic — e.g. "what's related to
X?", "how does X connect to Y?", "what do we know about X?".

## How to query
Emit a marker on its own line:

    [KG_QUERY: <term>]

The system replaces it with the matching subgraph (entities and
`A --[relation]--> B` edges) injected into your context. Then answer the
user from that subgraph.

## Notes
- Use one `[KG_QUERY: ...]` per distinct term; keep the term short (an entity
  name or keyword), not a whole sentence.
- If the subgraph comes back empty, say so plainly — the graph only knows
  what earlier conversations, documents and research runs put into it.
