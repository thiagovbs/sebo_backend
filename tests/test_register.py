"""Autocadastro do cliente (com senha) + endereços e formas de pagamento."""

import uuid

from tests.conftest import register_customer


def _payload(**over):
    base = {
        "name": "Maria Leitora",
        "email": f"maria-{uuid.uuid4().hex[:8]}@ex.com",
        "password": "senha-forte",
        "phone": "11999990000",
        "cpf": "123.456.789-00",
        "birth_date": "1990-05-20",
        "addresses": [
            {"label": "Casa", "street": "Rua A", "number": "10", "city": "Campinas", "state": "SP"},
            {"label": "Trabalho", "street": "Av B", "number": "200", "city": "Campinas", "state": "SP"},
        ],
        "payment_methods": [
            {"label": "Cartão principal", "brand": "Visa", "last4": "4321", "holder": "MARIA LEITORA", "expiry": "12/29"},
            {"label": "Cartão reserva", "brand": "Mastercard", "last4": "8888", "holder": "MARIA LEITORA", "expiry": "05/27"},
        ],
    }
    base.update(over)
    return base


def test_register_creates_customer_addresses_and_methods(client):
    c = register_customer(client, **_payload())  # já autentica o client
    assert c["birth_date"] == "1990-05-20"
    cid = c["id"]

    addrs = client.get(f"/customers/{cid}/addresses").json()
    assert len(addrs) == 2
    methods = client.get(f"/customers/{cid}/payment-methods").json()
    assert len(methods) == 2
    principal = next(m for m in methods if m["last4"] == "4321")
    assert principal["brand"] == "Visa"
    assert principal["holder"] == "MARIA LEITORA"


def test_register_marks_the_first_as_default_when_none_marked(client):
    cid = register_customer(client, **_payload())["id"]
    addrs = client.get(f"/customers/{cid}/addresses").json()
    defaults = [a for a in addrs if a["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["label"] == "Casa"


def test_register_keeps_only_one_default(client):
    payload = _payload()
    payload["addresses"][0]["is_default"] = True
    payload["addresses"][1]["is_default"] = True
    cid = register_customer(client, **payload)["id"]
    addrs = client.get(f"/customers/{cid}/addresses").json()
    assert len([a for a in addrs if a["is_default"]]) == 1


def test_register_requires_a_password(client):
    payload = _payload()
    del payload["password"]
    assert client.post("/customers/register", json=payload).status_code == 422


def test_register_rejects_duplicate_email(client, admin_headers):
    payload = _payload(email=f"dup-{uuid.uuid4().hex[:8]}@ex.com")
    assert client.post("/customers/register", json=payload).status_code == 201
    assert client.post("/customers/register", json=payload).status_code == 409
    # não duplicou: o admin vê um só cliente com esse e-mail
    rows = client.get("/admin/customers", headers=admin_headers).json()
    assert sum(1 for c in rows if c["email"] == payload["email"]) == 1


def test_register_works_without_addresses_or_methods(client):
    c = register_customer(
        client, name="Só Dados", email=f"min-{uuid.uuid4().hex[:8]}@ex.com"
    )
    assert client.get(f"/customers/{c['id']}/addresses").json() == []


def test_registered_customer_can_enroll_and_pay_via_jsr(client, fake_initiator):
    """Cliente autocadastrado vincula o dispositivo e paga sem redirect."""
    from tests.conftest import _make_product, enroll_and_register

    cid = register_customer(client, **_payload())["id"]
    enroll_and_register(client, fake_initiator, cid)

    token = client.post("/admin/login", json={"password": "admin123"}).json()["token"]
    product = _make_product(client, {"Authorization": f"Bearer {token}"}, price=25.0)
    client.post(f"/cart/{cid}/items", json={"product_id": product["id"], "quantity": 1})

    order = client.post("/orders/checkout", json={"customer_id": cid}).json()
    assert order["status"] == "PAID"
    assert order["payment_id"] == "pay-test"
