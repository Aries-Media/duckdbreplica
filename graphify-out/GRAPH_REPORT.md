# Graph Report - duckdb-quack-replica  (2026-10-09)

## Corpus Check
- 2 files · ~2,533 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 4 file(s) not represented in the graph (top: (none) 3, .example 1)

## Summary
- 31 nodes · 42 edges · 4 communities
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 1 edges (avg confidence: 0.85)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `127648bb`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- server.py
- main
- DuckDB Quack receiver for Coolify
- start_ui_stack

## God Nodes (most connected - your core abstractions)
1. `main()` - 10 edges
2. `DuckDB Quack receiver for Coolify` - 7 edges
3. `start_ui_stack()` - 6 edges
4. `sql_literal()` - 4 edges
5. `authz_macro()` - 4 edges
6. `child_env()` - 3 edges
7. `start_duckdb()` - 3 edges
8. `wait_for_port()` - 3 edges
9. `read_config()` - 2 edges
10. `write_runtime_files()` - 2 edges

## Surprising Connections (you probably didn't know these)
- `start_ui_stack()` --calls--> `sql_literal()`  [EXTRACTED]
  scripts/server.py → scripts/server.py  _Bridges community 1 → community 3_

## Import Cycles
- None detected.

## Communities (4 total, 0 thin omitted)

### Community 0 - "server.py"
Cohesion: 0.22
Nodes (8): os, Supervise the container processes for the DuckDB Quack replica. Every process…, shutil, signal, socket, subprocess, sys, time

### Community 1 - "main"
Cohesion: 0.32
Nodes (8): authz_macro(), main(), on_signal(), shutdown(), Authorization hook, persisted in the replica catalog so the transient server-…, read_config(), sql_literal(), write_runtime_files()

### Community 2 - "DuckDB Quack receiver for Coolify"
Cohesion: 0.25
Nodes (7): Architecture, Coolify deployment, DuckDB Quack receiver for Coolify, DuckDB UI (read-only access), Important limitations, Primary-to-secondary example (manual copy, not streaming replication), Read-only user

### Community 3 - "start_ui_stack"
Cohesion: 0.33
Nodes (6): child_env(), start_ui_stack(), prepare_ui_dir(), Environment for children that must not see any secret., start_duckdb(), wait_for_port()

## Knowledge Gaps
- **6 isolated node(s):** `Architecture`, `Coolify deployment`, `Primary-to-secondary example (manual copy, not streaming replication)`, `Read-only user`, `DuckDB UI (read-only access)` (+1 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 17 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `main` to `server.py`, `start_ui_stack`?**
  _High betweenness centrality (0.122) - this node is a cross-community bridge._
- **Why does `child_env()` connect `start_ui_stack` to `server.py`?**
  _High betweenness centrality (0.052) - this node is a cross-community bridge._
- **Why does `authz_macro()` connect `main` to `server.py`?**
  _High betweenness centrality (0.048) - this node is a cross-community bridge._
- **What connects `Architecture`, `Coolify deployment`, `Primary-to-secondary example (manual copy, not streaming replication)` to the rest of the system?**
  _6 weakly-connected nodes found - possible documentation gaps or missing edges._