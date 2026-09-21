"""Status público do pagamento Open Finance.

O cliente usa isto para saber se a opção de pagamento pode aparecer. Enquanto a
integração não estiver configurada no admin, ``available`` é ``false`` e o front
não mostra o pagamento por Open Finance.
"""

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..db import get_session
from ..integration import (
    boleto_available,
    card_available,
    get_integration,
    is_configured,
    pix_qr_available,
)

router = APIRouter(prefix="/open-finance", tags=["open-finance"])


@router.get("/status")
def status(session: Session = Depends(get_session)) -> dict:
    """Métodos de pagamento disponíveis para o cliente.

    - ``jsr``: PIX Open Finance sem redirect (precisa da iniciadora configurada).
    - ``redirect``: PIX Open Finance com redirect (mesma exigência do JSR:
      iniciadora + recebedor). Oferecido como opção na tela do PIX QR.
    - ``pix_qr``: PIX QR clássico / copia e cola (precisa só do recebedor).
    - ``card``: cartão de crédito (autorização simulada na própria loja).
    - ``boleto``: boleto bancário (precisa dos dados do beneficiário e da conta,
      preenchidos em Integração, no admin).
    - ``available``: compat — true se qualquer método estiver disponível.
    """
    cfg = get_integration(session)
    jsr = is_configured(cfg)
    pix = pix_qr_available(cfg)
    card = card_available(cfg)
    boleto = boleto_available(cfg)
    return {
        "jsr": jsr,
        "redirect": jsr,
        "pix_qr": pix,
        "card": card,
        "boleto": boleto,
        # Compat: a tela antiga só perguntava se HAVIA algum método.
        "available": jsr or pix or card or boleto,
    }
