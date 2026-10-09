# DuckDB Quack receiver for Coolify

A Docker Compose deployment of DuckDB (pinned, see `DUCKDB_VERSION` in the Dockerfile — currently **v1.5.6**) with Quack listening on port 9494, a server-enforced read-only endpoint on port 9495, and the password-protected DuckDB UI on port 8080. Designed as a **secondary data destination**: the primary must connect and explicitly copy/merge data. **Quack does not automatically replicate data.**

## Architecture

One container, one pinned DuckDB build for everything, three supervised processes:

1. **Owner** (critical, `duckdb /data/replica.duckdb`): the sole owner of the replica file. Serves two Quack endpoints: `0.0.0.0:9494` with `QUACK_TOKEN` (admin, full access — used by the primary to write) and `0.0.0.0:9495` with `QUACK_READONLY_TOKEN` (the **read-only user**). A server-side authorization hook tells the two endpoints apart via `quack_active_connections()`: admin connections may run anything; read-only connections may only run pure `SELECT` statements — validated by parsing with `json_serialize_sql`, which rejects DDL/DML, `WITH ... INSERT` tricks and multi-statement payloads — plus the bare `BEGIN`/`COMMIT`/`ROLLBACK` used by the Quack catalog protocol. Functions touching the local filesystem, the environment or dynamic/remote SQL (`read_text`, `read_csv`, `glob`, `getenv`, `query`, `quack_query`, ...) are denied to read-only connections. Enforcement is **server-side**: it does not rely on any client option.
2. **UI** (best-effort, `duckdb /data/ui/ui_catalog.duckdb`, runs as the unprivileged `nobody` user): a separate DuckDB process with its own catalog (the UI needs writable storage for notebooks). It attaches the replica **through the read-only endpoint**, so everything seen in the UI is read-only towards the replica, and runs `start_ui_server()` on `localhost:4213`. Running unprivileged means UI users cannot read root-owned container files (tokens, password hashes). Its view of the replica catalog is refreshed every 60 seconds so new tables become visible.
3. **nginx** (best-effort): HTTP Basic Auth (`UI_USERNAME` / `UI_PASSWORD`) on port 8080 in front of the UI, which has no authentication of its own.

The UI stack is **best-effort**: if it fails to start or crashes, the container logs a warning and keeps serving Quack. The healthcheck monitors ports 9494 and 9495.

## Coolify deployment

1. Upload all files to a GitHub repository (keep the structure intact).
2. In Coolify, create a new Resource > Public/Private Git Repository > Docker Compose. Set compose path to `docker-compose.yml`.
3. In Coolify Environment Variables, create `QUACK_TOKEN`, `QUACK_READONLY_TOKEN` (two different long random secrets, 32+ characters) and `UI_PASSWORD` (12+ characters), all marked as secret. Optionally set `UI_USERNAME` (default `duckdb`). Deploy.
4. **Expose services with HTTPS domains, not the server IP** (the Coolify proxy terminates TLS with automatic Let's Encrypt certificates; no extra ports need to be opened):
   - Write endpoint: `https://replicadb.yourdomain.com:9494`
   - Read-only endpoint: `https://replicadb-ro.yourdomain.com:9495`
   - UI: `https://replicadb-ui.yourdomain.com:8080`
   The `:port` suffix in the Domains field tells Coolify which container port to route to; point the DNS A records to the server. Do not publish ports directly: Quack tokens and Basic Auth credentials must only travel over TLS.
5. Keep the named volume `duckdb_replica_data` persistent across deployments.
6. Check container logs and the health status before sending writes.

## Primary-to-secondary example (manual copy, not streaming replication)

From a DuckDB CLI (same pinned version) on the primary machine:

```sql
INSTALL quack;
LOAD quack;
ATTACH 'quack:replicadb.yourdomain.com:443' AS secondary (TOKEN 'YOUR_QUACK_TOKEN');
-- Example only: the target table must be managed by your sync workflow.
CREATE TABLE secondary.main.orders AS SELECT * FROM main.orders;
```

The example is a **one-time copy** and fails if the table already exists. Implement initial snapshot + subsequent idempotent upserts or atomic refresh, scheduling, failure retries and conflict handling on the primary side.

## Read-only user

Any DuckDB client with Quack can connect to the read-only endpoint with `QUACK_READONLY_TOKEN` and gets SELECT-only access to the replica:

```sql
INSTALL quack;
LOAD quack;
ATTACH 'quack:replicadb-ro.yourdomain.com:443' AS replica_ro (TOKEN 'YOUR_QUACK_READONLY_TOKEN');
SELECT * FROM replica_ro.main.orders LIMIT 100;   -- works
INSERT INTO replica_ro.main.orders VALUES (...);  -- Authorization failed
```

Writes, DDL, `SET`, `CALL` and multi-statement payloads are rejected server-side by the gateway's authorization hook, regardless of client flags.

## DuckDB UI (read-only access)

Open `https://replicadb-ui.yourdomain.com` and log in with `UI_USERNAME` / `UI_PASSWORD`. The replica data is attached through the read-only gateway as the `replica` catalog:

```sql
SELECT * FROM replica.main.orders LIMIT 100;
```

Notes:

- Writes to `replica.*` fail: they are rejected server-side by the read-only endpoint. Notebooks and scratch tables live in the UI's own catalog (`/data/ui/ui_catalog.duckdb`), which does not touch the replica.
- Anyone with the UI password can run arbitrary SQL in the UI process (read-only towards the replica, but with local access inside the container). Treat UI credentials as sensitive, share them only with semi-trusted users, and always serve the UI over HTTPS.
- The UI fetches its static assets from `https://ui.duckdb.org` at runtime, so the container needs outbound internet access.

## Important limitations

- **No built-in replication**: this repository starts a Quack server; it does not read WALs, subscribe to primary changes, or create a replication job.
- **Admin endpoint is not read-only**: the primary needs to write into this receiver. The `QUACK_TOKEN` endpoint (9494) authorizes queries, including writes; restrict network access and token distribution. Read-only access goes through the gateway on 9495.
- **Pinned version**: `DUCKDB_VERSION` is pinned in the Dockerfile. Keep clients on the same version and bump it deliberately after validating.
- **Single owner**: do not mount/open the same database file from a second writer process. The UI never opens the replica file; it goes through Quack.
- **Storage format**: a replica file written by a different DuckDB major/preview version (e.g. the previous 2.0-dev alpha) cannot be opened by v1.5.6. When switching versions, wipe the `duckdb_replica_data` volume (or the `.duckdb` files in it) and re-copy from the primary.
- **No direct PostgreSQL compatibility**: clients need DuckDB with Quack; standard PostgreSQL drivers do not work.
- **Healthcheck** only verifies the TCP listeners (9494 and 9495), not query execution or replication freshness.
- **Security**: tokens and transport must be secured. Always front Quack and the UI with TLS (the Coolify proxy does this) when crossing untrusted networks.

Docs: https://duckdb.org/docs/stable/quack/overview, https://duckdb.org/docs/stable/quack/security and https://duckdb.org/docs/stable/core_extensions/ui
