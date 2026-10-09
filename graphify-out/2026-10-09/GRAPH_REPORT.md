# Graph Report - duckdb-quack-replica  (2026-10-08)

## Corpus Check
- 2 files · ~2,626 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 4 file(s) not represented in the graph (top: (none) 3, .example 1)

## Summary
- 29 nodes · 40 edges · 4 communities (3 shown, 1 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 1 edges (avg confidence: 0.85)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- server.py
- main
- DuckDB 2.0-dev Quack receiver for Coolify
- create_gateway_secret

## God Nodes (most connected - your core abstractions)
1. `main()` - 11 edges
2. `DuckDB 2.0-dev Quack receiver for Coolify` - 7 edges
3. `start_ui_stack()` - 5 edges
4. `sql_literal()` - 4 edges
5. `child_env()` - 4 edges
6. `create_gateway_secret()` - 4 edges
7. `start_duckdb()` - 3 edges
8. `wait_for_port()` - 3 edges
9. `read_config()` - 2 edges
10. `write_runtime_files()` - 2 edges

## Surprising Connections (you probably didn't know these)
- `create_gateway_secret()` --calls--> `sql_literal()`  [EXTRACTED]
  scripts/server.py → scripts/server.py  _Bridges community 1 → community 3_
- `main()` --calls--> `read_config()`  [EXTRACTED]
  scripts/server.py → scripts/server.py  _Bridges community 0 → community 1_

## Import Cycles
- None detected.

## Communities (4 total, 1 thin omitted)

### Community 0 - "server.py"
Cohesion: 0.20
Nodes (9): os, Supervise the container processes for the DuckDB Quack replica. Every process…, read_config(), write_runtime_files(), signal, socket, subprocess, sys (+1 more)

### Community 1 - "main"
Cohesion: 0.36
Nodes (9): child_env(), main(), on_signal(), shutdown(), start_ui_stack(), Environment for children that must not see any secret., sql_literal(), start_duckdb() (+1 more)

### Community 2 - "DuckDB 2.0-dev Quack receiver for Coolify"
Cohesion: 0.25
Nodes (7): Architecture, Coolify deployment, DuckDB 2.0-dev Quack receiver for Coolify, DuckDB UI (read-only access), Important limitations, Primary-to-secondary example (manual copy, not streaming replication), Read-only user

## Knowledge Gaps
- **6 isolated node(s):** `Architecture`, `Read-only user`, `Coolify deployment`, `DuckDB UI (read-only access)`, `Primary-to-secondary example (manual copy, not streaming replication)` (+1 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 16 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `main` to `server.py`, `create_gateway_secret`?**
  _High betweenness centrality (0.146) - this node is a cross-community bridge._
- **Why does `child_env()` connect `main` to `server.py`?**
  _High betweenness centrality (0.054) - this node is a cross-community bridge._
- **What connects `Architecture`, `Read-only user`, `Coolify deployment` to the rest of the system?**
  _6 weakly-connected nodes found - possible documentation gaps or missing edges._