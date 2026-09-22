"""Boleto: a aritmética FEBRABAN de verdade, e o checkout que a usa.

A aritmética tem um teste que vale mais que os outros: a volta. Dado a linha
digitável, reconstruir o código de barras e conferir os quatro dígitos
verificadores é o que um app de banco faz ao ler o número — se a volta fecha, o
boleto é bem formado. O resto dos testes protege os limites.
"""

from datetime import date, timedelta

import pytest

from app import boleto
from app.boleto import BoletoError


def _add(client, customer_id, product, qty=1):
    return client.post(
        f"/cart/{customer_id}/items", json={"product_id": product["id"], "quantity": qty}
    )


def _integracao_completa(**over):
    corpo = {
        "payment_initiator_url": "http://fake-initiator",
        "payment_initiator_user": "sebo_online",
        "payment_initiator_password": "x",
        "sebo_name": "Sebo On-Line LTDA",
        "sebo_cpf_cnpj": "12345678000199",
        "sebo_city": "SAO PAULO",
        "sebo_pix_key_type": "CNPJ",
        "sebo_pix_key_value": "12345678000199",
        "boleto_bank_code": "341",
        "boleto_bank_name": "Itau Unibanco",
        "boleto_agency": "1234",
        "boleto_account": "567890",
        "boleto_wallet": "109",
        "boleto_days_to_due": 3,
        "boleto_instructions": "Nao receber apos o vencimento.",
        "sebo_street": "Rua das Livrarias",
        "sebo_number": "100",
        "sebo_district": "Centro",
        "sebo_state": "SP",
        "sebo_zip_code": "01001-000",
    }
    corpo.update(over)
    return corpo


def _configurar(client, admin_headers, **over):
    r = client.put(
        "/admin/integration", headers=admin_headers, json=_integracao_completa(**over)
    )
    assert r.status_code == 200, r.text
    return r.json()


# --- Aritmética ------------------------------------------------------------


def test_fator_de_vencimento_usa_a_base_vigente():
    # A contagem original (07/10/1997) chegou a 9999 em 21/02/2025 e reiniciou
    # em 1000 no dia seguinte. Código que ainda somasse sobre a base antiga
    # produziria hoje um fator que não cabe em quatro posições.
    assert boleto.fator_de_vencimento(date(2025, 2, 22)) == 1000
    assert boleto.fator_de_vencimento(date(2025, 2, 23)) == 1001
    assert boleto.fator_de_vencimento(date(2026, 9, 24)) == 1579


def test_vencimento_fora_do_alcance_do_fator_e_recusado():
    with pytest.raises(BoletoError, match="anterior"):
        boleto.fator_de_vencimento(date(2025, 2, 21))
    with pytest.raises(BoletoError, match="além"):
        boleto.fator_de_vencimento(date(2025, 2, 22) + timedelta(days=9000))


def test_modulo10_e_modulo11_em_vetores_conhecidos():
    # Módulo 10: pesos 2 e 1 da direita para a esquerda, somando os algarismos
    # do produto quando ele passa de 9.
    assert boleto.modulo10("341912341") == 3
    # Módulo 11 do código de barras: resto 0, 1 ou 10 dá DV 1, por regra do
    # padrão -- e não por arredondamento nosso.
    assert boleto.modulo11_barras("0" * 43) == 1


def test_campo_livre_recusa_dado_maior_que_o_layout():
    # Cortar em silêncio produziria um boleto com a conta de outra pessoa.
    with pytest.raises(BoletoError, match="Agência"):
        boleto.campo_livre(
            agencia="12345", conta="1", carteira="1", nosso_numero="1"
        )
    with pytest.raises(BoletoError, match="Conta"):
        boleto.campo_livre(
            agencia="1", conta="12345678", carteira="1", nosso_numero="1"
        )
    assert len(
        boleto.campo_livre(agencia="1234", conta="567890", carteira="109", nosso_numero="42")
    ) == 25


