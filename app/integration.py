"""Configuração de integração com a aplicação pagadora (iniciadora).

Fonte da verdade no banco (linha única ``IntegrationSettings``), editável no
admin em tempo de execução. Enquanto não estiver completa, o pagamento por Open
Finance fica indisponível para o cliente.
"""

from sqlmodel import Session

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
