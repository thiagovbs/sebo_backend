"""Vínculo do dispositivo do cliente (jornada JSR)."""


def test_device_starts_unenrolled(client, customer, fake_initiator):
    d = client.get(f"/customers/{customer['id']}/device").json()
    assert d["enrolled"] is False
    assert d["status"] is None


def test_enroll_returns_login_url_and_marks_pending(client, customer, fake_initiator):
    r = client.post(f"/customers/{customer['id']}/device/enroll")
    assert r.status_code == 200
    body = r.json()
    assert body["login_url"] == "http://banco/enroll"
    assert body["enrollment_id"]

    d = client.get(f"/customers/{customer['id']}/device").json()
    assert d["enrolled"] is True
    assert d["status"] == "PENDING"


def test_device_syncs_to_registered_after_bank_auth(client, customer, fake_initiator):
    enrollment_id = client.post(f"/customers/{customer['id']}/device/enroll").json()["enrollment_id"]
    # cliente conclui no banco:
    fake_initiator.register(enrollment_id)

    d = client.get(f"/customers/{customer['id']}/device").json()
    assert d["status"] == "REGISTERED"
    assert d["account_id"] == f"acc-{enrollment_id}"


def test_reenroll_replaces_the_previous_device(client, customer, fake_initiator):
    first = client.post(f"/customers/{customer['id']}/device/enroll").json()["enrollment_id"]
    second = client.post(f"/customers/{customer['id']}/device/enroll").json()["enrollment_id"]
    assert first != second
    d = client.get(f"/customers/{customer['id']}/device").json()
    assert d["enrollment_id"] == second
    assert d["status"] == "PENDING"


def test_enroll_when_initiator_is_down_returns_502(client, customer, fake_initiator):
    fake_initiator.down = True
    r = client.post(f"/customers/{customer['id']}/device/enroll")
    assert r.status_code == 502


def test_admin_can_delete_a_customers_authorization(client, customer, fake_initiator, admin_headers):
    eid = client.post(f"/customers/{customer['id']}/device/enroll").json()["enrollment_id"]
    fake_initiator.register(eid)
    assert client.get(f"/customers/{customer['id']}/device").json()["status"] == "REGISTERED"

    r = client.delete(f"/admin/customers/{customer['id']}/device", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["enrolled"] is False

    # Depois de apagada, o cliente volta a "não autorizado" e pode re-autorizar.
    assert client.get(f"/customers/{customer['id']}/device").json()["enrolled"] is False


def test_delete_device_requires_admin(client, customer, fake_initiator):
    client.post(f"/customers/{customer['id']}/device/enroll")
    # Sem token de admin (o header padrão é o do cliente) -> negado.
    assert client.delete(f"/admin/customers/{customer['id']}/device").status_code == 401