def test_valor_precisa_ser_positivo_e_caber_no_codigo():
    livre = boleto.campo_livre(agencia="1", conta="1", carteira="1", nosso_numero="1")
    with pytest.raises(BoletoError, match="positivo"):
        boleto.codigo_de_barras(
            banco="341", valor=0, vencimento=date(2026, 10, 1), livre=livre
        )
    # R$ 99.999.999,99 são exatamente os 10 dígitos de centavos: cabe.
    assert boleto.codigo_de_barras(
        banco="341", valor=99_999_999.99, vencimento=date(2026, 10, 1), livre=livre
    )[9:19] == "9999999999"
    with pytest.raises(BoletoError, match="acima"):
        boleto.codigo_de_barras(
            banco="341", valor=100_000_000.00, vencimento=date(2026, 10, 1), livre=livre
        )


def test_emissao_tem_44_e_47_digitos_e_vencimento_pelo_prazo():
    doc = boleto.emitir(
        order_id=42, valor=159.80, banco="341", agencia="1234", conta="567890",
        carteira="109", dias_para_vencer=3, hoje=date(2026, 9, 21),
    )
    assert len(doc["barcode"]) == 44 and doc["barcode"].isdigit()
    assert len(doc["digitable_line"]) == 47 and doc["digitable_line"].isdigit()
    assert doc["due_date"] == "2026-09-24"
    assert doc["our_number"] == "00000000042"
    # A linha impressa tem a pontuação, e os mesmos dígitos.
    assert doc["digitable_line_formatted"].replace(".", "").replace(" ", "") == doc["digitable_line"]


def test_o_valor_em_centavos_ocupa_as_dez_posicoes_certas():
    doc = boleto.emitir(
        order_id=1, valor=159.80, banco="341", agencia="1234", conta="567890",
        carteira="109", dias_para_vencer=0, hoje=date(2026, 9, 21),
    )
    # Posições 10 a 19 (base 1) são o valor em centavos.
    assert doc["barcode"][9:19] == "0000015980"


def _linha_para_barras(linha: str) -> str:
    """Reconstrói o código de barras a partir da linha digitável.

    É o caminho inverso, escrito aqui de propósito: se a ida e a volta usassem
    o mesmo código, o teste provaria apenas que ele é consistente consigo mesmo.
    """
    campo1, dv1 = linha[0:9], linha[9]
    campo2, dv2 = linha[10:20], linha[20]
    campo3, dv3 = linha[21:31], linha[31]
    dv_geral = linha[32]
    fator_valor = linha[33:47]
    assert boleto.modulo10(campo1) == int(dv1)
    assert boleto.modulo10(campo2) == int(dv2)
    assert boleto.modulo10(campo3) == int(dv3)
    barras = campo1[0:4] + dv_geral + fator_valor + campo1[4:9] + campo2 + campo3
    assert boleto.modulo11_barras(barras[0:4] + barras[5:]) == int(dv_geral)
    return barras


def test_a_volta_fecha_a_ida():
    for order_id, valor, dias in ((1, 10.0, 0), (42, 159.80, 3), (98765, 1234.56, 30)):
        doc = boleto.emitir(
            order_id=order_id, valor=valor, banco="341", agencia="1234",
            conta="567890", carteira="109", dias_para_vencer=dias,
            hoje=date(2026, 9, 21),
        )
        # Um app de banco lê a linha digitável e remonta o código de barras;
        # se os quatro DVs fecham, o número é bem formado.
        assert _linha_para_barras(doc["digitable_line"]) == doc["barcode"]


# --- Checkout --------------------------------------------------------------


def test_boleto_indisponivel_diz_o_que_falta(client, admin_headers, customer, product):
    _configurar(client, admin_headers, boleto_agency="", boleto_account="")
    _add(client, customer["id"], product, 1)
    r = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "boleto"}
    )
    assert r.status_code == 409
    detalhe = r.json()["detail"]
    assert detalhe["error"] == "BOLETO_UNAVAILABLE"
    # O admin não precisa adivinhar qual campo está vazio.
    assert "Agência" in detalhe["message"] and "Conta" in detalhe["message"]


