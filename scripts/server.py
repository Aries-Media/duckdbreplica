#!/usr/bin/env python3
"""Supervise the container processes for the DuckDB Quack replica.

Every process runs the same DuckDB 2.0-dev (alpha) binary. Four children are
managed for the container lifetime:

1. "owner"   (critical)   - DuckDB CLI that owns the replica database file and
   serves Quack on 0.0.0.0:QUACK_PORT with the admin token (full access, used
   by the primary to write).
2. "gateway" (critical)   - The read-only user. A second DuckDB CLI that opens
   the owner's Quack endpoint as its primary database (token provided through
   a persistent secret) and serves Quack on 0.0.0.0:QUACK_READONLY_PORT with
   the read-only token. A server-side authorization hook allows only SELECT
   statements (validated with json_serialize_sql, which rejects anything that
   is not a pure SELECT, including WITH ... INSERT tricks and multi-statement
   payloads) plus bare transaction control (BEGIN/COMMIT/ROLLBACK, needed by
   the Quack catalog protocol). Local filesystem access is disabled so
   read-only clients cannot read container files (tokens, /proc, ...).
3. "ui"      (best-effort) - DuckDB CLI with its own catalog file (the UI
   needs writable storage for notebooks) that attaches the replica through
   the read-only gateway and runs the DuckDB UI HTTP server on
   localhost:4213. The ui extension is not yet published for alpha builds;
   until it is, this process fails to start the UI and is shut down.
4. "nginx"   (best-effort) - Reverse proxy with HTTP Basic Auth on
   0.0.0.0:UI_PROXY_PORT in front of the UI (which has no auth of its own).

If a critical child exits, everything is torn down so the container restart
policy can bring the stack back in a consistent state. The UI stack is
best-effort: its failure only logs a warning and Quack keeps running.
"""
import os
import signal
import socket
import subprocess
import sys
import time

RUNTIME_DIR = "/run/quack"
UI_INTERNAL_PORT = 4213
UI_CATALOG = "/data/ui_catalog.duckdb"
SECRET_ENV_VARS = ("QUACK_TOKEN", "QUACK_READONLY_TOKEN", "UI_PASSWORD")
# The gateway caches the owner's catalog: refresh it periodically so tables
# created by the primary after gateway startup become visible to read-only
# clients. Runs in the gateway's own session, not subject to the auth hook.
GATEWAY_CACHE_REFRESH_SECONDS = 30

NGINX_CONF_TEMPLATE = """\
pid {runtime_dir}/nginx.pid;
error_log stderr warn;
events {{ worker_connections 128; }}
http {{
    access_log off;
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        ''      close;
    }}
    server {{
        listen {proxy_port};
        auth_basic "DuckDB UI";
        auth_basic_user_file {runtime_dir}/htpasswd;
        location / {{
            proxy_pass http://127.0.0.1:{ui_port};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_buffering off;
            proxy_request_buffering off;
            client_max_body_size 0;
            proxy_read_timeout 1h;
            proxy_send_timeout 1h;
        }}
    }}
}}
"""

# Read-only authorization hook. Lives in an in-memory catalog attached to the
# gateway instance so no local file is needed after filesystem access is
# disabled. json_serialize_sql only serializes pure SELECT statements: any
# write, DDL, SET, CALL, multi-statement payload or parse error yields
# "error": true and the query is rejected. Bare transaction control statements
# are whitelisted because the Quack catalog protocol wraps queries in them.
GATEWAY_SETUP = (
    "LOAD json; "
    "ATTACH ':memory:' AS m; "
    "CREATE MACRO m.main.quack_read_only(sid, q) AS ("
    "regexp_matches(trim(q::VARCHAR), '^(BEGIN|COMMIT|ROLLBACK|ABORT)( TRANSACTION)?;?\\s*$', 'i')"
    " OR coalesce(json_serialize_sql(q::VARCHAR)->>'error', 'x') = 'false'"
    "); "
    "SET GLOBAL quack_authorization_function = 'm.main.quack_read_only'; "
    "SET GLOBAL disabled_filesystems = 'LocalFileSystem'; "
    "SET GLOBAL lock_configuration = true; "
)


def sql_literal(value):
    return "'" + value.replace("'", "''") + "'"


