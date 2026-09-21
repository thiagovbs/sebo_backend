"""Boleto bancário: código de barras e linha digitável (padrão FEBRABAN).

Aqui não há simulação: a aritmética é a de verdade, e é ela que faz o número
colado no app do banco ser aceito como um boleto bem formado. O que esta demo
**não** tem é registro em banco — o boleto não existe na CIP, então ninguém
consegue pagá-lo de fato. A confirmação do pagamento é acionada na loja, do
mesmo jeito que o PIX QR já faz, e isso está dito na tela para não passar por
cobrança real.

O que é padrão e o que é nosso:

- **Padrão**: as 44 posições do código de barras (banco, moeda, DV geral, fator
  de vencimento, valor em centavos, campo livre), o cálculo do DV por módulo 11,
  os cinco campos da linha digitável de 47 dígitos com DV por módulo 10, e a
  regra do fator de vencimento.
- **Nosso**: o layout do campo livre. Ele é definido por cada banco, e como aqui
  não há banco emissor de verdade, usamos um layout declarado (agência, carteira,
  nosso número e conta). Trocar de banco significa trocar esta parte, e só ela.

O fator de vencimento merece nota. Ele conta dias desde uma data-base, e a base
MUDOU: a contagem original (07/10/1997) chegou a 9999 em 21/02/2025 e reiniciou
em 1000 no dia seguinte. Um código escrito antes disso, ainda somando sobre a
base antiga, produz hoje fator acima de 9999 — que não cabe nas quatro posições.
É por isso que a base aqui é 22/02/2025.
"""

from datetime import date, timedelta

# Data-base vigente do fator de vencimento: 22/02/2025 vale 1000.
_BASE_FATOR = date(2025, 2, 22)
_FATOR_BASE_VALOR = 1000
_FATOR_MAXIMO = 9999
# Moeda 9 = real. É a única que o padrão usa por aqui.
_MOEDA = "9"


class BoletoError(ValueError):
    """Dado insuficiente ou inválido para montar o boleto."""


def _digitos(valor: str, tamanho: int, campo: str) -> str:
    """Só dígitos, alinhado à direita com zeros. Excesso é erro, não corte.

    Cortar silenciosamente produziria um boleto com a conta de outra pessoa —
    o tipo de defeito que ninguém nota até o dinheiro não chegar.
    """
    limpo = "".join(c for c in str(valor) if c.isdigit())
    if len(limpo) > tamanho:
        raise BoletoError(f"{campo} tem mais de {tamanho} dígitos.")
    return limpo.rjust(tamanho, "0")


def fator_de_vencimento(vencimento: date, *, hoje: date | None = None) -> int:
    """Dias entre a data-base e o vencimento, no intervalo 1000..9999."""
    del hoje  # a base é fixa; o parâmetro existe só para simetria em teste
    fator = (vencimento - _BASE_FATOR).days + _FATOR_BASE_VALOR
    if fator < _FATOR_BASE_VALOR:
        raise BoletoError("Vencimento anterior à data-base do fator FEBRABAN.")
    if fator > _FATOR_MAXIMO:
        raise BoletoError("Vencimento além do que o fator FEBRABAN alcança.")
    return fator


def modulo10(bloco: str) -> int:
    """DV de módulo 10: pesos 2 e 1 alternados da direita para a esquerda."""
    soma = 0
    peso = 2
    for digito in reversed(bloco):
        produto = int(digito) * peso
        # Produto de dois algarismos entra pela soma dos seus algarismos, que
        # para 2 dígitos é o mesmo que subtrair 9.
        soma += produto - 9 if produto > 9 else produto
        peso = 1 if peso == 2 else 2
    return (10 - soma % 10) % 10


def modulo11_barras(bloco: str) -> int:
    """DV geral do código de barras: módulo 11 com pesos de 2 a 9.

    Resto 0, 1 ou 10 dá DV 1 — regra do padrão, e não arredondamento nosso.
    """
    soma = 0
    peso = 2
    for digito in reversed(bloco):
        soma += int(digito) * peso
        peso = 2 if peso == 9 else peso + 1
    resto = soma % 11
    dv = 11 - resto
    return 1 if dv in (0, 10, 11) else dv


