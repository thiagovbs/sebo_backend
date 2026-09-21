"""Cartão de crédito no checkout: validação, autorização simulada e sigilo.

O teste mais importante deste arquivo é o último: o número do cartão não pode
sobrar em lugar nenhum do banco. Todo o resto é consequência disso.
"""

from datetime import date

import pytest
from sqlmodel import Session, select

from app.card import CardError, autorizar, autorizar_salvo, bandeira_de, luhn_valido
from app.db import engine
from app.models import Order, PaymentMethod, Product

# Cartões de teste. Todos passam por Luhn; o fim do número decide o desfecho.
APROVADO = "4111111111111111"
RECUSADO = "4012001037141112"  # termina em 1112 -> aprovado
SEM_LIMITE = "5555555555554444"  # termina em 4444 -> aprovado
LIMITE_ESTOURADO = "4000000000000002"  # termina em 0002 -> recusado


def _add(client, customer_id, product, qty=1):
    return client.post(
        f"/cart/{customer_id}/items", json={"product_id": product["id"], "quantity": qty}
    )


def _novo_cartao(**over):
    corpo = {
        "number": APROVADO,
        "holder": "FULANO DE TESTE",
        "expiry": "12/30",
        "cvv": "123",
        "save": False,
    }
    corpo.update(over)
    return corpo


def _checkout(client, customer, **over):
    corpo = {"customer_id": customer["id"], "method": "card"}
    corpo.update(over)
    return client.post("/orders/checkout", json=corpo)


# --- Unidade ---------------------------------------------------------------


def test_luhn_pega_erro_de_digitacao():
    assert luhn_valido(APROVADO)
    assert not luhn_valido("4111111111111112")
    assert not luhn_valido("123")


def test_bandeira_por_prefixo_com_elo_antes_de_visa():
    assert bandeira_de("4111111111111111") == "Visa"
    assert bandeira_de("5555555555554444") == "Mastercard"
    assert bandeira_de("378282246310005") == "Amex"
    # 4011 é Elo e também começa com 4: testar o prefixo mais específico
    # primeiro é o que evita rotular Elo como Visa.
    assert bandeira_de("4011780000000000") == "Elo"


def test_validade_no_passado_e_recusada():
    with pytest.raises(CardError, match="vencido"):
        autorizar(
            numero=APROVADO, holder="F", expiry="01/25", cvv="123", valor=10.0,
            hoje=date(2026, 9, 21),
        )


def test_cartao_vale_ate_o_ultimo_dia_do_mes_impresso():
    # Validade 09/26 com hoje em 21/09/2026: ainda vale.
    resultado = autorizar(
        numero=APROVADO, holder="F", expiry="09/26", cvv="123", valor=10.0,
        hoje=date(2026, 9, 21),
    )
    assert resultado["approved"] is True


def test_cvv_de_amex_tem_quatro_digitos():
    with pytest.raises(CardError, match="4 dígitos"):
        autorizar(
            numero="378282246310005", holder="F", expiry="12/30", cvv="123", valor=10.0
        )


def test_autorizacao_nao_devolve_o_numero():
    resultado = autorizar(
        numero=APROVADO, holder="Fulano", expiry="12/30", cvv="123", valor=99.9
    )
    assert resultado["approved"] is True
    assert resultado["last4"] == "1111"
    # O número inteiro não aparece em nenhum campo da resposta.
    assert APROVADO not in str(resultado)
    assert len(resultado["authorization_code"]) == 6


def test_mesma_tentativa_produz_o_mesmo_codigo_de_autorizacao():
    a = autorizar(numero=APROVADO, holder="F", expiry="12/30", cvv="123", valor=10.0,
                  referencia="pedido-1")
    b = autorizar(numero=APROVADO, holder="F", expiry="12/30", cvv="123", valor=10.0,
                  referencia="pedido-1")
    assert a["authorization_code"] == b["authorization_code"]


def test_cartao_de_teste_recusado_traz_motivo_em_vez_de_excecao():
    resultado = autorizar(
        numero=LIMITE_ESTOURADO, holder="F", expiry="12/30", cvv="123", valor=10.0
    )
    # Recusa não é erro de dado: é desfecho, e o chamador precisa poder
    # registrar a tentativa.
    assert resultado["approved"] is False
    assert "limite" in resultado["reason"]
    assert resultado["authorization_code"] == ""


def test_cartao_salvo_autoriza_sem_numero_nem_cvv():
    resultado = autorizar_salvo(
        brand="Visa", last4="1111", holder="Fulano", expiry="12/30", valor=50.0
    )
    assert resultado["approved"] is True
    # E o cartão de teste recusado continua recusando pelo last4.
    recusado = autorizar_salvo(
        brand="Visa", last4="0002", holder="Fulano", expiry="12/30", valor=50.0
    )
    assert recusado["approved"] is False


# --- Checkout --------------------------------------------------------------


