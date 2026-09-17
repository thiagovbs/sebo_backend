"""Vitrine: busca, filtro de categoria e ordenação — o que o cliente controla."""


def test_search_by_name_is_partial_and_case_insensitive(client):
    r = client.get("/products", params={"q": "dom cas"})
    nomes = [p["name"] for p in r.json()["items"]]
    assert "Dom Casmurro" in nomes


def test_filter_by_category(client):
    r = client.get("/products", params={"category": "Eletrônicos"})
    assert r.json()["total"] >= 1
    assert all(p["category"] == "Eletrônicos" for p in r.json()["items"])


def test_sort_by_lowest_price_is_ascending(client):
    precos = [p["price"] for p in client.get("/products", params={"sort": "menor_preco"}).json()["items"]]
    assert precos == sorted(precos)


def test_sort_by_highest_price_is_descending(client):
    precos = [p["price"] for p in client.get("/products", params={"sort": "maior_preco"}).json()["items"]]
    assert precos == sorted(precos, reverse=True)


def test_categories_endpoint_lists_distinct_categories(client):
    cats = client.get("/products/categories").json()
    assert "Eletrônicos" in cats
    assert len(cats) == len(set(cats))


def test_create_product_registers_the_showcase_options(client, admin_headers):
    novo = {
        "name": "Produto de Teste",
        "category": "Teste",
        "price": 10.5,
        "stock": 3,
        "brand": "Marca Teste",
        "condition": "novo",
    }
    r = client.post("/products", json=novo, headers=admin_headers)
    assert r.status_code == 201
    criado = r.json()
    assert criado["category"] == "Teste"
    # e passa a aparecer no filtro por categoria
    assert "Teste" in client.get("/products/categories").json()


def test_soft_delete_hides_from_the_showcase(client, admin_headers):
    pid = client.post(
        "/products",
        headers=admin_headers,
        json={"name": "Efêmero", "category": "X", "price": 1, "stock": 1},
    ).json()["id"]
    assert client.delete(f"/products/{pid}", headers=admin_headers).status_code == 204
    ids = [p["id"] for p in client.get("/products", params={"page_size": 100}).json()["items"]]
    assert pid not in ids
    # mas ainda é acessível diretamente (preserva histórico)
    assert client.get(f"/products/{pid}").json()["active"] is False


def test_pagination_reports_total_independent_of_page_size(client):
    total = client.get("/products", params={"page_size": 1}).json()["total"]
    assert total >= 16


# -- Upload de imagem (base64) ----------------------------------------------


def test_upload_image_returns_a_base64_data_uri(client, admin_headers):
    import base64

    raw = b"\x89PNG\r\n\x1a\n bytes de imagem de teste"
    files = {"file": ("foto.png", raw, "image/png")}
    r = client.post("/products/upload-image", files=files, headers=admin_headers)
    assert r.status_code == 200
    data_uri = r.json()["image_url"]
    assert data_uri.startswith("data:image/png;base64,")
    # o base64 embutido decodifica de volta para os bytes originais
    b64 = data_uri.split(",", 1)[1]
    assert base64.b64decode(b64) == raw


def test_upload_image_can_be_stored_on_a_product(client, admin_headers):
    files = {"file": ("foto.png", b"conteudo", "image/png")}
    data_uri = client.post("/products/upload-image", files=files, headers=admin_headers).json()["image_url"]
    pid = client.post(
        "/products",
        headers=admin_headers,
        json={"name": "Com foto", "price": 5, "category": "X", "image_url": data_uri},
    ).json()["id"]
    assert client.get(f"/products/{pid}").json()["image_url"] == data_uri


def test_upload_image_requires_admin(client):
    files = {"file": ("foto.png", b"x", "image/png")}
    assert client.post("/products/upload-image", files=files).status_code == 401


def test_upload_rejects_non_image(client, admin_headers):
    files = {"file": ("doc.txt", b"hello", "text/plain")}
    r = client.post("/products/upload-image", files=files, headers=admin_headers)
    assert r.status_code == 400
