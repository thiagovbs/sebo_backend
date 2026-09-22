"""Integração com o omnicommerce: leitura do pedido e fila de avisos.

Nenhum teste toca a rede: o cliente HTTP é substituído por um dublê.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from sqlmodel import Session, select

import app.omnicommerce as omni
from app.config import settings
from app.db import engine
from app.models import Order, OutboxNotification, Product
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
            # Presente mesmo vazio: produto cadastrado à mão não tem SKU, e o
            # consumidor recebe sempre a mesma forma de item.
            "sku": "",
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


def test_listing_changed_orders_needs_the_token_and_a_valid_date(client, service_headers):
    assert client.get("/integration/orders?updated_since=2020-01-01T00:00:00+00:00").status_code == 401
    r = client.get("/integration/orders?updated_since=nao-e-data", headers=service_headers)
    assert r.status_code == 400
    # Sem o parâmetro, a conciliação varreria o histórico inteiro sem querer.
    assert client.get("/integration/orders", headers=service_headers).status_code == 422


def test_listing_tolerates_an_unescaped_plus_in_the_timezone(client, service_headers):
    # "+" cru vira espaço na query string; o endpoint recupera em vez de dar 400.
    r = client.get(
        "/integration/orders?updated_since=2000-01-01T00:00:00+00:00", headers=service_headers
    )
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_listing_returns_only_what_changed_since(
    client, customer, product, fake_initiator, service_headers
):
    pedido = _comprar(client, customer, product, fake_initiator)

    with Session(engine) as session:
        alvo = session.get(Order, pedido["id"])
        marco = omni._as_utc(alvo.updated_at)

    # A partir do próprio carimbo, o pedido entra (a comparação é inclusiva).
    ids = [o["id"] for o in client.get(
        f"/integration/orders?updated_since={quote(marco.isoformat())}", headers=service_headers
    ).json()]
    assert pedido["id"] in ids

    # Depois dele, não.
    depois = (marco + timedelta(seconds=1)).isoformat()
    ids = [o["id"] for o in client.get(
        f"/integration/orders?updated_since={quote(depois)}", headers=service_headers
    ).json()]
    assert pedido["id"] not in ids


def test_listing_carries_timezone_and_the_fields_the_consumer_reads(
    client, customer, product, fake_initiator, service_headers
):
    _comprar(client, customer, product, fake_initiator)
    linhas = client.get(
        "/integration/orders?updated_since=" + quote("2000-01-01T00:00:00+00:00"),
        headers=service_headers,
    ).json()
    assert linhas
    for linha in linhas:
        assert set(linha) == {"id", "status", "updated_at"}
        # O consumidor recusa data sem deslocamento de fuso.
        assert datetime.fromisoformat(linha["updated_at"]).tzinfo is not None
    # Ordenado por updated_at: a conciliação pagina a partir do mais antigo.
    carimbos = [datetime.fromisoformat(l["updated_at"]) for l in linhas]
    assert carimbos == sorted(carimbos)


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


def test_notification_of_a_deleted_order_is_discarded_instead_of_sent(
    client, customer, product, fake_initiator, integracao
):
    """Aviso de pedido apagado não sai da fila.

    Do outro lado, o aviso vira uma consulta ao pedido -- que responde 404 e
    deixa lá uma falha permanente que ninguém consegue resolver. Aconteceu de
    verdade: 224 eventos assim, e a venda nova esperando atrás deles.
    """
    integracao.status_code = 500  # segura o aviso na fila
    pedido = _comprar(client, customer, product, fake_initiator)

    with Session(engine) as session:
        # O pedido some (rollback de emissão, limpeza, o que for).
        session.delete(session.get(Order, pedido["id"]))
        session.commit()

    integracao.chamadas.clear()
    integracao.status_code = 200
    with Session(engine) as session:
        aviso = session.exec(
            select(OutboxNotification)
            .where(OutboxNotification.order_id == pedido["id"])
            .where(OutboxNotification.status == "PENDING")
        ).one()
        aviso_id = aviso.id
        aviso.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(aviso)
        session.commit()
        resultado = omni.flush(session)

    assert resultado["discarded"] >= 1 and resultado["delivered"] == 0
    # E nada foi enviado: o destino nem fica sabendo.
    assert integracao.chamadas == []
    with Session(engine) as session:
        aviso = session.get(OutboxNotification, aviso_id)
    assert aviso.status == "DISCARDED"
    assert aviso.last_error == "ORDER_GONE"
    # Descarte não é falha: não há o que corrigir, e a tentativa não é gasta.
    assert aviso.attempts == 1


def test_a_notification_that_aged_in_the_queue_is_dropped(
    client, customer, product, fake_initiator, integracao
):
    """A fila só é esvaziada quando alguém compra.

    Uma loja dias fora do ar despejava, na primeira venda seguinte, toda a
    história acumulada -- e a venda nova ia para o fim dessa fila. Passado o
    prazo, o aviso velho é descartado; pedido antigo entra pela conciliação.
    """
    integracao.status_code = 500
    pedido = _comprar(client, customer, product, fake_initiator)

    # Pelo id do aviso, e não pelo do pedido: o SQLite reaproveita o id de um
    # pedido apagado, e o teste acima apaga um.
    with Session(engine) as session:
        aviso = session.exec(
            select(OutboxNotification)
            .where(OutboxNotification.order_id == pedido["id"])
            .where(OutboxNotification.status == "PENDING")
        ).one()
        aviso_id = aviso.id
        antigo = datetime.now(timezone.utc) - timedelta(
            days=settings.notification_max_age_days + 1)
        aviso.created_at = antigo
        aviso.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(aviso)
        session.commit()

    integracao.chamadas.clear()
    integracao.status_code = 200
    with Session(engine) as session:
        assert omni.flush(session)["discarded"] >= 1
        aviso = session.get(OutboxNotification, aviso_id)
    assert aviso.status == "DISCARDED" and aviso.last_error == "TOO_OLD"
    # E nada deste pedido foi enviado.
    assert all(f"/orders/{pedido['id']}" != c["json"]["resource"] for c in integracao.chamadas)


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


# ---------------------------------------------------------------------------
# Catálogo publicado de fora
# ---------------------------------------------------------------------------


def _publicar(client, headers, **campos):
    corpo = {
        "sku": "LIVRO-001",
        "name": "Livro publicado pelo omnicommerce",
        "description": "Descrição",
        "category": "Livros",
        "price": 49.9,
        "stock": 7,
        "brand": "Editora",
        "condition": "usado",
        "active": True,
    }
    corpo.update(campos)
    return client.post("/integration/products", json=corpo, headers=headers)


def test_publishing_a_product_requires_the_service_token(client):
    assert _publicar(client, {}).status_code == 401
    assert _publicar(client, {"Authorization": "Bearer errado"}).status_code == 401


def test_publishing_creates_the_product_with_the_values_sent(client, service_headers):
    r = _publicar(client, service_headers)
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["name"] == "Livro publicado pelo omnicommerce"
    assert corpo["price"] == 49.9
    assert corpo["stock"] == 7
    assert corpo["active"] is True

    # Aparece na vitrine como qualquer outro produto.
    vitrine = client.get("/products").json()["items"]
    assert corpo["id"] in [p["id"] for p in vitrine]


def test_republishing_the_same_sku_updates_instead_of_duplicating(client, service_headers):
    primeiro = _publicar(client, service_headers).json()
    segundo = _publicar(client, service_headers, price=59.9, stock=3).json()

    # O mesmo id volta: uma retentativa não cria um segundo anúncio.
    assert segundo["id"] == primeiro["id"]
    assert segundo["price"] == 59.9
    assert segundo["stock"] == 3

    with Session(engine) as session:
        iguais = session.exec(
            select(Product).where(Product.external_sku == "LIVRO-001")
        ).all()
    assert len(iguais) == 1


def test_sku_is_normalized_to_upper_case(client, service_headers):
    criado = _publicar(client, service_headers, sku="livro-002").json()
    with Session(engine) as session:
        assert session.get(Product, criado["id"]).external_sku == "LIVRO-002"
    # E o mesmo SKU em minúsculas encontra o mesmo produto.
    assert _publicar(client, service_headers, sku="LIVRO-002").json()["id"] == criado["id"]


def test_reading_a_published_product_back(client, service_headers):
    criado = _publicar(client, service_headers).json()
    lido = client.get(f"/integration/products/{criado['id']}", headers=service_headers)
    assert lido.status_code == 200
    assert lido.json()["stock"] == 7
    assert client.get("/integration/products/999999", headers=service_headers).status_code == 404


def test_order_items_carry_the_sku_so_the_publisher_can_debit_stock(
    client, customer, product, fake_initiator, service_headers
):
    # Produto da fixture não nasceu de publicação: SKU vazio, campo presente.
    pedido = _comprar(client, customer, product, fake_initiator)
    item = client.get(
        f"/integration/orders/{pedido['id']}", headers=service_headers
    ).json()["items"][0]
    assert item["sku"] == ""

    # Já um produto publicado carrega o SKU de volta no pedido.
    publicado = _publicar(client, service_headers, stock=5).json()
    client.post(
        f"/cart/{customer['id']}/items",
        json={"product_id": publicado["id"], "quantity": 1},
    )
    novo = client.post("/orders/checkout", json={"customer_id": customer["id"]}).json()
    itens = client.get(
        f"/integration/orders/{novo['id']}", headers=service_headers
    ).json()["items"]
    assert [i["sku"] for i in itens] == ["LIVRO-001"]
