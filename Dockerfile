# Arm A investigation agent — container image.
#
# The application reads configuration/secrets from the environment ONLY
# (ANTHROPIC_API_KEY, COMPANIES_HOUSE_API_KEY, AUDIT_DATABASE_URL, ...); none are
# baked into the image. In production these are injected at runtime (workload
# identity -> Key Vault). git is needed at build time to resolve the pinned
# git-subdirectory shared-package dependencies.

FROM python:3.12-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Resolve from the committed lockfile; install runtime deps + the audit extra
# (Postgres driver) but not dev tooling.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY fixtures ./fixtures
RUN uv sync --frozen --no-dev --extra audit


FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY --from=builder /app /app
RUN chown -R appuser:appuser /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser

# Default: run the multi-hop investigation agent over the bundled fixtures and
# write traces. Override the entrypoint args to `score` etc. as needed.
ENTRYPOINT ["credit-risk-eval"]
CMD ["run-agent", "--fixtures", "/app/fixtures/fixtures.yaml", "--trace-dir", "/app/traces/agent"]
