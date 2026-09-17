"""Controle de acesso do painel de admin.

Uma senha só (``ADMIN_PASSWORD``) protege o painel. O login devolve um token
assinado (HMAC-SHA256 com ``ADMIN_SECRET``) que carrega apenas a expiração —
sem estado no servidor, sem dependência externa. As rotas administrativas e as
escritas de produto exigem esse token; a vitrine (leitura) continua pública.
"""

import base64
import hashlib
import hmac
import json
import logging
import time

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False, description="Token obtido em POST /admin/login")


# ---------------------------------------------------------------------------
# Token assinado (mini-JWT com stdlib)
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload_b64: str) -> str:
    mac = hmac.new(
        settings.admin_secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    )
    return _b64(mac.digest())


def create_admin_token(hours: int | None = None) -> dict:
    """Emite o token do admin e diz quando expira."""
    ttl = (hours or settings.admin_token_hours) * 3600
    expires_at = int(time.time()) + ttl
    payload = _b64(json.dumps({"exp": expires_at}).encode("utf-8"))
    token = f"{payload}.{_sign(payload)}"
    return {"token": token, "token_type": "bearer", "expires_in": ttl}


def verify_admin_token(token: str) -> bool:
    """Valida assinatura (tempo constante) e expiração."""
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return False
    if not hmac.compare_digest(signature, _sign(payload)):
        return False
    try:
        claims = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return False
    return int(claims.get("exp", 0)) > int(time.time())


def check_admin_password(password: str) -> bool:
    if not settings.admin_password:
        return False
    return hmac.compare_digest(password, settings.admin_password)


# ---------------------------------------------------------------------------
# Dependência de rota
# ---------------------------------------------------------------------------


def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Barra a rota quando não vem um token de admin válido."""
    if credentials is None or not verify_admin_token(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acesso administrativo requerido",
            headers={"WWW-Authenticate": "Bearer"},
        )


def warn_on_dev_defaults() -> None:
    if settings.admin_password == "admin123":
        logger.warning(
            "ADMIN_PASSWORD não definido: usando a senha de desenvolvimento "
            "'admin123'. Defina uma própria em produção."
        )
    if settings.admin_secret == "dev-insecure-admin-secret-change-me":
        logger.warning(
            "ADMIN_SECRET não definido: tokens de admin assinados com o segredo "
            "de desenvolvimento. Defina um próprio em produção."
        )
