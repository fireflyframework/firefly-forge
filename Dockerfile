FROM node:22-bookworm-slim AS studio
WORKDIR /build/studio
COPY studio/package*.json ./
RUN npm ci
COPY studio/ ./
RUN npm run build

FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && apt-get clean
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
COPY --from=studio /build/studio/dist ./studio/dist
RUN uv sync --locked --no-dev
RUN useradd --create-home --uid 10001 forge && mkdir /data && chown forge:forge /data
USER forge
ENV FORGE_DATA_DIR=/data
EXPOSE 8787
CMD ["/app/.venv/bin/forge", "serve", "--host", "0.0.0.0"]
