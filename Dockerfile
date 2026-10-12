FROM node:22-bookworm-slim AS studio
WORKDIR /build/studio
COPY studio/package*.json ./
RUN npm ci
COPY studio/ ./
RUN npm run build

FROM python:3.13-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates php-cli php-mbstring php-xml php-curl php-sqlite3 php-zip composer && apt-get clean
COPY --from=studio /usr/local/bin/node /usr/local/bin/node
COPY --from=studio /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
COPY scripts/build_hook.py ./scripts/build_hook.py
COPY --from=studio /build/studio/dist ./studio/dist
RUN uv sync --locked --no-dev
RUN useradd --create-home --uid 10001 forge && mkdir /data && chown forge:forge /data
USER forge
ENV FORGE_DATA_DIR=/data
EXPOSE 8787
CMD ["/app/.venv/bin/forge", "serve", "--host", "0.0.0.0"]
