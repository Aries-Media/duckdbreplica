#!/usr/bin/env python3
"""Supervise the container processes for the DuckDB Quack replica.

Every process runs the same pinned DuckDB binary (see DUCKDB_VERSION in the
Dockerfile). Three children are managed for the container lifetime:

1. "owner" (critical) - DuckDB CLI that owns the replica database file and
   serves two Quack endpoints:
     - 0.0.0.0:QUACK_PORT with the admin token (full access, used by the
       primary to write);
     - 0.0.0.0:QUACK_READONLY_PORT with the read-only token (the read-only
       user).
   A server-side authorization hook distinguishes the two endpoints through
   quack_active_connections(): connections on the admin endpoint may run
   anything; connections on the read-only endpoint may only run pure SELECT
   statements — validated by parsing with json_serialize_sql, which rejects
   DDL/DML, WITH ... INSERT tricks and multi-statement payloads — plus the
   bare BEGIN/COMMIT/ROLLBACK used by the Quack catalog protocol. Functions
   that touch the local filesystem or environment (read_text, read_csv, glob,
   getenv, dynamic query(), quack_query, ...) are denied to read-only
   connections so they cannot exfiltrate container files or tokens.
2. "ui" (best-effort) - DuckDB CLI running as the unprivileged "nobody" user
   with its own catalog file (the UI needs writable storage for notebooks).
   It attaches the replica through the read-only endpoint and runs the DuckDB
   UI HTTP server on localhost:4213. Running unprivileged means UI users
   cannot read root-owned files (tokens in /proc, htpasswd, ...).
3. "nginx" (best-effort) - Reverse proxy with HTTP Basic Auth on
   0.0.0.0:UI_PROXY_PORT in front of the UI (which has no auth of its own).

If the owner exits, everything is torn down so the container restart policy
can bring the stack back in a consistent state. The UI stack is best-effort:
its failure only logs a warning and Quack keeps running.
"""
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

RUNTIME_DIR = "/run/quack"
UI_INTERNAL_PORT = 4213
UI_DIR = "/data/ui"
UI_CATALOG = os.path.join(UI_DIR, "ui_catalog.duckdb")
UI_HOME = "/var/lib/duckdb-ui"  # holds the preinstalled extensions (Dockerfile)
UI_USER = "nobody"
UI_GROUP = "nogroup"
SECRET_ENV_VARS = ("QUACK_TOKEN", "QUACK_READONLY_TOKEN", "UI_PASSWORD")
# Refresh the UI process's cached view of the replica catalog so tables
# created by the primary become visible without reconnecting. Runs in the UI
# session's own stdin, not subject to the authorization hook.
UI_CACHE_REFRESH_SECONDS = 60

NGINX_USER = "www-data"  # workers must read the htpasswd; UI user must not

