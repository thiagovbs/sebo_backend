"""Configuração de integração com a aplicação pagadora (iniciadora).

Fonte da verdade no banco (linha única ``IntegrationSettings``), editável no
admin em tempo de execução. Enquanto não estiver completa, o pagamento por Open
Finance fica indisponível para o cliente.
"""

from sqlmodel import Session

from .boleto import BoletoError, conferir_emissor
from .models import IntegrationSettings
from .payments import get_client  # noqa: F401  (reexport para os routers)


def get_integration(session: Session) -> IntegrationSettings:
    """Devolve a linha de configuração, criando-a vazia se ainda não existir."""
    cfg = session.get(IntegrationSettings, 1)
    if cfg is None:
        cfg = IntegrationSettings(id=1)
        session.add(cfg)
        session.commit()
        session.refresh(cfg)
    return cfg


def is_configured(cfg: IntegrationSettings) -> bool:
    """True quando dá para operar o **PIX JSR** (Open Finance pela iniciadora).

    Precisa das credenciais do lojista na iniciadora (url, usuário, senha) e do
    recebedor do PIX (nome, documento e chave). Alias: JSR disponível.
    """
    return all(
        [
            cfg.payment_initiator_url,
            cfg.payment_initiator_user,
            cfg.payment_initiator_password,
            cfg.sebo_name,
            cfg.sebo_cpf_cnpj,
            cfg.sebo_pix_key_value,
        ]
    )


def pix_qr_available(cfg: IntegrationSettings) -> bool:
    """True quando dá para gerar o **PIX QR clássico** (copia e cola).

    Independe da iniciadora: basta os dados do recebedor (nome, cidade e chave).
    """
    return all([cfg.sebo_name, cfg.sebo_city, cfg.sebo_pix_key_value])


def card_available(cfg: IntegrationSettings) -> bool:
    """True quando dá para cobrar no cartão.

    A autorização é simulada dentro da própria loja (ver ``app/card.py``), sem
    adquirente — então não há credencial de adquirente a exigir. Mas a exigência
    não é nenhuma: pede-se a IDENTIFICAÇÃO da loja (razão social e CNPJ), que é
    o mínimo para uma cobrança fazer sentido e é o mesmo dado que o PIX já pede.

    Isso preserva uma propriedade que vale a pena: loja recém-instalada, sem
    nada configurado, não oferece pagamento nenhum. Sem essa condição, o cartão
    apareceria sempre e um pedido nasceria PAGO numa loja que ninguém terminou
    de configurar.

    No dia em que houver adquirente de verdade, é aqui que a credencial dela
    entra na conta.
    """
    return all([cfg.sebo_name, cfg.sebo_cpf_cnpj])


def boleto_form_errors(cfg: IntegrationSettings) -> list[str]:
    """Problemas de FORMA no que está preenchido, na conta da própria emissão.

    ``boleto_missing`` diz o que está vazio; isto diz o que está preenchido e
    não cabe no layout -- uma agência com dígito verificador, por exemplo. Usa
    a mesma função que monta o boleto, de propósito: enquanto a
    disponibilidade só olhava se o campo tinha algo, o painel anunciava um
    boleto que o checkout recusava.
    """
    try:
        conferir_emissor(
            banco=cfg.boleto_bank_code,
            agencia=cfg.boleto_agency,
            conta=cfg.boleto_account,
            carteira=cfg.boleto_wallet,
        )
    except BoletoError as exc:
        return [str(exc)]
    return []


def boleto_available(cfg: IntegrationSettings) -> bool:
    """True quando há dados suficientes para EMITIR um boleto válido.

    Beneficiário (razão social, CNPJ e endereço) e conta (banco, agência, conta
    e carteira), todos preenchidos e em forma. Faltando qualquer um, o boleto
    sairia sem quem recebe ou sem onde cair -- e um boleto assim é pior que
    método indisponível, porque o cliente só descobre no banco.
    """
    if boleto_form_errors(cfg):
        return False
    return all(
        [
            cfg.sebo_name,
            cfg.sebo_cpf_cnpj,
            cfg.sebo_street,
            cfg.sebo_city,
            cfg.sebo_state,
            cfg.sebo_zip_code,
            cfg.boleto_bank_code,
            cfg.boleto_agency,
            cfg.boleto_account,
            cfg.boleto_wallet,
        ]
    )


def boleto_missing(cfg: IntegrationSettings) -> list[str]:
    """O que impede o boleto, em nome legível, para o admin não adivinhar.

    Campo vazio e campo fora de forma entram na mesma lista: para quem está na
    tela, os dois são a mesma pergunta -- "o que falta para o boleto
    funcionar?".
    """
    rotulos = {
        "sebo_name": "Razão social",
        "sebo_cpf_cnpj": "CNPJ",
        "sebo_street": "Logradouro",
        "sebo_city": "Cidade",
        "sebo_state": "UF",
        "sebo_zip_code": "CEP",
        "boleto_bank_code": "Código do banco",
        "boleto_agency": "Agência",
        "boleto_account": "Conta",
        "boleto_wallet": "Carteira",
    }
    vazios = [rotulo for campo, rotulo in rotulos.items() if not getattr(cfg, campo, "")]
    return vazios + boleto_form_errors(cfg)
