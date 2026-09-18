"""Engine e sessão do SQLite."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

SQLITE_PREFIX = "sqlite:///"

# Colunas acrescentadas depois da criação inicial das tabelas. ``create_all``
# cria tabelas que faltam, mas nunca altera as existentes: sem isto, um banco
# antigo quebraria com "no such column" ao ler os campos novos.
_MIGRATIONS = {
    "customer": {
        "birth_date": "VARCHAR DEFAULT ''",
        "password_hash": "VARCHAR DEFAULT ''",
    },
    "paymentmethod": {
        "brand": "VARCHAR DEFAULT ''",
        "last4": "VARCHAR DEFAULT ''",
        "holder": "VARCHAR DEFAULT ''",
        "expiry": "VARCHAR DEFAULT ''",
    },
    "order": {
        "payment_method": "VARCHAR DEFAULT 'jsr'",
        "pix_code": "VARCHAR DEFAULT ''",
        "updated_at": "DATETIME",
    },
    "integrationsettings": {"sebo_city": "VARCHAR DEFAULT 'SAO PAULO'"},
}


def _ensure_sqlite_directory(url: str) -> None:
    """Cria o diretório do arquivo SQLite, que o engine não cria sozinho."""
    if not url.startswith(SQLITE_PREFIX):
        return
    path = Path(url[len(SQLITE_PREFIX) :])
    if str(path.parent) not in (".", ""):
        path.parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_directory(settings.database_url)

engine = create_engine(
    settings.database_url, connect_args={"check_same_thread": False}
)


def init_db() -> None:
    # Importa os modelos para registrá-los no metadata antes do create_all.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Acrescenta as colunas novas a bancos criados antes delas (só SQLite)."""
    if not settings.database_url.startswith(SQLITE_PREFIX):
        return
    with Session(engine) as session:
        for table, columns in _MIGRATIONS.items():
            existing = {
                row[1]
                for row in session.execute(text(f'PRAGMA table_info("{table}")')).all()
            }
            if not existing:
                continue  # tabela ainda não existe; create_all já a criou com tudo
            for name, ddl in columns.items():
                if name not in existing:
                    session.execute(
                        text(f'ALTER TABLE "{table}" ADD COLUMN {name} {ddl}')
                    )
        # Pedido antigo nasce sem updated_at; sem preencher, a leitura devolve
        # None num campo que não aceita nulo.
        session.execute(
            text('UPDATE "order" SET updated_at = created_at WHERE updated_at IS NULL')
        )
        session.commit()


def get_session() -> Iterator[Session]:
    """Dependência do FastAPI: uma sessão por requisição."""
    with Session(engine) as session:
        yield session
