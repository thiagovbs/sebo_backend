"""Jornada de pagamento com redirect (consentimento único) sobre o PIX QR.

Num pedido PIX QR em aberto, o cliente pode autorizar o pagamento pelo Open
Finance: a loja cria o consentimento na iniciadora (POST /payments) e leva o
cliente ao banco; na volta, o pedido é reconciliado pela iniciadora.
"""


def _add(client, customer_id, product, qty=1):
    return client.post(
        f"/cart/{customer_id}/items", json={"product_id": product["id"], "quantity": qty}
    )


def _pix_order(client, customer, product):
    _add(client, customer["id"], product)
    return client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "pix_qr"}
    ).json()


def test_status_exposes_redirect_when_configured(client, fake_initiator):
    assert client.get("/open-finance/status").json()["redirect"] is True


def test_start_returns_authorisation_url(client, customer, product, fake_initiator):
    order = _pix_order(client, customer, product)
    r = client.post(f"/orders/{order['id']}/openfinance")
    assert r.status_code == 200
    body = r.json()
    assert body["payment_login_url"].startswith("http://banco/auth/")
    assert body["status"] == "AWAITING_PAYMENT"  # ainda não pago


def test_confirm_before_authorising_keeps_awaiting(client, customer, product, fake_initiator):
    order = _pix_order(client, customer, product)
    client.post(f"/orders/{order['id']}/openfinance")
    r = client.post(f"/orders/{order['id']}/confirm-openfinance")
    assert r.status_code == 200
    assert r.json()["status"] == "AWAITING_PAYMENT"


def test_confirm_after_authorising_marks_paid(client, customer, product, fake_initiator):
    order = _pix_order(client, customer, product)
    client.post(f"/orders/{order['id']}/openfinance")
    fake_initiator.authorise_payment(fake_initiator.last_consent)  # titular aprova

    r = client.post(f"/orders/{order['id']}/confirm-openfinance")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "PAID"
    assert body["payment_status"] == "COMPLETED"


def test_rejected_payment_keeps_order_awaiting(client, customer, product, fake_initiator):
    order = _pix_order(client, customer, product)
    client.post(f"/orders/{order['id']}/openfinance")
    fake_initiator.reject_payment(fake_initiator.last_consent)

    r = client.post(f"/orders/{order['id']}/confirm-openfinance")
    assert r.json()["status"] == "AWAITING_PAYMENT"  # pode tentar de novo/copiar
    assert r.json()["payment_status"] == "REJECTED"


def test_confirm_without_starting_is_rejected(client, customer, product, fake_initiator):
    order = _pix_order(client, customer, product)
    r = client.post(f"/orders/{order['id']}/confirm-openfinance")
    assert r.status_code == 409


def test_start_on_a_missing_order_is_404(client, customer, fake_initiator):
    assert client.post("/orders/99999/openfinance").status_code == 404


def test_start_when_initiator_is_down_returns_502(client, customer, product, fake_initiator):
    order = _pix_order(client, customer, product)
    fake_initiator.down = True
    assert client.post(f"/orders/{order['id']}/openfinance").status_code == 502
