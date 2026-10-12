# syntax=docker/dockerfile:1.7
#
#   docker build -t insurance-rag .                    -> API image (default, last stage)
#   docker build --target ingest -t insurance-rag:ingest .   -> one-off ingestion job image

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# The API image gets API + tracing only; PDF/eval libraries stay out (smaller attack surface).
RUN pip install --prefix=/install ".[observability]" \
 && pip install --prefix=/install-ingest ".[observability,ingestion]"

FROM python:3.12-slim AS ingest
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /install-ingest /usr/local
RUN useradd --system --uid 10001 --home-dir /app app && mkdir -p /app/data && chown -R 10001 /app
USER 10001
WORKDIR /app
ENTRYPOINT ["python", "-m", "insurance_rag"]
CMD ["ingest"]

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --no-create-home app
USER 10001
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status==200 else 1)"
# Each task runs 2 workers; scale out with more ECS tasks rather than more workers per task.
CMD ["uvicorn", "insurance_rag.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "2", "--no-access-log"]