NGINX_CONF_TEMPLATE = """\
user {nginx_user};
pid {runtime_dir}/nginx.pid;
error_log stderr warn;
events {{ worker_connections 128; }}
http {{
    access_log off;
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        ''      close;
    }}
    # The UI may bind the IPv6 or the IPv4 loopback depending on the build.
    upstream duckdb_ui {{
        server [::1]:{ui_port};
        server 127.0.0.1:{ui_port} backup;
    }}
    server {{
        listen {proxy_port};
        auth_basic "DuckDB UI";
        auth_basic_user_file {runtime_dir}/htpasswd;
        location / {{
            proxy_pass http://duckdb_ui;
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

# Functions denied to read-only connections (matched case-insensitively
# against the parsed statement). Everything reading local files, environment
# or executing dynamic/remote SQL.
READONLY_BLOCKED_FUNCTIONS = (
    "read_[a-z0-9_]*|[a-z0-9_]*_scan|scan[a-z0-9_]*|glob|getenv|query|"
    "query_table|sniff[a-z0-9_]*|quack_[a-z0-9_]*|load[a-z0-9_]*|install[a-z0-9_]*"
)


def sql_literal(value):
    return "'" + value.replace("'", "''") + "'"


def authz_macro(admin_server_uri):
    """Authorization hook, persisted in the replica catalog so the transient
    server-side connections (default catalog = replica) can resolve it."""
    return (
        "CREATE OR REPLACE MACRO quack_authz(sid, q) AS ("
        "EXISTS (SELECT 1 FROM quack_active_connections() c"
        " WHERE c.connection_id = sid AND c.server_id = " + sql_literal(admin_server_uri) + ")"
        " OR regexp_matches(trim(q::VARCHAR),"
        " '^(BEGIN|COMMIT|ROLLBACK|ABORT)( TRANSACTION)?;?\\s*$', 'i')"
        " OR (coalesce(json_serialize_sql(q::VARCHAR)->>'error', 'x') = 'false'"
        " AND NOT regexp_matches(json_serialize_sql(q::VARCHAR)::VARCHAR,"
        " '\"function_name\":\"(" + READONLY_BLOCKED_FUNCTIONS + ")\"', 'i'))"
        "); "
    )


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
    if cfg["db"].startswith(UI_DIR + "/"):
        sys.exit(f"DUCKDB_DATABASE must not live under {UI_DIR} (reserved for the UI)")
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


def child_env(extra=None):
    """Environment for children that must not see any secret."""
    env = dict(os.environ)
    for key in SECRET_ENV_VARS:
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def write_runtime_files(cfg):
    os.makedirs(RUNTIME_DIR, mode=0o750, exist_ok=True)
    shutil.chown(RUNTIME_DIR, user="root", group=NGINX_USER)
    os.chmod(RUNTIME_DIR, 0o750)
    import crypt  # glibc SHA-512 crypt, supported by nginx auth_basic

    htpasswd_path = os.path.join(RUNTIME_DIR, "htpasswd")
    hashed = crypt.crypt(cfg["ui_password"], crypt.mksalt(crypt.METHOD_SHA512))
    with open(htpasswd_path, "w") as f:
        f.write(f"{cfg['ui_username']}:{hashed}\n")
    # Readable by the nginx workers only, not by the UI's unprivileged user.
    shutil.chown(htpasswd_path, user="root", group=NGINX_USER)
    os.chmod(htpasswd_path, 0o640)

    conf_path = os.path.join(RUNTIME_DIR, "nginx.conf")
    with open(conf_path, "w") as f:
        f.write(
            NGINX_CONF_TEMPLATE.format(
                runtime_dir=RUNTIME_DIR,
                proxy_port=cfg["ui_proxy_port"],
                ui_port=UI_INTERNAL_PORT,
                nginx_user=NGINX_USER,
            )
        )
    return conf_path


def prepare_ui_dir():
    os.makedirs(UI_DIR, exist_ok=True)
    shutil.chown(UI_DIR, user=UI_USER, group=UI_GROUP)
    for name in os.listdir(UI_DIR):
        shutil.chown(os.path.join(UI_DIR, name), user=UI_USER, group=UI_GROUP)


def start_duckdb(primary, statement, env, user=None, group=None):
    child = subprocess.Popen(
        ["duckdb", primary],
        stdin=subprocess.PIPE,
        stdout=sys.stdout,
        stderr=sys.stderr,
        text=True,
        bufsize=1,
        env=env,
        user=user,
        group=group,
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
        for name in ("nginx", "ui", "owner"):
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

    # 1. Owner: sole owner of the replica file, serves both endpoints with the
    #    per-endpoint authorization hook.
    admin_server_uri = f"quack:0.0.0.0:{cfg['quack_port']}"
    owner_statement = (
        "INSTALL quack; LOAD quack; LOAD json; "
        + authz_macro(admin_server_uri)
        + "SET GLOBAL quack_authorization_function = 'quack_authz'; "
        "CALL quack_serve(" + sql_literal(admin_server_uri)
        + ", token = " + sql_literal(cfg["admin_token"])
        + ", allow_other_hostname = true); "
        "CALL quack_serve(" + sql_literal(f"quack:0.0.0.0:{cfg['readonly_port']}")
        + ", token = " + sql_literal(cfg["readonly_token"])
        + ", allow_other_hostname = true);\n"
    )
    children["owner"] = start_duckdb(cfg["db"], owner_statement, dict(os.environ))
    if not wait_for_port(cfg["quack_port"], "Quack (admin)", process=children["owner"]):
        shutdown(1)
    if not wait_for_port(cfg["readonly_port"], "Quack (read-only)", process=children["owner"]):
        shutdown(1)
    print("Quack admin and read-only endpoints are listening", flush=True)

    # 2 + 3. UI stack, best-effort: the Quack endpoints must keep working even
    #    if the UI cannot start. The UI reaches the replica through the
    #    read-only endpoint, so UI users cannot write to the replica.
    def start_ui_stack():
        prepare_ui_dir()
        ui_statement = (
            "INSTALL quack; LOAD quack; INSTALL ui; LOAD ui; "
            f"SET ui_local_port = {UI_INTERNAL_PORT}; "
            "ATTACH " + sql_literal(f"quack:localhost:{cfg['readonly_port']}")
            + " AS replica (TOKEN " + sql_literal(cfg["readonly_token"]) + "); "
            "CALL start_ui_server();\n"
        )
        children["ui"] = start_duckdb(
            UI_CATALOG,
            ui_statement,
            child_env({"HOME": UI_HOME}),
            user=UI_USER,
            group=UI_GROUP,
        )
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
        print("Stack ready: Quack + read-only endpoint + password-protected UI", flush=True)
    else:
        print("WARNING: UI stack unavailable, continuing with Quack only", flush=True)
        for name in ("nginx", "ui"):
            child = children.pop(name, None)
            if child is not None and child.poll() is None:
                child.terminate()

    next_cache_refresh = time.monotonic() + UI_CACHE_REFRESH_SECONDS
    while True:
        code = children["owner"].poll()
        if code is not None:
            print(f"Owner process exited with code {code}", flush=True)
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
            next_cache_refresh = time.monotonic() + UI_CACHE_REFRESH_SECONDS
            ui_child = children.get("ui")
            if ui_child is not None and ui_child.poll() is None:
                try:
                    ui_child.stdin.write("CALL quack_clear_cache();\n")
                    ui_child.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass  # exit is handled by the checks above
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
