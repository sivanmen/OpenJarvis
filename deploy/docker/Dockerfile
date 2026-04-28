# Stage 1: Build frontend SPA
FROM node:22-slim AS frontend

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --ignore-scripts 2>/dev/null || npm install
COPY frontend/ .
RUN npm run build

# Stage 2: Build Python package
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/

# Copy built frontend into the server static directory
COPY --from=frontend /src/openjarvis/server/static src/openjarvis/server/static/

RUN pip install --no-cache-dir uv && \
    uv pip install --system ".[server,inference-cloud,inference-google,channel-gmail,memory-bm25,scheduler,tools-search]"

# Stage 3: Runtime
FROM python:3.12-slim-bookworm

COPY --from=builder /usr/local /usr/local
COPY --from=builder /app /app
WORKDIR /app

EXPOSE 8000

COPY deploy/docker/entrypoint.sh /usr/local/bin/openjarvis-entrypoint.sh
RUN chmod +x /usr/local/bin/openjarvis-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/openjarvis-entrypoint.sh"]
