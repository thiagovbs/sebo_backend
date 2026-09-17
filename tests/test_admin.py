"""Área administrativa: métricas de venda, pedidos e clientes."""


def _add(client, customer_id, product, qty=1):
    return client.post(
        f"/cart/{customer_id}/items", json={"product_id": product["id"], "quantity": qty}
    )


def _paid_order(client, customer, product, fake_initiator, qty=1):
    from tests.conftest import enroll_and_register

    enroll_and_register(client, fake_initiator, customer["id"])
    _add(client, customer["id"], product, qty)
    # checkout via JSR já devolve o pedido pago
    return client.post("/orders/checkout", json={"customer_id": customer["id"]}).json()


def test_stats_counts_revenue_only_from_paid_orders(client, customer, product, fake_initiator, admin_headers):
    _paid_order(client, customer, product, fake_initiator, qty=2)

    # Um cliente sem dispositivo tenta comprar: 409, nenhum pedido criado, e a
    # receita não é inflada por tentativas que não viraram pagamento.
    from tests.conftest import register_customer

    outro = register_customer(client, name="Sem Device")  # troca o client p/ ele
    _add(client, outro["id"], product, 1)
    assert client.post("/orders/checkout", json={"customer_id": outro["id"]}).status_code == 409

    stats = client.get("/admin/stats", headers=admin_headers).json()
    assert stats["kpis"]["revenue"] == round(product["price"] * 2, 2)
    assert stats["kpis"]["paid_orders"] == 1
    assert stats["kpis"]["total_orders"] == 1
    assert stats["orders_by_status"]["PAID"] == 1


def test_stats_revenue_by_day_covers_the_window(client, admin_headers):
    stats = client.get("/admin/stats", params={"days": 7}, headers=admin_headers).json()
    assert len(stats["revenue_by_day"]) == 7
    # a série vem ordenada por data crescente
    datas = [d["date"] for d in stats["revenue_by_day"]]
    assert datas == sorted(datas)


def test_stats_top_products_ranks_by_quantity(client, customer, product, product2, fake_initiator, admin_headers):
    from tests.conftest import enroll_and_register

    enroll_and_register(client, fake_initiator, customer["id"])
    _add(client, customer["id"], product, 3)
    _add(client, customer["id"], product2, 1)
    client.post("/orders/checkout", json={"customer_id": customer["id"]})  # PAID via JSR

    top = client.get("/admin/stats", headers=admin_headers).json()["top_products"]
    assert top[0]["name"] == product["name"]
    assert top[0]["quantity"] == 3


def test_admin_orders_carry_the_customer(client, customer, product, fake_initiator, admin_headers):
    _paid_order(client, customer, product, fake_initiator)
    orders = client.get("/admin/orders", headers=admin_headers).json()
    assert orders[0]["customer"]["email"] == customer["email"]
    assert orders[0]["items"][0]["name"] == product["name"]


def test_admin_customers_aggregate_orders_and_spend(client, customer, product, fake_initiator, admin_headers):
    _paid_order(client, customer, product, fake_initiator, qty=2)
    rows = client.get("/admin/customers", headers=admin_headers).json()
    mine = next(r for r in rows if r["id"] == customer["id"])
    assert mine["paid_count"] == 1
    assert mine["total_spent"] == round(product["price"] * 2, 2)
    assert mine["orders_count"] >= 1


def test_customer_detail_has_registration_data_and_orders(client, product, fake_initiator, admin_headers):
    from tests.conftest import enroll_and_register, register_customer

    # cliente com dados completos (autocadastro) + um pedido pago
    cid = register_customer(
        client,
        name="Cliente Detalhe", email="detalhe@ex.com", cpf="111", birth_date="1990-01-01",
        addresses=[{"label": "Casa", "city": "Campinas", "state": "SP"}],
        payment_methods=[{"label": "Nu", "brand": "Mastercard", "last4": "4321"}],
    )["id"]
    enroll_and_register(client, fake_initiator, cid)
    client.post(f"/cart/{cid}/items", json={"product_id": product["id"], "quantity": 2})
    client.post("/orders/checkout", json={"customer_id": cid})

    detail = client.get(f"/admin/customers/{cid}", headers=admin_headers).json()

    assert detail["customer"]["birth_date"] == "1990-01-01"
    assert len(detail["addresses"]) == 1
    assert detail["payment_methods"][0]["last4"] == "4321"
    assert detail["device"]["status"] == "REGISTERED"
    assert detail["stats"]["paid_count"] == 1
    assert detail["orders"][0]["status"] == "PAID"
    assert detail["orders"][0]["items"][0]["quantity"] == 2


def test_customer_detail_requires_admin(client, customer):
    assert client.get(f"/admin/customers/{customer['id']}").status_code == 401


# -- Controle de acesso -----------------------------------------------------


def test_admin_endpoints_require_a_token(client):
    assert client.get("/admin/stats").status_code == 401
    assert client.get("/admin/orders").status_code == 401
    assert client.get("/admin/customers").status_code == 401


def test_login_rejects_a_wrong_password(client):
    assert client.post("/admin/login", json={"password": "errada"}).status_code == 401


def test_login_returns_a_token_that_opens_the_panel(client):
    token = client.post("/admin/login", json={"password": "admin123"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/admin/stats", headers=headers).status_code == 200


def test_a_tampered_token_is_rejected(client, admin_headers):
    bad = admin_headers["Authorization"] + "x"
    assert client.get("/admin/stats", headers={"Authorization": bad}).status_code == 401


def test_creating_a_product_requires_admin(client):
    r = client.post("/products", json={"name": "X", "price": 1, "category": "Y"})
    assert r.status_code == 401