def read_config():
    cfg = {}
    cfg["admin_token"] = os.environ.get("QUACK_TOKEN", "")
    if len(cfg["admin_token"]) < 16:
        sys.exit("QUACK_TOKEN must be set to at least 16 characters")
    cfg["readonly_token"] = os.environ.get("QUACK_READONLY_TOKEN", "")
    if len(cfg["readonly_token"]) < 16:
        sys.exit("QUACK_READONLY_TOKEN must be set to at least 16 characters")
    if cfg["readonly_token"] == cfg["admin_token"]:
        sys.exit("QUACK_READONLY_TOKEN must differ from QUACK_TOKEN")
    cfg["ui_username"] = os.environ.get("UI_USERNAME", "duckdb")
    if not cfg["ui_username"] or ":" in cfg["ui_username"]:
        sys.exit("UI_USERNAME must be non-empty and must not contain ':'")
    cfg["ui_password"] = os.environ.get("UI_PASSWORD", "")
    if len(cfg["ui_password"]) < 12:
        sys.exit("UI_PASSWORD must be set to at least 12 characters")
    cfg["db"] = os.environ.get("DUCKDB_DATABASE", "/data/replica.duckdb")
    if not cfg["db"].startswith("/data/") or not cfg["db"].endswith(".duckdb"):
        sys.exit("DUCKDB_DATABASE must be an absolute .duckdb path inside /data")
    if cfg["db"] == UI_CATALOG:
        sys.exit(f"DUCKDB_DATABASE must not be {UI_CATALOG} (reserved for the UI)")
    for name, env, default in (
        ("quack_port", "QUACK_PORT", "9494"),
        ("readonly_port", "QUACK_READONLY_PORT", "9495"),
        ("ui_proxy_port", "UI_PROXY_PORT", "8080"),
    ):
        port = int(os.environ.get(env, default))
        if not 1 <= port <= 65535:
            sys.exit(f"Invalid {env}")
        cfg[name] = port
    if len({cfg["quack_port"], cfg["readonly_port"], cfg["ui_proxy_port"], UI_INTERNAL_PORT}) != 4:
        sys.exit("QUACK_PORT, QUACK_READONLY_PORT, UI_PROXY_PORT and 4213 must all differ")
    return cfg


def child_env():
    """Environment for children that must not see any secret."""
    env = dict(os.environ)
    for key in SECRET_ENV_VARS:
        env.pop(key, None)
    return env


def write_runtime_files(cfg):
    os.makedirs(RUNTIME_DIR, mode=0o700, exist_ok=True)
    import crypt  # glibc SHA-512 crypt, supported by nginx auth_basic

    htpasswd_path = os.path.join(RUNTIME_DIR, "htpasswd")
    hashed = crypt.crypt(cfg["ui_password"], crypt.mksalt(crypt.METHOD_SHA512))
    with open(htpasswd_path, "w") as f:
        f.write(f"{cfg['ui_username']}:{hashed}\n")
    os.chmod(htpasswd_path, 0o600)

    conf_path = os.path.join(RUNTIME_DIR, "nginx.conf")
    with open(conf_path, "w") as f:
        f.write(
            NGINX_CONF_TEMPLATE.format(
                runtime_dir=RUNTIME_DIR,
                proxy_port=cfg["ui_proxy_port"],
                ui_port=UI_INTERNAL_PORT,
            )
        )
    return conf_path


