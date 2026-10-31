FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
RUN useradd --system --no-create-home app
USER app
ENV PATH="/app/.venv/bin:$PATH"
# `exec` makes the server PID 1, so `docker stop` reaches it and it shuts down cleanly.
# FORWARDED_ALLOW_IPS: the reverse proxy whose X-Forwarded-For is trusted (see README).
CMD ["sh", "-c", "alembic upgrade head && exec fastapi run app/main.py --port 8000 --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
