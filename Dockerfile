FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl python3 nginx-light && rm -rf /var/lib/apt/lists/*
# Pinned DuckDB version, used by every process in the container.
# quack and ui extensions are preinstalled at build time, both for root
# (owner process) and for the unprivileged home used by the UI process.
ENV DUCKDB_VERSION=v1.5.6
RUN curl -fsSL https://install.duckdb.org -o /tmp/install-duckdb.sh && bash /tmp/install-duckdb.sh && rm /tmp/install-duckdb.sh \
    && cp /root/.duckdb/cli/${DUCKDB_VERSION#v}/duckdb /usr/local/bin/duckdb \
    && duckdb -c 'INSTALL quack; INSTALL ui; SELECT version();' \
    && mkdir -p /var/lib/duckdb-ui \
    && cp -r /root/.duckdb /var/lib/duckdb-ui/.duckdb \
    && rm -rf /var/lib/duckdb-ui/.duckdb/cli \
    && chown -R nobody:nogroup /var/lib/duckdb-ui
WORKDIR /app
COPY scripts/server.py /app/server.py
RUN mkdir -p /data
EXPOSE 9494 9495 8080
VOLUME /data
CMD ["python3", "-u", "/app/server.py"]
