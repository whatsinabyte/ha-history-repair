# The HA build system passes --build-arg BUILD_ARCH=aarch64 (or amd64, etc.)
# but does NOT pass BUILD_FROM when build.yaml is absent.
# We resolve the correct architecture-specific base image here directly.
ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.24

# Clear any existing entrypoint from the base image
ENTRYPOINT []

# libpq is PostgreSQL's client library. psycopg is installed pure-Python (see
# app/requirements.txt for why), which means it loads libpq at runtime rather
# than bundling it — without this the add-on imports fine but every PostgreSQL
# connection fails.
RUN apk add --no-cache python3 py3-pip bash curl jq libpq

WORKDIR /app

# Copy requirements first to maximise Docker layer cache reuse —
# these change less frequently than application code.
COPY app/requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages

# Copy application code (modules, templates, and the vendored Chart.js assets
# under static/ — nothing is fetched from a CDN at runtime).
COPY app/ ./

# ADDON_VERSION in history_corrections.py matches the version: field in
# config.yaml so the two cannot drift apart without a test catching it.
COPY config.yaml /

COPY run.sh /
RUN chmod a+x /run.sh

LABEL org.opencontainers.image.title="History Corrections"
LABEL org.opencontainers.image.description="Correct outlier values in the Home Assistant recorder history"
LABEL org.opencontainers.image.source="https://github.com/whatsinabyte/ha-history-corrections"
LABEL org.opencontainers.image.licenses="MIT"

# Python becomes PID 1 via exec in run.sh and receives signals directly
CMD ["/run.sh"]
