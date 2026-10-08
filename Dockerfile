FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl python3 nginx-light && rm -rf /var/lib/apt/lists/*
ENV DUCKDB_VERSION=alpha
# Single DuckDB build (2.0-dev alpha) for every process. The ui extension is
# not yet published for alpha builds: the INSTALL below is allowed to fail and
# the UI stack stays disabled at runtime until upstream publishes it.
RUN curl -fsSL https://install.duckdb.org -o /tmp/install-duckdb.sh && bash /tmp/install-duckdb.sh && rm /tmp/install-duckdb.sh \
    && cp /root/.duckdb/cli/latest/duckdb /usr/local/bin/duckdb \
    && duckdb -c 'INSTALL quack; SELECT version();' \
    && (duckdb -c 'INSTALL ui;' || echo 'ui extension not yet published for this alpha build; the UI will be disabled at runtime')
WORKDIR /app
COPY scripts/server.py /app/server.py
RUN mkdir -p /data
EXPOSE 9494 8080
VOLUME /data
CMD ["python3", "-u", "/app/server.py"]