def create_gateway_secret(cfg):
    """Store the admin token as a persistent quack secret, scoped to the
    owner's localhost URI, so the gateway can open it as primary database."""
    owner_uri = f"quack:localhost:{cfg['quack_port']}"
    statement = (
        "CREATE OR REPLACE PERSISTENT SECRET quack_gateway_admin ("
        "TYPE quack, TOKEN " + sql_literal(cfg["admin_token"])
        + ", SCOPE " + sql_literal(owner_uri) + ");\n"
    )
    result = subprocess.run(
        ["duckdb"], input=statement, capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit(f"Failed to create the gateway secret: {result.stderr.strip()}")


def start_duckdb(primary, statement, env):
    child = subprocess.Popen(
        ["duckdb", primary],
        stdin=subprocess.PIPE,
        stdout=sys.stdout,
        stderr=sys.stderr,
        text=True,
        bufsize=1,
        env=env,
    )
    try:
        child.stdin.write(statement)
        child.stdin.flush()
    except BrokenPipeError:
        raise RuntimeError("duckdb closed its input during startup")
    return child


def wait_for_port(port, label, timeout=90, process=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            print(f"{label}: process exited before listening on port {port}", flush=True)
            return False
        try:
            # "localhost" so both 127.0.0.1 and ::1 bindings are detected.
            with socket.create_connection(("localhost", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    print(f"Timed out waiting for {label} on port {port}", flush=True)
    return False


def main():
    cfg = read_config()
    nginx_conf = write_runtime_files(cfg)
    children = {}

    def shutdown(exit_code):
        for name in ("nginx", "ui", "gateway", "owner"):
            child = children.get(name)
            if child is not None and child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 30
        for child in children.values():
            remaining = max(0.1, deadline - time.monotonic())
            try:
                child.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                child.kill()
        sys.exit(exit_code)

    def on_signal(_signum, _frame):
        shutdown(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # 1. Owner: sole owner of the replica file, serves the admin endpoint.
    owner_statement = (
        "INSTALL quack; LOAD quack; "
        "CALL quack_serve(" + sql_literal(f"quack:0.0.0.0:{cfg['quack_port']}")
        + ", token = " + sql_literal(cfg["admin_token"])
        + ", allow_other_hostname = true);\n"
    )
    children["owner"] = start_duckdb(cfg["db"], owner_statement, dict(os.environ))
    if not wait_for_port(cfg["quack_port"], "Quack (admin)", process=children["owner"]):
        shutdown(1)
    print("Quack admin endpoint is listening", flush=True)

    # 2. Read-only gateway: opens the owner over Quack as primary database so
    #    read-only clients see the replica catalog directly.
    create_gateway_secret(cfg)
    gateway_statement = (
        GATEWAY_SETUP
        + "CALL quack_serve(" + sql_literal(f"quack:0.0.0.0:{cfg['readonly_port']}")
        + ", token = " + sql_literal(cfg["readonly_token"])
        + ", allow_other_hostname = true);\n"
    )
    children["gateway"] = start_duckdb(
        f"quack:localhost:{cfg['quack_port']}", gateway_statement, child_env()
    )
    if not wait_for_port(cfg["readonly_port"], "Quack (read-only)", process=children["gateway"]):
        shutdown(1)
    print("Quack read-only endpoint is listening", flush=True)

    # 3 + 4. UI stack, best-effort: the Quack endpoints must keep working even
    #    if the UI cannot start (the ui extension is not yet published for
    #    alpha builds). The UI reaches the replica through the read-only
    #    gateway, so UI users cannot write to the replica.
    def start_ui_stack():
        ui_statement = (
            "INSTALL quack; LOAD quack; INSTALL ui; LOAD ui; "
            f"SET ui_local_port = {UI_INTERNAL_PORT}; "
            "ATTACH " + sql_literal(f"quack:localhost:{cfg['readonly_port']}")
            + " AS replica (TOKEN " + sql_literal(cfg["readonly_token"]) + "); "
            "CALL start_ui_server();\n"
        )
        children["ui"] = start_duckdb(UI_CATALOG, ui_statement, child_env())
        if not wait_for_port(UI_INTERNAL_PORT, "DuckDB UI", timeout=60, process=children["ui"]):
            return False
        children["nginx"] = subprocess.Popen(
            ["nginx", "-c", nginx_conf, "-g", "daemon off;"],
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=child_env(),
        )
        return wait_for_port(cfg["ui_proxy_port"], "UI proxy", process=children["nginx"])

    try:
        ui_ok = start_ui_stack()
    except Exception as exc:  # the UI stack must never take Quack down
        print(f"WARNING: UI stack failed to start: {exc}", flush=True)
        ui_ok = False
    if ui_ok:
        print("Stack ready: Quack + read-only gateway + password-protected UI", flush=True)
    else:
        print("WARNING: UI stack unavailable, continuing with Quack only", flush=True)
        for name in ("nginx", "ui"):
            child = children.pop(name, None)
            if child is not None and child.poll() is None:
                child.terminate()

    next_cache_refresh = time.monotonic() + GATEWAY_CACHE_REFRESH_SECONDS
    while True:
        for name in ("owner", "gateway"):
            code = children[name].poll()
            if code is not None:
                print(f"Critical process '{name}' exited with code {code}", flush=True)
                shutdown(code if code != 0 else 1)
        for name in ("ui", "nginx"):
            child = children.get(name)
            if child is not None and child.poll() is not None:
                print(
                    f"WARNING: child '{name}' exited with code {child.poll()}; "
                    "UI may be unavailable, Quack keeps running",
                    flush=True,
                )
                children.pop(name)
        if time.monotonic() >= next_cache_refresh:
            next_cache_refresh = time.monotonic() + GATEWAY_CACHE_REFRESH_SECONDS
            try:
                children["gateway"].stdin.write("CALL quack_clear_cache();\n")
                children["gateway"].stdin.flush()
            except (BrokenPipeError, OSError):
                pass  # exit is handled by the critical-process check above
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
