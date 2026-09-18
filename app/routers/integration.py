"""Leitura de pedidos por outro sistema (omnicommerce) e fila de avisos.

Autenticação por token de serviço, separado da senha do admin e do login do
cliente: é máquina falando com máquina. Sem ``INTEGRATION_TOKEN`` definido,
toda rota daqui recusa — falha fechada.
"""

import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..models import Customer, Order, OrderItem
from ..omnicommerce import flush

router = APIRouter(prefix="/integration", tags=["integração"])

_bearer = HTTPBearer(auto_error=False, description="INTEGRATION_TOKEN")


def require_service_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    token = settings.integration_token
    if not token or credentials is None or not hmac.compare_digest(
        credentials.credentials, token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de integração requerido",
            headers={"WWW-Authenticate": "Bearer"},
        )


_service = Depends(require_service_token)


def _iso(value: datetime) -> str:
    """SQLite devolve datetime sem fuso; quem consome exige o deslocamento."""
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


@router.get("/orders/{order_id}", dependencies=[_service])
def read_order(order_id: int, session: Session = Depends(get_session)) -> dict:
    """Pedido no formato que o omnicommerce normaliza.

    Contrato fechado com o outro lado: os nomes e tipos daqui são o que o
    adapter espera. Mudou aqui, muda lá.
    """
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    items = session.exec(
        select(OrderItem).where(OrderItem.order_id == order.id)
    ).all()
    customer = session.get(Customer, order.customer_id)
    return {
        "id": order.id,
        "status": order.status,
        "total": round(order.total, 2),
        "created_at": _iso(order.created_at),
        "updated_at": _iso(order.updated_at),
        "customer": {
            "name": customer.name if customer else "",
            "email": customer.email if customer else "",
        },
        "items": [
            {
                "product_id": item.product_id,
                "name": item.name,
                "unit_price": round(item.unit_price, 2),
                "quantity": item.quantity,
            }
            for item in items
        ],
    }


@router.post("/outbox/flush", dependencies=[_service])
def flush_outbox(session: Session = Depends(get_session)) -> dict:
    """Reenvia avisos pendentes. Idempotente; serve para recuperar na mão."""
    return flush(session)
