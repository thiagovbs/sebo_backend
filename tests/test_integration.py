"""Config de integração (admin) e disponibilidade do Open Finance p/ o cliente."""


def _put(client, headers, **fields):
    body = {
        "payment_initiator_url": "",
        "payment_initiator_user": "",
        "payment_initiator_password": "",
        "sebo_name": "",
        "sebo_cpf_cnpj": "",
        "sebo_pix_key_type": "CNPJ",
        "sebo_pix_key_value": "",
    }
    body.update(fields)
    return client.put("/admin/integration", json=body, headers=headers)


def _configure(client, headers, password="secret"):
    return _put(
        client, headers,
        payment_initiator_url="http://iniciadora",
        payment_initiator_user="sebo_online",
        payment_initiator_password=password,
        sebo_name="Sebo On-Line",
        sebo_cpf_cnpj="12345678000199",
        sebo_pix_key_value="12345678000199",
    )


def test_status_is_false_when_not_configured(client, admin_headers):
    _put(client, admin_headers)  # tudo vazio
    assert client.get("/open-finance/status").json()["available"] is False


def test_status_is_true_after_configuring(client, admin_headers):
    _configure(client, admin_headers)
    assert client.get("/open-finance/status").json()["available"] is True


def test_get_integration_never_returns_the_password(client, admin_headers):
    _configure(client, admin_headers, password="super-secreta")
    out = client.get("/admin/integration", headers=admin_headers).json()
    assert "super-secreta" not in str(out)
    assert "payment_initiator_password" not in out
    assert out["has_password"] is True
    assert out["configured"] is True


def test_password_is_kept_when_editing_without_resending_it(client, admin_headers):
    _configure(client, admin_headers, password="mantida")
    # edita a URL sem reenviar a senha (campo vazio)
    _put(
        client, admin_headers,
        payment_initiator_url="http://outra",
        payment_initiator_user="sebo_online",
        sebo_name="Sebo On-Line",
        sebo_cpf_cnpj="12345678000199",
        sebo_pix_key_value="12345678000199",
    )
    out = client.get("/admin/integration", headers=admin_headers).json()
    assert out["has_password"] is True
    assert out["configured"] is True
    assert out["payment_initiator_url"] == "http://outra"


def test_integration_endpoints_require_admin(client):
    assert client.get("/admin/integration").status_code == 401
    assert client.put("/admin/integration", json={}).status_code == 401


def test_enroll_is_blocked_when_not_configured(client, customer, admin_headers):
    _put(client, admin_headers)  # limpa a config
    r = client.post(f"/customers/{customer['id']}/device/enroll")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "OPEN_FINANCE_UNAVAILABLE"


def test_checkout_is_blocked_when_not_configured(client, customer, product, admin_headers):
    _put(client, admin_headers)  # limpa a config
    client.post(f"/cart/{customer['id']}/items", json={"product_id": product["id"], "quantity": 1})
    r = client.post("/orders/checkout", json={"customer_id": customer["id"]})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "OPEN_FINANCE_UNAVAILABLE"