def test_status_reporta_boleto_conforme_a_configuracao(client, admin_headers):
    _configurar(client, admin_headers, boleto_wallet="")
    assert client.get("/open-finance/status").json()["boleto"] is False
    saida = _configurar(client, admin_headers)
    assert saida["boleto_available"] is True
    assert saida["boleto_missing"] == []
    assert client.get("/open-finance/status").json()["boleto"] is True


def test_checkout_com_boleto_gera_linha_e_baixa_estoque(
    client, admin_headers, customer, product
):
    _configurar(client, admin_headers)
    _add(client, customer["id"], product, 2)
    antes = client.get(f"/products/{product['id']}").json()["stock"]

    r = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "boleto"}
    )
    assert r.status_code == 201, r.text
    pedido = r.json()
    # O dinheiro chega depois: o pedido nasce aguardando pagamento.
    assert pedido["status"] == "AWAITING_PAYMENT"
    assert pedido["payment_method"] == "boleto"
    assert len(pedido["boleto_digitable_line"]) == 47
    assert len(pedido["boleto_barcode"]) == 44
    assert pedido["boleto_digitable_line_formatted"].count(" ") == 4
    assert pedido["boleto_due_date"]
    assert pedido["boleto_our_number"].endswith(str(pedido["id"]))

    # A reserva acontece quando o pedido nasce, como no PIX QR.
    assert client.get(f"/products/{product['id']}").json()["stock"] == antes - 2
    assert client.get(f"/cart/{customer['id']}").json()["items"] == []


def test_a_linha_gravada_e_valida_de_verdade(client, admin_headers, customer, product):
    _configurar(client, admin_headers)
    _add(client, customer["id"], product, 1)
    pedido = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "boleto"}
    ).json()
    assert _linha_para_barras(pedido["boleto_digitable_line"]) == pedido["boleto_barcode"]


def test_prazo_de_vencimento_vem_da_configuracao(
    client, admin_headers, customer, product
):
    _configurar(client, admin_headers, boleto_days_to_due=10)
    _add(client, customer["id"], product, 1)
    pedido = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "boleto"}
    ).json()
    esperado = (date.today() + timedelta(days=10)).isoformat()
    assert pedido["boleto_due_date"] == esperado


def test_o_admin_recusa_agencia_com_digito_demais_ao_salvar(client, admin_headers):
    """A recusa acontece onde o dado é digitado, não no carrinho do cliente."""
    _configurar(client, admin_headers)  # parte de uma configuração boa
    r = client.put(
        "/admin/integration",
        headers=admin_headers,
        json=_integracao_completa(boleto_agency="123456"),
    )
    assert r.status_code == 409
    detalhe = r.json()["detail"]
    assert detalhe["error"] == "BOLETO_INVALID_SETTINGS"
    # A mensagem nomeia o campo: é ela que o painel mostra.
    assert "Agência" in detalhe["message"]

    # E nada foi gravado: a configuração boa continua valendo.
    atual = client.get("/admin/integration", headers=admin_headers).json()
    assert atual["boleto_agency"] == "1234"
    assert atual["boleto_available"] is True


def test_o_admin_recusa_banco_com_digito_demais_ao_salvar(client, admin_headers):
    """O código do banco só estourava depois de o pedido existir."""
    r = client.put(
        "/admin/integration",
        headers=admin_headers,
        json=_integracao_completa(boleto_bank_code="0341"),
    )
    assert r.status_code == 409
    assert "Banco" in r.json()["detail"]["message"]