def campo_livre(
    *, agencia: str, conta: str, carteira: str, nosso_numero: str
) -> str:
    """As 25 posições do campo livre, no layout DESTA loja.

    Cada banco define o seu; este é declarado e documentado: agência (4),
    carteira (3), nosso número (11) e conta (7). Soma 25.
    """
    return (
        _digitos(agencia, 4, "Agência")
        + _digitos(carteira, 3, "Carteira")
        + _digitos(nosso_numero, 11, "Nosso número")
        + _digitos(conta, 7, "Conta")
    )


def codigo_de_barras(
    *,
    banco: str,
    valor: float,
    vencimento: date,
    livre: str,
) -> str:
    """As 44 posições, com o DV geral calculado sobre as outras 43."""
    if len(livre) != 25:
        raise BoletoError("Campo livre precisa ter 25 dígitos.")
    centavos = int(round(valor * 100))
    if centavos <= 0:
        raise BoletoError("Valor do boleto precisa ser positivo.")
    if centavos > 9_999_999_999:
        raise BoletoError("Valor acima do que o código de barras comporta.")

    prefixo = _digitos(banco, 3, "Banco") + _MOEDA
    corpo = f"{fator_de_vencimento(vencimento):04d}{centavos:010d}" + livre
    dv = modulo11_barras(prefixo + corpo)
    # O DV mora na quinta posição, entre a moeda e o fator de vencimento.
    return f"{prefixo}{dv}{corpo}"


def linha_digitavel(barras: str) -> str:
    """Converte as 44 posições nos 47 dígitos que a pessoa digita.

    A linha digitável não é o código de barras em outra ordem: ela reagrupa os
    mesmos dados em cinco campos, cada um dos três primeiros com DV próprio de
    módulo 10. É por isso que são 47 dígitos e não 44.
    """
    if len(barras) != 44 or not barras.isdigit():
        raise BoletoError("Código de barras inválido.")
    banco_moeda = barras[0:4]
    dv_geral = barras[4]
    fator_valor = barras[5:19]
    livre = barras[19:44]

    campo1 = banco_moeda + livre[0:5]
    campo2 = livre[5:15]
    campo3 = livre[15:25]
    return (
        f"{campo1}{modulo10(campo1)}"
        f"{campo2}{modulo10(campo2)}"
        f"{campo3}{modulo10(campo3)}"
        f"{dv_geral}"
        f"{fator_valor}"
    )


def formatar_linha(linha: str) -> str:
    """Linha digitável com a pontuação em que ela é impressa."""
    if len(linha) != 47 or not linha.isdigit():
        raise BoletoError("Linha digitável inválida.")
    return (
        f"{linha[0:5]}.{linha[5:10]} "
        f"{linha[10:15]}.{linha[15:21]} "
        f"{linha[21:26]}.{linha[26:32]} "
        f"{linha[32]} "
        f"{linha[33:47]}"
    )


def nosso_numero_de_pedido(order_id: int) -> str:
    """Nosso número derivado do pedido.

    Precisa ser único por boleto e reencontrável a partir do pagamento — é o
    que liga um recebimento ao pedido. O id do pedido serve aos dois papéis e
    não depende de contador à parte, que exigiria coordenação.
    """
    if order_id <= 0:
        raise BoletoError("Pedido inválido para gerar nosso número.")
    return _digitos(str(order_id), 11, "Nosso número")


def emitir(
    *,
    order_id: int,
    valor: float,
    banco: str,
    agencia: str,
    conta: str,
    carteira: str,
    dias_para_vencer: int,
    hoje: date | None = None,
) -> dict:
    """Monta o boleto do pedido: barras, linha digitável e vencimento."""
    if dias_para_vencer < 0:
        raise BoletoError("Prazo de vencimento não pode ser negativo.")
    referencia = hoje or date.today()
    vencimento = referencia + timedelta(days=dias_para_vencer)
    nosso_numero = nosso_numero_de_pedido(order_id)
    livre = campo_livre(
        agencia=agencia, conta=conta, carteira=carteira, nosso_numero=nosso_numero
    )
    barras = codigo_de_barras(
        banco=banco, valor=valor, vencimento=vencimento, livre=livre
    )
    linha = linha_digitavel(barras)
    return {
        "barcode": barras,
        "digitable_line": linha,
        "digitable_line_formatted": formatar_linha(linha),
        "our_number": nosso_numero,
        "due_date": vencimento.isoformat(),
    }
