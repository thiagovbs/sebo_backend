"""Autorização de cartão de crédito — simulada, e declarada como tal.

Não há adquirente nesta demo. O que existe aqui é a metade que uma loja de
verdade também faz antes de falar com a adquirente: validar o número (Luhn),
identificar a bandeira pelo prefixo, conferir validade e código de segurança.
A decisão de aprovar é **determinística e nossa**, com cartões de teste
documentados abaixo — nenhum dinheiro se move.

A regra que não se negocia: **o número do cartão não é gravado nem registrado
em log**. Ele entra na requisição, é validado, e o que sobra é bandeira, quatro
últimos dígitos, nome impresso e validade — que é exatamente o que o cadastro
do cliente já guardava antes desta funcionalidade existir. Guardar o PAN exigiria
PCI-DSS, e uma demo não tem por que carregar esse risco.

Cartões de teste (o fim do número decide o desfecho):

- terminado em ``0002`` → recusado por saldo/limite
- terminado em ``0069`` → recusado por cartão expirado no emissor
- terminado em ``0127`` → recusado por código de segurança inválido
- qualquer outro número válido → aprovado
"""

import hashlib
from datetime import date

# Prefixos por bandeira. Lista curta de propósito: cobre o que os cartões de
# teste usam e o que o cadastro do cliente já oferecia como opção.
_BANDEIRAS = (
    ("Visa", ("4",)),
    ("Mastercard", ("51", "52", "53", "54", "55", "2221", "2720")),
    ("Amex", ("34", "37")),
    ("Elo", ("4011", "4312", "4389", "5041", "5067", "6277", "6362", "6363")),
    ("Hipercard", ("606282", "3841")),
)

_RECUSAS = {
    "0002": "Cartão recusado pelo emissor (limite insuficiente).",
    "0069": "Cartão recusado pelo emissor (cartão expirado).",
    "0127": "Cartão recusado pelo emissor (código de segurança inválido).",
}


class CardError(ValueError):
    """Dado do cartão inválido. A mensagem é mostrada a quem digitou."""


def apenas_digitos(valor: str) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def luhn_valido(numero: str) -> bool:
    """Dígito verificador do cartão. Pega erro de digitação, não fraude."""
    if len(numero) < 13 or not numero.isdigit():
        return False
    soma = 0
    par = False
    for digito in reversed(numero):
        valor = int(digito)
        if par:
            valor *= 2
            if valor > 9:
                valor -= 9
        soma += valor
        par = not par
    return soma % 10 == 0


def bandeira_de(numero: str) -> str:
    """Bandeira pelo prefixo. Elo e Hipercard antes de Visa e Mastercard.

    A ordem importa: 4011 é Elo e também começa com 4, que é Visa. Testar o
    prefixo mais específico primeiro é o que evita rotular Elo como Visa.
    """
    for nome, prefixos in reversed(_BANDEIRAS):
        if any(numero.startswith(p) for p in prefixos):
            return nome
    return "Outra"


def validade_futura(expiry: str, *, hoje: date | None = None) -> tuple[int, int]:
    """Aceita MM/AA e MM/AAAA; devolve (mês, ano de quatro dígitos)."""
    bruto = (expiry or "").strip()
    partes = bruto.replace("-", "/").split("/")
    if len(partes) != 2 or not all(p.strip().isdigit() for p in partes):
        raise CardError("Validade inválida: use MM/AA.")
    mes = int(partes[0])
    ano = int(partes[1])
    if ano < 100:
        ano += 2000
    if not 1 <= mes <= 12:
        raise CardError("Validade inválida: mês fora de 1 a 12.")
    referencia = hoje or date.today()
    # O cartão vale até o ÚLTIMO dia do mês impresso.
    if (ano, mes) < (referencia.year, referencia.month):
        raise CardError("Cartão vencido.")
    if ano > referencia.year + 20:
        raise CardError("Validade inválida.")
    return mes, ano


def _cvv_esperado(bandeira: str) -> int:
    return 4 if bandeira == "Amex" else 3