def test_configuracao_fora_de_forma_nao_anuncia_boleto_disponivel(
    client, admin_headers, customer, product
):
    """Disponibilidade e emissão passam a usar a mesma conta.

    Simula o que já está gravado num ambiente configurado antes desta
    validação existir: o campo tem conteúdo, mas não cabe no boleto. Antes, o
    painel dizia "boleto ativo", o cliente via a opção e o checkout recusava.
    """
    from sqlmodel import Session

    from app.db import engine
    from app.integration import boleto_available, boleto_missing, get_integration

    _configurar(client, admin_headers)
    with Session(engine) as session:
        cfg = get_integration(session)
        cfg.boleto_agency = "123456"  # burla o admin, como um registro antigo
        session.add(cfg)
        session.commit()
        assert boleto_available(cfg) is False
        assert any("Agência" in p for p in boleto_missing(cfg))

    # O cliente não recebe a opção...
    assert client.get("/open-finance/status").json()["boleto"] is False
    # ...e, se tentar mesmo assim, nenhum pedido fica pela metade.
    _add(client, customer["id"], product, 1)
    antes = len(client.get("/orders").json())
    r = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "boleto"}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] in ("BOLETO_UNAVAILABLE", "BOLETO_INVALID_SETTINGS")
    assert len(client.get("/orders").json()) == antes


def test_a_loja_confirma_o_boleto_e_o_pedido_fica_pago(
    client, admin_headers, customer, product
):
    _configurar(client, admin_headers)
    _add(client, customer["id"], product, 1)
    pedido = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "boleto"}
    ).json()

    r = client.post(
        f"/admin/orders/{pedido['id']}/confirm-payment", headers=admin_headers
    )
    assert r.status_code == 200
    assert r.json()["status"] == "PAID"
    # Idempotente: dois cliques no botão não podem virar dois avisos de venda.
    repetido = client.post(
        f"/admin/orders/{pedido['id']}/confirm-payment", headers=admin_headers
    ).json()
    assert repetido == {"id": pedido["id"], "status": "PAID", "already_paid": True}


def test_o_cliente_nao_confirma_o_boleto(client, admin_headers, customer, product):
    """A confirmação é da loja: é ela que vê o dinheiro entrar."""
    _configurar(client, admin_headers)
    _add(client, customer["id"], product, 1)
    pedido = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "boleto"}
    ).json()
    # Sem rota no cliente, e a do admin não aceita o token dele.
    assert client.post(f"/orders/{pedido['id']}/confirm-boleto").status_code == 404
    assert client.post(f"/admin/orders/{pedido['id']}/confirm-payment").status_code in (
        401,
        403,
    )
    assert client.get(f"/orders/{pedido['id']}").json()["status"] == "AWAITING_PAYMENT"


def test_a_listagem_do_admin_diz_o_que_tem_pagamento_a_confirmar(
    client, admin_headers, customer, product
):
    _configurar(client, admin_headers)
    _add(client, customer["id"], product, 1)
    pedido = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "boleto"}
    ).json()

    linha = next(
        o
        for o in client.get("/admin/orders", headers=admin_headers).json()
        if o["id"] == pedido["id"]
    )
    assert linha["payment_method"] == "boleto"
    assert linha["awaiting_confirmation"] is True
    assert linha["boleto_due_date"] == pedido["boleto_due_date"]

    client.post(f"/admin/orders/{pedido['id']}/confirm-payment", headers=admin_headers)
    linha = next(
        o
        for o in client.get("/admin/orders", headers=admin_headers).json()
        if o["id"] == pedido["id"]
    )
    assert linha["awaiting_confirmation"] is False


def test_confirmacao_manual_nao_vale_para_cartao(
    client, admin_headers, customer, product, fake_initiator
):
    """Cartão nasce pago: não há confirmação a registrar."""
    _configurar(client, admin_headers)
    _add(client, customer["id"], product, 1)
    pedido = client.post(
        "/orders/checkout",
        json={"customer_id": customer["id"], "method": "card",
              "card": {"number": "4111111111111111", "holder": "F",
                       "expiry": "12/30", "cvv": "123"}},
    ).json()
    r = client.post(
        f"/admin/orders/{pedido['id']}/confirm-payment", headers=admin_headers
    )
    assert r.status_code == 409
    assert "card" in r.json()["detail"]


def test_confirmar_pedido_inexistente_e_404(client, admin_headers):
    r = client.post("/admin/orders/999999/confirm-payment", headers=admin_headers)
    assert r.status_code == 404


