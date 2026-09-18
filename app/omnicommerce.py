"""Entrega de avisos de pedido ao omnicommerce.

O aviso é gravado (``OutboxNotification``) junto com a mudança do pedido e só
sai da fila quando o destino confirma. Sem isso, um POST direto perderia a
venda em silêncio numa queda de rede — e o omnicommerce nunca saberia que ela
existiu.

O aviso carrega só a referência do pedido; quem lê o pedido é o omnicommerce,
chamando ``GET /integration/orders/{id}`` de volta. Mesmo desenho do Mercado
Livre, para haver um modelo só.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlmodel import Session, select

from .config import settings
from .models import Order, OutboxNotification

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite devolve datetime sem fuso; a aplicação sempre grava em UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_enabled() -> bool:
    return bool(settings.omnicommerce_webhook_url)


def enqueue_order_event(session: Session, order: Order) -> OutboxNotification | None:
    """Enfileira o aviso. Quem chama faz o commit, junto com o pedido.

    Chamar **depois** de os itens estarem gravados: o omnicommerce lê o pedido
    assim que recebe o aviso, e um pedido sem itens seria recusado por ele.
    """
    if not is_enabled() or order.id is None:
        return None
    notification = OutboxNotification(order_id=order.id)
    session.add(notification)
    return notification


def _payload(notification: OutboxNotification) -> dict:
    return {
        "topic": notification.topic,
        "resource": f"/orders/{notification.order_id}",
        "store_id": settings.store_id,
        # Estável entre tentativas: é a identidade do evento do outro lado.
        "sent": _as_utc(notification.sent_at).isoformat(),
    }


def _deliver(notification: OutboxNotification) -> None:
    response = httpx.post(
        settings.omnicommerce_webhook_url,
        json=_payload(notification),
        timeout=10.0,
        follow_redirects=False,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")


def flush(session: Session, limit: int = 20) -> dict:
    """Tenta entregar os avisos vencidos. Seguro de chamar a qualquer momento."""
    if not is_enabled():
        return {"delivered": 0, "failed": 0, "skipped": "integração desligada"}

    now = _now()
    pending = session.exec(
        select(OutboxNotification)
        .where(OutboxNotification.status == "PENDING")
        .order_by(OutboxNotification.created_at)
        .limit(limit)
    ).all()

    delivered = failed = 0
    for notification in pending:
        if _as_utc(notification.next_attempt_at) > now:
            continue
        notification.attempts += 1
        try:
            _deliver(notification)
        except Exception as exc:  # rede, timeout, status de erro
            # A mensagem do erro pode conter detalhe do destino; guarda curta.
            notification.last_error = type(exc).__name__
            if notification.attempts >= settings.notification_max_attempts:
                notification.status = "FAILED"
            else:
                atraso = min(3600, 2**notification.attempts)
                notification.next_attempt_at = _now() + timedelta(seconds=atraso)
            failed += 1
            logger.warning(
                "aviso do pedido %s não entregue (tentativa %s): %s",
                notification.order_id, notification.attempts, notification.last_error,
            )
        else:
            notification.status = "DELIVERED"
            notification.delivered_at = _now()
            notification.last_error = ""
            delivered += 1
        session.add(notification)
    session.commit()
    return {"delivered": delivered, "failed": failed}


def flush_in_background() -> None:
    """Versão para BackgroundTasks: abre a própria sessão e nunca propaga erro."""
    from .db import engine

    try:
        with Session(engine) as session:
            flush(session)
    except Exception:  # nunca derruba a requisição que já respondeu
        logger.exception("falha ao esvaziar a fila de avisos")
