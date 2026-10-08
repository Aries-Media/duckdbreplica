# DuckDB 2.0-dev Quack receiver for Coolify

A Docker Compose deployment of DuckDB 2.0-dev with Quack listening on port 9494, plus the password-protected DuckDB UI on port 8080. Designed as a **secondary data destination**: the primary must connect and explicitly copy/merge data. **Quack does not automatically replicate data.**

## Architecture

One container, one DuckDB build (2.0-dev alpha) for everything, four supervised processes:

1. **Owner** (critical, `duckdb /data/replica.duckdb`): the sole owner of the replica file. Serves Quack on `0.0.0.0:9494` with `QUACK_TOKEN` (admin, full access — used by the primary to write).
2. **Read-only gateway** (critical): this is the **read-only user**. A second DuckDB process that opens the owner's endpoint as its primary database (`duckdb quack:localhost:9494`, admin token provided via a scoped persistent secret) and serves Quack on `0.0.0.0:9495` with `QUACK_READONLY_TOKEN`. A server-side authorization hook only admits pure `SELECT` statements — validated by parsing with `json_serialize_sql`, which rejects DDL/DML, `WITH ... INSERT` tricks and multi-statement payloads — plus the bare `BEGIN`/`COMMIT`/`ROLLBACK` used by the Quack catalog protocol. Local filesystem access is disabled in this process, so read-only clients cannot read container files. Enforcement is **server-side**: it does not rely on any client option.
3. **UI** (best-effort, `duckdb /data/ui_catalog.duckdb`): a separate DuckDB process with its own catalog (the UI needs writable storage for notebooks). It attaches the replica **through the read-only gateway**, so everything seen in the UI is read-only towards the replica, and runs `start_ui_server()` on `localhost:4213`.
4. **nginx** (best-effort): HTTP Basic Auth (`UI_USERNAME` / `UI_PASSWORD`) on port 8080 in front of the UI, which has no authentication of its own.

The UI stack is **best-effort**: if it fails to start or crashes, the container logs a warning and keeps serving Quack. The healthcheck monitors ports 9494 and 9495.

> **Current UI status**: the `ui` extension is not yet published for DuckDB 2.0-dev alpha builds (404 from the extension repository for every platform). Until upstream publishes it, the UI process logs a warning and shuts down, and the container serves the two Quack endpoints only. No version downgrade is performed: everything runs exclusively on the 2.0-dev alpha. Once the extension becomes available for the alpha, the password-protected UI starts working on rebuild/restart with no code changes.

## Read-only user

Any DuckDB client with Quack can connect to port 9495 with `QUACK_READONLY_TOKEN` and gets SELECT-only access to the replica:

```sql
INSTALL quack;
LOAD quack;
ATTACH 'quack:REPLICA_HOST:9495' AS replica_ro (TOKEN 'YOUR_QUACK_READONLY_TOKEN');
SELECT * FROM replica_ro.main.orders LIMIT 100;   -- works
INSERT INTO replica_ro.main.orders VALUES (...);  -- Authorization failed
```

Writes, DDL, `SET`, `CALL` and multi-statement payloads are rejected server-side by the gateway's authorization hook, regardless of client flags. Keep 9495 unpublished (Docker network only) unless external read-only clients need it; in that case expose it through an HTTPS domain like the other ports.

## Coolify deployment

1. Upload all files to a GitHub repository (keep the structure intact).
2. In Coolify, create a new Resource > Public/Private Git Repository > Docker Compose. Set compose path to `docker-compose.yml`.
3. In Coolify Environment Variables, create `QUACK_TOKEN`, `QUACK_READONLY_TOKEN` (two different long random secrets, 32+ characters) and `UI_PASSWORD` (12+ characters), all marked as secret. Optionally set `UI_USERNAME` (default `duckdb`). Deploy.
4. **Expose the UI with an HTTPS domain, not the server IP**: in the service settings, set the domain as `https://duckdb-ui.yourdomain.com:8080` (the `:8080` tells Coolify which container port to route to), and point the DNS A record of that domain to the server. Coolify's proxy obtains the Let's Encrypt certificate automatically. Do not publish port 8080 directly: Basic Auth credentials must only travel over TLS.
5. Quack (9494) listens on port 9494 **inside the Docker network only** by default. For remote primaries, connect via a private Docker network, VPN/Tailscale, or assign a second HTTPS domain in Coolify (`https://duckdb-quack.yourdomain.com:9494`): the Quack client assumes HTTPS for non-local URIs, so it works out of the box. Quack itself does not provide TLS termination.
6. Keep the named volume `duckdb_replica_data` persistent across deployments.
7. Check container logs and the health status before sending writes.

## DuckDB UI (read-only access)

Once the `ui` extension is available for the alpha, open `https://duckdb-ui.yourdomain.com` and log in with `UI_USERNAME` / `UI_PASSWORD`. The replica data is attached through the read-only gateway as the `replica` catalog:

```sql
SELECT * FROM replica.main.orders LIMIT 100;
```

Notes:

- Writes to `replica.*` fail: they are rejected server-side by the read-only gateway. Notebooks and scratch tables live in the UI's own catalog (`/data/ui_catalog.duckdb`), which does not touch the replica.
- Anyone with the UI password can run arbitrary SQL in the UI process (read-only towards the replica, but with local access inside the container). Treat UI credentials as sensitive, share them only with semi-trusted users, and always serve the UI over HTTPS.
- The UI fetches its static assets from `https://ui.duckdb.org` at runtime, so the container needs outbound internet access.

## Primary-to-secondary example (manual copy, not streaming replication)

From a compatible DuckDB CLI on the primary machine, connect to the secondary via a reachable Quack endpoint:

```sql
INSTALL quack;
LOAD quack;
ATTACH 'quack:REPLICA_HOST:9494' AS secondary (TOKEN 'YOUR_QUACK_TOKEN');
-- Example only: the target table must be managed by your sync workflow.
CREATE TABLE secondary.main.orders AS SELECT * FROM main.orders;
```

The example is a **one-time copy** and fails if the table already exists. Implement initial snapshot + subsequent idempotent upserts or atomic refresh, scheduling, failure retries and conflict handling on the primary side. For large datasets, validate schema/transaction behavior on the selected alpha build.

## Important limitations

- **No built-in replication**: this repository starts a Quack server; it does not read WALs, subscribe to primary changes, or create a replication job.
- **Admin endpoint is not read-only**: the primary needs to write into this receiver. The `QUACK_TOKEN` endpoint (9494) authorizes queries, including writes; restrict network access and token distribution. Read-only access goes through the gateway on 9495.
- **Alpha build**: `DUCKDB_VERSION=alpha` resolves at image build time. Rebuilds can change the exact version. Record the version from build logs and pin a specific build before production use.
- **Single owner**: do not mount/open the same database file from a second writer process. The UI process never opens the replica file; it goes through Quack.
- **No direct PostgreSQL compatibility**: clients need DuckDB with Quack; standard PostgreSQL drivers do not work.
- **Healthcheck** only verifies the TCP listeners (9494 and 9495), not query execution or replication freshness.
- **Security**: tokens and transport must be secured. Put TLS in front of Quack and of the UI when crossing untrusted networks.

Docs: https://duckdb.org/docs/preview/quack/overview, https://duckdb.org/docs/current/quack/security and https://duckdb.org/docs/preview/core_extensions/ui
