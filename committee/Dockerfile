FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --all-extras --no-dev
COPY config ./config
COPY prompts ./prompts
COPY docs ./docs
ENV PATH="/app/.venv/bin:$PATH"
CMD ["committee", "--help"]
