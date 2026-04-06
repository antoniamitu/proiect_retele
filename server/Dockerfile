# Build from repository root:
#   docker build -f server/Dockerfile .
#
# PYTHONPATH must include the project root so `shared` and `server` packages resolve
# (see shared/src/config.py PROJECT_ROOT).

FROM python:3.12-slim

WORKDIR /app

COPY shared /app/shared
COPY server /app/server

ENV PYTHONPATH=/app

EXPOSE 9000

CMD ["python", "-m", "server.src.server_main"]
