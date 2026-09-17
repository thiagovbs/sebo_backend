# Backend do Sebo On-Line — imagem para rodar via Docker Compose no host.
# Imagem leve: usa uv para instalar as deps a partir do lockfile.
FROM python:3.11-slim

# uv (gerenciador de pacotes) copiado da imagem oficial.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1) Só o manifesto + lock primeiro: camada de dependências cacheável
#    (não rebuilda as deps quando muda apenas o código).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 2) Código da aplicação.
COPY app ./app

# Diretório do banco SQLite. O compose monta um volume aqui (/app/data)
# para o banco sobreviver a recriações do container. O default do app já é
# ./data/sebo.db (= /app/data), e o .env do host pode sobrescrever.
RUN mkdir -p /app/data

# NÃO copiamos o .env para a imagem (segredos). Ele é lido em tempo de
# execução, do host: o compose injeta as variáveis (env_file) e monta o
# arquivo em /app/.env, que o app (pydantic, env_file=".env") também lê.

EXPOSE 8200

# Porta configurável por env ($PORT); por padrão, 8200.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8200}"]
