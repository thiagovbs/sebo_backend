"""Integração com o omnicommerce: leitura do pedido e fila de avisos.

Nenhum teste toca a rede: o cliente HTTP é substituído por um dublê.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

import app.omnicommerce as omni
from app.config import settings
from app.db import engine
from app.models import Order, OutboxNotification
from tests.conftest import enroll_and_register

TOKEN = "token-de-servico-de-teste"
WEBHOOK = "https://omni.invalid/api/webhooks/sebo/segredo"


class RespostaFake:
    def __init__(self, status_code: int):
        self.status_code = status_code


class HttpxFake:
    """Dublê do httpx: registra as chamadas e devolve o status combinado."""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.chamadas: list[dict] = []
        self.erro: Exception | None = None

    def post(self, url, json=None, timeout=None, follow_redirects=None):
        self.chamadas.append({"url": url, "json": json, "timeout": timeout})
        if self.erro:
            raise self.erro
        return RespostaFake(self.status_code)


@pytest.fixture
def integracao(monkeypatch):
    """Liga a integração e troca o cliente HTTP pelo dublê."""
    monkeypatch.setattr(settings, "omnicommerce_webhook_url", WEBHOOK)
    monkeypatch.setattr(settings, "integration_token", TOKEN)
    monkeypatch.setattr(settings, "store_id", "sebo_online")
    fake = HttpxFake()
    monkeypatch.setattr(omni, "httpx", fake)
    return fake


@pytest.fixture
def service_headers(integracao):
    return {"Authorization": f"Bearer {TOKEN}"}


def _comprar(client, customer, product, fake_initiator, qty=1):
    enroll_and_register(client, fake_initiator, customer["id"])
    client.post(
        f"/cart/{customer['id']}/items",
        json={"product_id": product["id"], "quantity": qty},
    )
    return client.post("/orders/checkout", json={"customer_id": customer["id"]}).json()


# ---------------------------------------------------------------------------
# Leitura do pedido
# ---------------------------------------------------------------------------


def test_reading_an_order_requires_the_service_token(client, integracao):
    assert client.get("/integration/orders/1").status_code == 401
    r = client.get("/integration/orders/1", headers={"Authorization": "Bearer errado"})
    assert r.status_code == 401


def test_without_token_configured_nothing_is_readable(client, monkeypatch):
    monkeypatch.setattr(settings, "integration_token", "")
    r = client.get("/integration/orders/1", headers={"Authorization": "Bearer "})
    assert r.status_code == 401


def test_order_payload_matches_the_contract(
    client, customer, product, fake_initiator, service_headers
):
    pedido = _comprar(client, customer, product, fake_initiator, qty=2)

    corpo = client.get(
        f"/integration/orders/{pedido['id']}", headers=service_headers
    ).json()

    assert corpo["id"] == pedido["id"]
    assert corpo["status"] == "PAID"
    assert corpo["total"] == round(product["price"] * 2, 2)
    assert corpo["customer"]["email"] == customer["email"]
    assert corpo["items"] == [
        {
            "product_id": product["id"],
            "name": product["name"],
            "unit_price": product["price"],
            "quantity": 2,
        }
    ]
    # O outro lado recusa data sem deslocamento de fuso.
    for campo in ("created_at", "updated_at"):
        assert datetime.fromisoformat(corpo[campo]).tzinfo is not None
    # O total precisa cobrir a soma dos itens, senão o adapter recusa.
    soma = sum(i["unit_price"] * i["quantity"] for i in corpo["items"])
    assert corpo["total"] >= round(soma, 2) - 0.005


def test_unknown_order_is_404(client, service_headers):
    assert client.get("/integration/orders/999999", headers=service_headers).status_code == 404


# ---------------------------------------------------------------------------
# Fila de avisos
# ---------------------------------------------------------------------------


def test_checkout_enqueues_and_delivers_one_notification(
    client, customer, product, fake_initiator, integracao
):
    pedido = _comprar(client, customer, product, fake_initiator)

    with Session(engine) as session:
        avisos = session.exec(
            select(OutboxNotification).where(OutboxNotification.order_id == pedido["id"])
        ).all()
    assert len(avisos) == 1
    assert avisos[0].status == "DELIVERED"

    assert len(integracao.chamadas) == 1
    enviado = integracao.chamadas[0]
    assert enviado["url"] == WEBHOOK
    assert enviado["json"]["topic"] == "orders"
    assert enviado["json"]["resource"] == f"/orders/{pedido['id']}"
    assert enviado["json"]["store_id"] == "sebo_online"
    assert datetime.fromisoformat(enviado["json"]["sent"]).tzinfo is not None


def test_cancelling_emits_another_notification(
    client, customer, product, fake_initiator, integracao
):
    pedido = _comprar(client, customer, product, fake_initiator)
    # Pedido pago não cancela; usa um PIX QR em aberto para exercitar o cancelamento.
    with Session(engine) as session:
        pago = session.get(Order, pedido["id"])
        pago.status = "AWAITING_PAYMENT"
        session.add(pago)
        session.commit()

    client.post(f"/orders/{pedido['id']}/cancel")

    with Session(engine) as session:
        avisos = session.exec(
            select(OutboxNotification).where(OutboxNotification.order_id == pedido["id"])
        ).all()
    assert len(avisos) == 2
    # Cada aviso tem a própria identidade: são mudanças distintas.
    assert len({a.sent_at for a in avisos}) == 2


def test_delivery_failure_keeps_the_notification_for_a_retry(
    client, customer, product, fake_initiator, integracao
):
    integracao.status_code = 500
    pedido = _comprar(client, customer, product, fake_initiator)

    with Session(engine) as session:
        aviso = session.exec(
            select(OutboxNotification).where(OutboxNotification.order_id == pedido["id"])
        ).one()
        assert aviso.status == "PENDING"
        assert aviso.attempts == 1
        assert aviso.last_error  # guardado, sem corpo da resposta
        assert omni._as_utc(aviso.next_attempt_at) > datetime.now(timezone.utc)
        identidade = aviso.sent_at

        # Vencido o backoff e com o destino de volta, entrega e conserva a identidade.
        aviso.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(aviso)
        session.commit()

    integracao.status_code = 200
    with Session(engine) as session:
        assert omni.flush(session)["delivered"] == 1
        aviso = session.exec(
            select(OutboxNotification).where(OutboxNotification.order_id == pedido["id"])
        ).one()
        assert aviso.status == "DELIVERED"
        assert aviso.sent_at == identidade


def test_notification_gives_up_after_the_attempt_ceiling(
    client, customer, product, fake_initiator, integracao
):
    integracao.status_code = 503
    pedido = _comprar(client, customer, product, fake_initiator)

    for _ in range(settings.notification_max_attempts):
        with Session(engine) as session:
            aviso = session.exec(
                select(OutboxNotification).where(
                    OutboxNotification.order_id == pedido["id"]
                )
            ).one()
            aviso.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.add(aviso)
            session.commit()
            omni.flush(session)

    with Session(engine) as session:
        aviso = session.exec(
            select(OutboxNotification).where(OutboxNotification.order_id == pedido["id"])
        ).one()
    assert aviso.status == "FAILED"
    assert aviso.attempts >= settings.notification_max_attempts


def test_flush_endpoint_requires_the_service_token(client, integracao, service_headers):
    assert client.post("/integration/outbox/flush").status_code == 401
    r = client.post("/integration/outbox/flush", headers=service_headers)
    assert r.status_code == 200
    assert "delivered" in r.json()


def test_integration_off_does_not_enqueue_anything(
    client, customer, product, fake_initiator, monkeypatch
):
    monkeypatch.setattr(settings, "omnicommerce_webhook_url", "")
    fake = HttpxFake()
    monkeypatch.setattr(omni, "httpx", fake)

    pedido = _comprar(client, customer, product, fake_initiator)

    with Session(engine) as session:
        avisos = session.exec(
            select(OutboxNotification).where(OutboxNotification.order_id == pedido["id"])
        ).all()
    assert avisos == []
    assert fake.chamadas == []


# ---------------------------------------------------------------------------
# Carimbo de atualização
# ---------------------------------------------------------------------------


def test_updated_at_advances_on_every_change(
    client, customer, product, fake_initiator, integracao
):
    pedido = _comprar(client, customer, product, fake_initiator)
    with Session(engine) as session:
        antes = omni._as_utc(session.get(Order, pedido["id"]).updated_at)
        alvo = session.get(Order, pedido["id"])
        alvo.status = "AWAITING_PAYMENT"
        session.add(alvo)
        session.commit()

    client.post(f"/orders/{pedido['id']}/cancel")

    with Session(engine) as session:
        depois = omni._as_utc(session.get(Order, pedido["id"]).updated_at)
    assert depois > antes