def autorizar(
    *,
    numero: str,
    holder: str,
    expiry: str,
    cvv: str,
    valor: float,
    referencia: str = "",
    hoje: date | None = None,
) -> dict:
    """Valida e decide. Devolve só dado não sensível; nunca o número.

    Levanta ``CardError`` quando o dado está errado — isso é problema de quem
    digitou, e a mensagem volta para a tela. A recusa do emissor NÃO é exceção:
    ela é um desfecho legítimo, e vem em ``approved: False`` com o motivo, para
    o chamador poder registrar a tentativa.
    """
    limpo = apenas_digitos(numero)
    if not luhn_valido(limpo):
        raise CardError("Número de cartão inválido.")
    if not (holder or "").strip():
        raise CardError("Informe o nome impresso no cartão.")

    bandeira = bandeira_de(limpo)
    mes, ano = validade_futura(expiry, hoje=hoje)
    cvv_limpo = apenas_digitos(cvv)
    if len(cvv_limpo) != _cvv_esperado(bandeira):
        raise CardError(
            f"Código de segurança deve ter {_cvv_esperado(bandeira)} dígitos."
        )
    if valor <= 0:
        raise CardError("Valor da cobrança inválido.")

    ultimos = limpo[-4:]
    motivo = _RECUSAS.get(ultimos)
    if motivo:
        return {
            "approved": False,
            "reason": motivo,
            "brand": bandeira,
            "last4": ultimos,
            "holder": holder.strip()[:120],
            "expiry": f"{mes:02d}/{str(ano)[-2:]}",
            "authorization_code": "",
        }

    # Código de autorização determinístico: a mesma tentativa produz o mesmo
    # código, o que torna o teste estável. Deriva de valor, pedido e últimos
    # quatro dígitos -- nunca do número inteiro, que não pode sobrar em lugar
    # nenhum, nem dentro de um hash.
    semente = f"{referencia}|{valor:.2f}|{ultimos}|{bandeira}"
    codigo = hashlib.sha256(semente.encode("utf-8")).hexdigest()[:6].upper()
    return {
        "approved": True,
        "reason": "",
        "brand": bandeira,
        "last4": ultimos,
        "holder": holder.strip()[:120],
        "expiry": f"{mes:02d}/{str(ano)[-2:]}",
        "authorization_code": codigo,
    }


def autorizar_salvo(
    *,
    brand: str,
    last4: str,
    holder: str,
    expiry: str,
    valor: float,
    referencia: str = "",
    hoje: date | None = None,
) -> dict:
    """Autoriza com um cartão já salvo no cadastro.

    O cadastro guarda apenas dado não sensível — foi decisão de projeto, e ela
    se mantém: aqui não há número nem código de segurança para conferir. Numa
    loja real este é o caminho do token da adquirente, que representa o cartão
    sem expor o número; a demo aplica as mesmas regras sobre o que tem.

    O código de segurança não é pedido de novo. Muitas lojas pedem, e seria o
    comportamento mais seguro — mas sem o número não há contra o que validar, e
    um campo que aceita qualquer coisa dá falsa sensação de conferência.
    """
    ultimos = apenas_digitos(last4)[-4:]
    if len(ultimos) != 4:
        raise CardError("Cartão salvo sem os últimos dígitos; cadastre-o de novo.")
    mes, ano = validade_futura(expiry, hoje=hoje)
    if valor <= 0:
        raise CardError("Valor da cobrança inválido.")

    motivo = _RECUSAS.get(ultimos)
    bandeira = (brand or "").strip() or "Outra"
    if motivo:
        return {
            "approved": False, "reason": motivo, "brand": bandeira, "last4": ultimos,
            "holder": (holder or "").strip()[:120], "expiry": f"{mes:02d}/{str(ano)[-2:]}",
            "authorization_code": "",
        }
    semente = f"{referencia}|{valor:.2f}|{ultimos}|{bandeira}"
    return {
        "approved": True, "reason": "", "brand": bandeira, "last4": ultimos,
        "holder": (holder or "").strip()[:120], "expiry": f"{mes:02d}/{str(ano)[-2:]}",
        "authorization_code": hashlib.sha256(semente.encode("utf-8")).hexdigest()[:6].upper(),
    }
