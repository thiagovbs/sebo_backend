"""Autenticação do cliente (login com e-mail e senha).

O cliente agora entra com credenciais, não é mais escolhido livremente. A senha
é guardada em PBKDF2-HMAC-SHA256 (só stdlib) e o login devolve um token assinado
(HMAC-SHA256 com ``customer_secret``) que carrega o id do cliente e a expiração.
As rotas do cliente exigem esse token e conferem que o id bate com o do token.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from .config import settings
from .db import get_session
from .models import Customer

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False, description="Token do cliente (POST /customers/login)")

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 200_000


# ---------------------------------------------------------------------------
# Senha (PBKDF2)
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join([_ALGORITHM, str(iterations), _b64(salt), _b64(digest)])


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, raw_iter, raw_salt, raw_digest = encoded.split("$")
        if algo != _ALGORITHM:
            return False
        expected = _unb64(raw_digest)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _unb64(raw_salt), int(raw_iter)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


# ---------------------------------------------------------------------------
# Token assinado (id do cliente + expiração)
# ---------------------------------------------------------------------------


def _sign(payload_b64: str) -> str:
    mac = hmac.new(
        settings.customer_secret.encode(), payload_b64.encode("ascii"), hashlib.sha256
    )
    return _b64(mac.digest())


def create_customer_token(customer_id: int, hours: int | None = None) -> dict:
    ttl = (hours or settings.customer_token_hours) * 3600
    expires_at = int(time.time()) + ttl
    payload = _b64(json.dumps({"sub": customer_id, "exp": expires_at}).encode())
    token = f"{payload}.{_sign(payload)}"
    return {"token": token, "token_type": "bearer", "expires_in": ttl}


def verify_customer_token(token: str) -> int | None:
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        claims = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(claims.get("exp", 0)) <= int(time.time()):
        return None
    return claims.get("sub")


# ---------------------------------------------------------------------------
# Dependência de rota
# ---------------------------------------------------------------------------


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_customer(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> Customer:
    """Resolve o cliente a partir do token; 401 se ausente/inválido/expirado."""
    if credentials is None or not credentials.credentials:
        raise _unauthorized("Faça login para continuar")
    customer_id = verify_customer_token(credentials.credentials)
    if not customer_id:
        raise _unauthorized("Sessão inválida ou expirada")
    customer = session.get(Customer, customer_id)
    if not customer:
        raise _unauthorized("Cliente não encontrado")
    return customer


def ensure_self(customer: Customer, customer_id: int) -> None:
    """Garante que o cliente do token é o dono do recurso pedido na URL."""
    if customer.id != customer_id:
        raise HTTPException(status_code=403, detail="Acesso negado a outro cliente")


def warn_on_dev_secret() -> None:
    if settings.customer_secret == "dev-insecure-customer-secret-change-me":
        logger.warning(
            "CUSTOMER_SECRET não definido: tokens de cliente assinados com o "
            "segredo de desenvolvimento. Defina um próprio em produção."
        )