def test_checkout_com_cartao_novo_paga_e_baixa_estoque(
    client, customer, product, fake_initiator
):
    _add(client, customer["id"], product, 2)
    antes = client.get(f"/products/{product['id']}").json()["stock"]

    r = _checkout(client, customer, card=_novo_cartao())
    assert r.status_code == 201, r.text
    pedido = r.json()
    assert pedido["status"] == "PAID"
    assert pedido["payment_method"] == "card"
    assert pedido["card_brand"] == "Visa"
    assert pedido["card_last4"] == "1111"
    assert pedido["card_authorization"]
    assert pedido["total"] == round(product["price"] * 2, 2)

    depois = client.get(f"/products/{product['id']}").json()["stock"]
    assert depois == antes - 2
    assert client.get(f"/cart/{customer['id']}").json()["items"] == []


def test_cartao_recusado_nao_cria_pedido_nem_mexe_no_estoque(
    client, customer, product, fake_initiator
):
    _add(client, customer["id"], product, 1)
    antes = client.get(f"/products/{product['id']}").json()["stock"]

    r = _checkout(client, customer, card=_novo_cartao(number=LIMITE_ESTOURADO))
    assert r.status_code == 402
    assert r.json()["detail"]["error"] == "CARD_DECLINED"

    # Autorizar antes de persistir é o que garante isto: carrinho e estoque
    # intactos depois de uma recusa.
    assert client.get(f"/products/{product['id']}").json()["stock"] == antes
    assert len(client.get(f"/cart/{customer['id']}").json()["items"]) == 1


def test_numero_invalido_volta_como_erro_de_quem_digitou(
    client, customer, product, fake_initiator
):
    _add(client, customer["id"], product, 1)
    r = _checkout(client, customer, card=_novo_cartao(number="4111111111111112"))
    assert r.status_code == 400
    assert "inválido" in r.json()["detail"]


def test_checkout_sem_escolher_cartao_pede_um(client, customer, product, fake_initiator):
    _add(client, customer["id"], product, 1)
    r = _checkout(client, customer)
    assert r.status_code == 400
    assert "cartão" in r.json()["detail"]


def test_salvar_guarda_so_dado_nao_sensivel(client, customer, product, fake_initiator):
    _add(client, customer["id"], product, 1)
    r = _checkout(
        client, customer, card=_novo_cartao(save=True, label="Meu cartão")
    )
    assert r.status_code == 201

    cartoes = client.get(f"/customers/{customer['id']}/payment-methods").json()
    assert len(cartoes) == 1
    salvo = cartoes[0]
    assert salvo["label"] == "Meu cartão"
    assert salvo["brand"] == "Visa"
    assert salvo["last4"] == "1111"
    assert salvo["expiry"] == "12/30"
    # Primeiro cartão do cadastro vira o padrão.
    assert salvo["is_default"] is True


def test_cartao_recusado_nao_e_salvo(client, customer, product, fake_initiator):
    _add(client, customer["id"], product, 1)
    _checkout(client, customer, card=_novo_cartao(number=LIMITE_ESTOURADO, save=True))
    assert client.get(f"/customers/{customer['id']}/payment-methods").json() == []


def test_checkout_com_cartao_salvo(client, customer, product, fake_initiator):
    cartao = client.post(
        f"/customers/{customer['id']}/payment-methods",
        json={"label": "Principal", "brand": "Mastercard", "last4": "4444",
              "holder": "FULANO", "expiry": "11/29", "is_default": True},
    ).json()

    _add(client, customer["id"], product, 1)
    r = _checkout(client, customer, payment_method_id=cartao["id"])
    assert r.status_code == 201, r.text
    pedido = r.json()
    assert pedido["status"] == "PAID"
    assert pedido["card_brand"] == "Mastercard"
    assert pedido["card_last4"] == "4444"


def test_cartao_de_outro_cliente_nao_e_alcancavel(client, product, fake_initiator):
    from tests.conftest import register_customer

    vitima = register_customer(client)
    cartao = client.post(
        f"/customers/{vitima['id']}/payment-methods",
        json={"label": "Da vítima", "brand": "Visa", "last4": "1111",
              "holder": "VITIMA", "expiry": "12/30"},
    ).json()

    # Outro cliente, autenticado como ele mesmo, tentando usar o cartão alheio.
    atacante = register_customer(client)
    _add(client, atacante["id"], product, 1)
    r = client.post(
        "/orders/checkout",
        json={"customer_id": atacante["id"], "method": "card",
              "payment_method_id": cartao["id"]},
    )
    assert r.status_code == 404


def test_o_numero_do_cartao_nao_fica_no_banco(client, customer, product, fake_initiator):
    _add(client, customer["id"], product, 1)
    r = _checkout(client, customer, card=_novo_cartao(save=True))
    assert r.status_code == 201

    # A regra que não se negocia: o PAN não sobra em nenhuma tabela. Guardá-lo
    # exigiria PCI-DSS, e a demo não tem por que carregar esse risco.
    with Session(engine) as s:
        for modelo in (Order, PaymentMethod, Product):
            for linha in s.exec(select(modelo)).all():
                assert APROVADO not in str(linha.model_dump())
