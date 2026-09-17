"""Carrinho e checkout: pagamento sem redirect pela jornada JSR.

O cliente precisa ter um dispositivo vinculado (REGISTERED) para pagar. Cada
teste usa produtos próprios (fixtures ``product``/``product2``, estoque folgado).
"""

from tests.conftest import enroll_and_register


def _add(client, customer_id, product, qty=1):
    return client.post(
        f"/cart/{customer_id}/items", json={"product_id": product["id"], "quantity": qty}
    )


def test_cart_sums_line_totals(client, customer, product, product2):
    _add(client, customer["id"], product, 1)
    cart = _add(client, customer["id"], product2, 2).json()
    esperado = round(product["price"] * 1 + product2["price"] * 2, 2)
    assert cart["total"] == esperado


def test_adding_the_same_product_accumulates_quantity(client, customer, product):
    _add(client, customer["id"], product, 1)
    cart = _add(client, customer["id"], product, 2).json()
    assert len(cart["items"]) == 1
    assert cart["items"][0]["quantity"] == 3


def test_cannot_add_more_than_stock(client, customer, product):
    r = _add(client, customer["id"], product, product["stock"] + 1)
    assert r.status_code == 409


def test_checkout_without_a_device_asks_for_enrollment(client, customer, product, fake_initiator):
    _add(client, customer["id"], product, 1)
    r = client.post("/orders/checkout", json={"customer_id": customer["id"]})
    assert r.status_code == 409
    assert r.json()["detail"]["need_enrollment"] is True
    # nada foi debitado, carrinho intacto
    assert client.get(f"/cart/{customer['id']}").json()["total"] > 0


def test_checkout_with_registered_device_pays_and_completes(client, customer, product, fake_initiator):
    enroll_and_register(client, fake_initiator, customer["id"])
    estoque_antes = client.get(f"/products/{product['id']}").json()["stock"]
    _add(client, customer["id"], product, 1)

    order = client.post("/orders/checkout", json={"customer_id": customer["id"]}).json()
    # JSR conclui na hora: pedido já pago, sem redirect.
    assert order["status"] == "PAID"
    assert order["payment_id"] == "pay-test"
    assert order["payment_login_url"] == ""

    assert client.get(f"/products/{product['id']}").json()["stock"] == estoque_antes - 1
    assert client.get(f"/cart/{customer['id']}").json()["total"] == 0


def test_checkout_with_pending_device_asks_to_finish_enrollment(client, customer, product, fake_initiator):
    # Vincula mas NÃO ativa (cliente não concluiu no banco).
    client.post(f"/customers/{customer['id']}/device/enroll")
    _add(client, customer["id"], product, 1)

    r = client.post("/orders/checkout", json={"customer_id": customer["id"]})
    assert r.status_code == 409
    assert r.json()["detail"]["need_enrollment"] is True
    assert client.get(f"/cart/{customer['id']}").json()["total"] > 0


def test_checkout_with_empty_cart_is_rejected(client, customer, fake_initiator):
    enroll_and_register(client, fake_initiator, customer["id"])
    r = client.post("/orders/checkout", json={"customer_id": customer["id"]})
    assert r.status_code == 400


def test_when_initiator_is_down_nothing_is_persisted(client, customer, product, fake_initiator):
    enroll_and_register(client, fake_initiator, customer["id"])
    fake_initiator.down = True
    estoque_antes = client.get(f"/products/{product['id']}").json()["stock"]
    _add(client, customer["id"], product, 1)

    r = client.post("/orders/checkout", json={"customer_id": customer["id"]})
    assert r.status_code == 502

    assert client.get(f"/products/{product['id']}").json()["stock"] == estoque_antes
    assert client.get(f"/cart/{customer['id']}").json()["total"] > 0
    assert client.get(f"/customers/{customer['id']}/orders").json() == []


def test_checkout_pix_qr_returns_a_br_code_and_awaits_payment(client, customer, product, fake_initiator):
    estoque_antes = client.get(f"/products/{product['id']}").json()["stock"]
    _add(client, customer["id"], product, 1)

    order = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "pix_qr"}
    ).json()
    assert order["status"] == "AWAITING_PAYMENT"
    assert order["payment_method"] == "pix_qr"
    assert order["pix_code"].startswith("000201")
    # estoque reservado (baixado) e carrinho esvaziado
    assert client.get(f"/products/{product['id']}").json()["stock"] == estoque_antes - 1
    assert client.get(f"/cart/{customer['id']}").json()["total"] == 0


def test_pix_qr_does_not_require_a_device(client, customer, product, fake_initiator):
    # sem enroll: JSR daria 409; pix_qr funciona.
    _add(client, customer["id"], product, 1)
    r = client.post("/orders/checkout", json={"customer_id": customer["id"], "method": "pix_qr"})
    assert r.status_code == 201


def test_confirm_pix_marks_the_order_paid(client, customer, product, fake_initiator):
    _add(client, customer["id"], product, 1)
    order = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "pix_qr"}
    ).json()
    paid = client.post(f"/orders/{order['id']}/confirm-pix").json()
    assert paid["status"] == "PAID"
    assert paid["payment_status"] == "COMPLETED"


def test_pix_qr_blocked_when_recebedor_not_configured(client, customer, product, admin_headers):
    # limpa a config: sem recebedor, o PIX QR fica indisponível
    client.put("/admin/integration", headers=admin_headers, json={
        "payment_initiator_url": "", "payment_initiator_user": "", "payment_initiator_password": "",
        "sebo_name": "", "sebo_cpf_cnpj": "", "sebo_city": "", "sebo_pix_key_type": "CNPJ", "sebo_pix_key_value": "",
    })
    _add(client, customer["id"], product, 1)
    r = client.post("/orders/checkout", json={"customer_id": customer["id"], "method": "pix_qr"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "PIX_QR_UNAVAILABLE"


def test_cancel_is_blocked_on_a_paid_order(client, customer, product, fake_initiator):
    enroll_and_register(client, fake_initiator, customer["id"])
    _add(client, customer["id"], product, 1)
    order = client.post("/orders/checkout", json={"customer_id": customer["id"]}).json()

    r = client.post(f"/orders/{order['id']}/cancel")
    assert r.status_code == 409  # já foi pago
