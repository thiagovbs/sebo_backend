"""Autenticação do cliente: login com e-mail/senha, /me e proteção das rotas."""

import uuid


def _register_raw(client, password="segredo123"):
    """Registra e devolve (email, senha), limpando o auth padrão do client."""
    email = f"u-{uuid.uuid4().hex[:8]}@ex.com"
    client.post(
        "/customers/register",
        json={"name": "User", "email": email, "password": password},
    )
    client.headers.pop("Authorization", None)
    return email, password


def test_login_returns_token_and_customer(client):
    email, pwd = _register_raw(client)
    r = client.post("/customers/login", json={"email": email, "password": pwd})
    assert r.status_code == 200
    body = r.json()
    assert body["customer"]["email"] == email
    assert body["token"]


def test_login_rejects_wrong_password(client):
    email, _ = _register_raw(client)
    r = client.post("/customers/login", json={"email": email, "password": "errada"})
    assert r.status_code == 401


def test_login_rejects_unknown_email(client):
    r = client.post("/customers/login", json={"email": "nao@existe.com", "password": "x"})
    assert r.status_code == 401


def test_me_requires_a_token(client):
    client.headers.pop("Authorization", None)
    assert client.get("/customers/me").status_code == 401


def test_me_returns_the_logged_in_customer(client, customer):
    # a fixture 'customer' já autenticou o client
    me = client.get("/customers/me").json()
    assert me["id"] == customer["id"]
    assert "password_hash" not in me


def test_customer_cannot_read_another_customers_addresses(client, customer):
    r = client.get(f"/customers/{customer['id'] + 99999}/addresses")
    assert r.status_code == 403


def test_cart_requires_authentication(client, customer):
    client.headers.pop("Authorization", None)
    assert client.get(f"/cart/{customer['id']}").status_code == 401


def test_checkout_requires_authentication(client, customer):
    client.headers.pop("Authorization", None)
    assert client.post("/orders/checkout", json={"customer_id": customer["id"]}).status_code == 401
