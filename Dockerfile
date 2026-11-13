FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS build

WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv

# Install the locked dependencies first, so a source-only change reuses this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# A numeric user, so a Kubernetes runAsNonRoot check can verify it without a passwd lookup.
USER 10001:10001

ENTRYPOINT ["wan-dns-sync"]
CMD ["--config", "/etc/wan-dns-sync/config.yaml"]
