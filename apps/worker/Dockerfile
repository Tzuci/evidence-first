# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY packages/shared/ /build/packages/shared/
COPY apps/worker/ /app/
COPY scripts/wait-for-it.sh /usr/local/bin/wait-for-it.sh

RUN chmod +x /usr/local/bin/wait-for-it.sh /app/entrypoint.sh && \
    pip install /build/packages/shared && \
    pip install \
        "sqlalchemy>=2.0,<2.1" \
        "psycopg[binary]>=3.1,<4" \
        "redis>=5.0,<6" \
        "pydantic>=2.6,<3" \
        "pydantic-settings>=2.2,<3" \
        "structlog>=24.1,<25" \
        "tenacity>=8.2,<10" \
        "pytest>=8.0,<9"

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "app.main"]