# --- Ficha do boleto (GET /orders/{id}/boleto) -----------------------------


def _pedido_com_boleto(client, admin_headers, customer, product):
    _configurar(client, admin_headers)
    _add(client, customer["id"], product, 1)
    r = client.post(
        "/orders/checkout", json={"customer_id": customer["id"], "method": "boleto"}
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_a_ficha_traz_beneficiario_pagador_e_o_mesmo_numero(
    client, admin_headers, customer, product
):
    """O que a ficha acrescenta ao pedido é justamente o que não cabia nele:
    quem recebe (que mora na config de admin) e quem paga."""
    endereco = client.post(
        f"/customers/{customer['id']}/addresses",
        json={"street": "Rua A", "number": "10", "complement": "ap 2",
              "district": "Centro", "city": "Campinas", "state": "SP",
              "zip_code": "13000-000"},
    ).json()
    pedido = _pedido_com_boleto(client, admin_headers, customer, product)
    r = client.get(f"/orders/{pedido['id']}/boleto")
    assert r.status_code == 200, r.text
    ficha = r.json()

    # Os números são os MESMOS do pedido: a ficha não reemite nada.
    assert ficha["digitable_line"] == pedido["boleto_digitable_line"]
    assert ficha["barcode"] == pedido["boleto_barcode"]
    assert ficha["due_date"] == pedido["boleto_due_date"]
    assert ficha["amount"] == pedido["total"]

    assert ficha["beneficiary"]["name"] == "Sebo On-Line LTDA"
    assert ficha["beneficiary"]["document"] == "12345678000199"
    assert "Rua das Livrarias" in ficha["beneficiary"]["address"]
    assert ficha["payer"]["name"] == customer["name"]
    assert ficha["bank_code"] == "341" and ficha["wallet"] == "109"

    # E o carimbo: o PDF sai convincente, e não pode passar por cobrança real.
    assert ficha["demonstration"] is True
    assert endereco["id"]


def test_a_ficha_desenha_o_endereco_do_pedido(
    client, admin_headers, customer, product
):
    end = client.post(
        f"/customers/{customer['id']}/addresses",
        json={"street": "Rua A", "number": "10", "district": "Centro",
              "city": "Campinas", "state": "SP", "zip_code": "13000-000",
              "is_default": True},
    ).json()
    _configurar(client, admin_headers)
    _add(client, customer["id"], product, 1)
    pedido = client.post(
        "/orders/checkout",
        json={"customer_id": customer["id"], "method": "boleto",
              "address_id": end["id"]},
    ).json()

    ficha = client.get(f"/orders/{pedido['id']}/boleto").json()
    assert "Rua A 10" in ficha["payer"]["address"]
    assert "Campinas" in ficha["payer"]["address"]


def test_a_ficha_de_outro_cliente_e_recusada(client, admin_headers, customer, product):
    """A ficha leva nome, CPF e endereço de quem paga: id sequencial não basta."""
    from tests.conftest import register_customer

    pedido = _pedido_com_boleto(client, admin_headers, customer, product)
    del client.headers["Authorization"]
    assert client.get(f"/orders/{pedido['id']}/boleto").status_code in (401, 403)

    register_customer(client)  # agora o client é outra pessoa
    assert client.get(f"/orders/{pedido['id']}/boleto").status_code == 403


def test_pedido_sem_boleto_nao_tem_ficha(
    client, admin_headers, customer, product, fake_initiator
):
    _configurar(client, admin_headers)
    _add(client, customer["id"], product, 1)
    pedido = client.post(
        "/orders/checkout",
        json={"customer_id": customer["id"], "method": "card",
              "card": {"number": "4111111111111111", "holder": "F",
                       "expiry": "12/30", "cvv": "123"}},
    ).json()
    assert client.get(f"/orders/{pedido['id']}/boleto").status_code == 409


def test_ficha_de_pedido_inexistente_e_404(client, customer):
    assert client.get("/orders/999999/boleto").status_code == 404
