FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_VERSION=2.4.3

WORKDIR /app

RUN pip install "poetry==${POETRY_VERSION}"

COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root --no-interaction --no-ansi

COPY README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps .

RUN useradd --create-home --uid 10001 appuser

USER appuser

EXPOSE 8000

CMD ["ja-pst-mcp"]
