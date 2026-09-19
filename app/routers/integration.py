"""Leitura de pedidos por outro sistema (omnicommerce) e fila de avisos.

Autenticação por token de serviço, separado da senha do admin e do login do
cliente: é máquina falando com máquina. Sem ``INTEGRATION_TOKEN`` definido,
toda rota daqui recusa — falha fechada.
"""

import hmac
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..models import Customer, Order, OrderItem, Product
from ..omnicommerce import flush
from ..schemas import ProductOut, ProductPublishIn

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


@router.get("/orders", dependencies=[_service])
def list_changed_orders(
    updated_since: str = Query(..., description="ISO 8601 com fuso"),
    limit: int = Query(200, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    """Pedidos alterados a partir de uma data, para a conciliação do omnicommerce.

    Devolve só identificação e carimbo: quem quiser o pedido inteiro busca em
    ``/integration/orders/{id}``. Assim a conciliação varre barato e o contrato
    do pedido continua num lugar só.
    """
    # "+" não escapado em query string chega como espaço, e o fuso se perde.
    # Restaurar só quando o que sobrou parece deslocamento evita confundir com
    # o separador de data e hora, que o ISO também aceita como espaço.
    bruto = re.sub(r" (\d{2}:\d{2})$", r"+\1", updated_since.strip())
    try:
        desde = datetime.fromisoformat(bruto)
    except ValueError:
        raise HTTPException(status_code=400, detail="updated_since inválido") from None
    # O banco guarda UTC sem fuso; comparar com valor ciente levantaria TypeError.
    if desde.tzinfo:
        desde = desde.astimezone(timezone.utc).replace(tzinfo=None)

    pedidos = session.exec(
        select(Order)
        .where(Order.updated_at >= desde)
        .order_by(Order.updated_at)
        .limit(limit)
    ).all()
    return [{"id": o.id, "status": o.status, "updated_at": _iso(o.updated_at)} for o in pedidos]


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
    # O SKU liga o item de volta ao catálogo de quem publicou, que é o que
    # permite baixar estoque lá. Produto cadastrado à mão não tem SKU, e o
    # campo vai vazio em vez de ausente: o contrato é o mesmo para todo item.
    skus = {
        item.product_id: (session.get(Product, item.product_id) or Product()).external_sku
        for item in items
    }
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
                "sku": skus.get(item.product_id, ""),
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


# ---------------------------------------------------------------------------
# Catálogo publicado de fora
# ---------------------------------------------------------------------------


@router.post("/products", response_model=ProductOut, dependencies=[_service])
def publish_product(
    payload: ProductPublishIn, session: Session = Depends(get_session)
) -> Product:
    """Publica (ou republica) um produto vindo de outro sistema.

    Idempotente pelo ``sku``: se já existe produto com aquele SKU, ele é
    atualizado e o mesmo id volta. É o que impede que uma retentativa, depois
    de uma resposta que se perdeu no caminho, crie um segundo anúncio do mesmo
    item — do outro lado não há transação que abranja a nossa escrita.
    """
    dados = payload.model_dump()
    sku = dados.pop("sku").strip().upper()
    if not sku:
        raise HTTPException(status_code=400, detail="sku inválido")

    produto = session.exec(
        select(Product).where(Product.external_sku == sku)
    ).first()
    if produto is None:
        produto = Product(external_sku=sku, **dados)
    else:
        for campo, valor in dados.items():
            setattr(produto, campo, valor)
    session.add(produto)
    session.commit()
    session.refresh(produto)
    return produto


@router.get("/products/{product_id}", response_model=ProductOut, dependencies=[_service])
def read_published_product(
    product_id: int, session: Session = Depends(get_session)
) -> Product:
    """Estado atual do produto, para o outro lado conferir o que publicou."""
    produto = session.get(Product, product_id)
    if not produto:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    return produto
