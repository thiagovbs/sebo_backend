"""Configuração central do backend do Sebo On-Line."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Variáveis de ambiente (com suporte a .env)."""

    # Banco local (SQLite). O diretório é criado automaticamente.
    database_url: str = "sqlite:///./data/sebo.db"

    # Porta do servidor FastAPI.
    port: int = 8200

    # Origem do frontend liberada no CORS (Vite dev server).
    frontend_origin: str = "http://localhost:5173"

    # Nota: os dados de integração com a iniciadora (URL/usuário/senha) e do
    # recebedor do PIX saíram do .env — são configurados no admin em tempo de
    # execução e ficam no banco (ver models.IntegrationSettings).

    # --- Acesso ao painel de admin ------------------------------------------
    # Senha do painel (/admin). Sem ADMIN_PASSWORD próprio, sobe com uma senha
    # de desenvolvimento e avisa no log.
    admin_password: str = "admin123"
    # Segredo que assina o token do admin. Idem: troque em produção.
    admin_secret: str = "dev-insecure-admin-secret-change-me"
    admin_token_hours: int = 8

    # --- Autenticação do cliente (login com e-mail e senha) -----------------
    customer_secret: str = "dev-insecure-customer-secret-change-me"
    customer_token_hours: int = 72

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
