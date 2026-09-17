"""Fixtures dos testes: banco temporário e cliente HTTP com iniciadora fake."""

import os
import tempfile

import pytest

# Banco isolado por sessão de teste, antes de importar a app.
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(), "test.db"
).replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.devices as devices_mod  # noqa: E402
import app.routers.orders as orders_mod  # noqa: E402
from app.payments import PaymentInitiatorError  # noqa: E402


class FakeInitiator:
    """Iniciadora de mentira (jornada JSR), controlável pelos testes."""

    def __init__(self):
        self.down = False
        self._devices: dict[str, str] = {}  # enrollment_id -> status
        self._counter = 0

    def start_enrollment(self, username, account_number="", redirect_uri=""):
        if self.down:
            raise PaymentInitiatorError("Iniciadora inacessível: ConnectError")
        self._counter += 1
        eid = f"enr-{self._counter}"
        self._devices[eid] = "PENDING"
        return {"enrollment_id": eid, "login_url": "http://banco/enroll"}

    def register(self, enrollment_id):
        """Simula o cliente concluindo o vínculo no banco."""
        self._devices[enrollment_id] = "REGISTERED"

    def list_devices(self):
        if self.down:
            raise PaymentInitiatorError("Iniciadora inacessível: ConnectError")
        return [
            {"enrollment_id": k, "status": v, "account_id": f"acc-{k}",
             "username": "Titular", "credential_id": f"cred-{k}"}
            for k, v in self._devices.items()
        ]

    def pay_jsr(self, amount, enrollment_id):
        if self.down:
            raise PaymentInitiatorError("Iniciadora inacessível: ConnectError")
        if self._devices.get(enrollment_id) == "REGISTERED":
            return {"payment_id": "pay-test", "consent_id": "con-test", "status": "COMPLETED"}
        return {"need_enrollment": True, "login_url": "http://banco/enroll",
                "message": "Dispositivo não vinculado"}


def _configure_integration():
    """Grava uma config de integração completa no banco de teste."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import IntegrationSettings

    with Session(engine) as s:
        cfg = s.get(IntegrationSettings, 1) or IntegrationSettings(id=1)
        cfg.payment_initiator_url = "http://fake-initiator"
        cfg.payment_initiator_user = "sebo_online"
        cfg.payment_initiator_password = "x"
        cfg.sebo_name = "Sebo On-Line"
        cfg.sebo_cpf_cnpj = "12345678000199"
        cfg.sebo_city = "SAO PAULO"
        cfg.sebo_pix_key_type = "CNPJ"
        cfg.sebo_pix_key_value = "12345678000199"
        s.add(cfg)
        s.commit()


@pytest.fixture
def fake_initiator(monkeypatch, client):
    # A integração precisa estar configurada para o pagamento não ser bloqueado.
    _configure_integration()
    fake = FakeInitiator()
    # orders.py e devices.py resolvem o cliente por get_client(cfg).
    monkeypatch.setattr(orders_mod, "get_client", lambda cfg: fake)
    monkeypatch.setattr(devices_mod, "get_client", lambda cfg: fake)
    return fake


def enroll_and_register(client, fake_initiator, customer_id):
    """Vincula e ativa o dispositivo do cliente (helper dos testes de checkout)."""
    res = client.post(f"/customers/{customer_id}/device/enroll").json()
    fake_initiator.register(res["enrollment_id"])
    return res["enrollment_id"]


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def admin_headers(client):
    """Cabeçalho Authorization com o token do admin (senha de dev 'admin123')."""
    token = client.post("/admin/login", json={"password": "admin123"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def register_customer(client, **fields):
    """Registra um cliente (com senha) e autentica o ``client`` como ele.

    Ajusta o header Authorization padrão do client para o token do cliente
    recém-criado — as chamadas seguintes passam a ser feitas como ele (as de
    admin sobrescrevem com o próprio header por chamada).
    """
    import uuid

    body = {"name": "Fulano", "password": "senha123"}
    body.setdefault("email", f"cli-{uuid.uuid4().hex[:8]}@ex.com")
    body.update(fields)
    res = client.post("/customers/register", json=body).json()
    client.headers["Authorization"] = f"Bearer {res['token']}"
    return res["customer"]


@pytest.fixture
def customer(client):
    return register_customer(client)


def _make_product(client, headers, price: float, stock: int = 50):
    import uuid

    return client.post(
        "/products",
        headers=headers,
        json={
            "name": f"Produto {uuid.uuid4().hex[:6]}",
            "category": "Teste",
            "price": price,
            "stock": stock,
        },
    ).json()


@pytest.fixture
def product(client, admin_headers):
    """Produto próprio do teste, com estoque folgado — evita depender do seed
    e do estoque que outros testes consomem no mesmo banco."""
    return _make_product(client, admin_headers, price=30.0, stock=50)


@pytest.fixture
def product2(client, admin_headers):
    return _make_product(client, admin_headers, price=22.0, stock=50)
