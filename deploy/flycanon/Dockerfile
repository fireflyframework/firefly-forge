# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
FROM python:3.13-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates gcc libpq-dev && apt-get clean
WORKDIR /app
COPY deploy/flycanon/install.py /app/install.py
RUN python /app/install.py
COPY deploy/flycanon/run.py /app/run.py
RUN useradd --create-home --uid 10001 canon && mkdir -p /app/canon-data/objects /run/knowledge && chown -R canon:canon /app/canon-data /run/knowledge
USER canon
ENV FLYCANON_OBJECT_STORE_LOCALFS_ROOT=/app/canon-data/objects \
    FLYCANON_CORPUS_PATH=/app/canon-data/corpus.db \
    FLYCANON_VECTOR_STORE=pgvector \
    FLYCANON_EDA_ADAPTER=postgres \
    FLYCANON_EMBEDDING_MODEL=ollama:nomic-embed-text \
    FLYCANON_EMBEDDING_DIMENSIONS=768 \
    FLYCANON_OLLAMA_BASE_URL=http://embeddings:11434 \
    FLYCANON_EMBEDDING_ZERO_VECTOR_ON_FAILURE=false
ENTRYPOINT ["python", "/app/run.py"]
CMD ["serve"]
