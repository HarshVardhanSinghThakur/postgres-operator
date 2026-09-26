# ── Operator Dockerfile ──────────────────────────────────────────────────────
#
# Multi-stage build:
#   Stage 1 (builder): install dependencies into a venv
#   Stage 2 (runtime): copy only the venv + source, no build tools
#
# WHY MULTI-STAGE:
#   pip install pulls compilers, headers, cache files.
#   The final image only needs the compiled packages.
#   Result: ~200MB → ~80MB. Smaller image = faster pull = smaller attack surface.
#
# WHY ALPINE:
#   Alpine Linux is a minimal distro (~5MB base vs ~70MB Debian).
#   Standard for production containers.

# ── Stage 1: build ───────────────────────────────────────────────────────────
FROM python:3.11-alpine AS builder

WORKDIR /build

# Install build dependencies (needed to compile some Python packages)
RUN apk add --no-cache gcc musl-dev libffi-dev

# Copy and install requirements into an isolated venv
COPY requirements.txt .
RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-alpine AS runtime

# Non-root user — never run production containers as root
RUN addgroup -S operator && adduser -S operator -G operator

WORKDIR /app

# Copy the venv from builder (no pip, no compilers in this stage)
COPY --from=builder /venv /venv

# Copy operator source
COPY operator/ .

# Make sure venv binaries are on PATH
ENV PATH="/venv/bin:$PATH"

# Python output unbuffered — logs appear immediately in kubectl logs
ENV PYTHONUNBUFFERED=1

# Switch to non-root
USER operator

# Health check — verifies the metrics server is up
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:8080/metrics | head -1 || exit 1

# Run the operator
CMD ["python", "main.py"]
