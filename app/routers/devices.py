"""Autorização de pagamento do cliente na iniciadora (jornada JSR).

O enrollment é **do cliente que vai pagar** (o titular da conta): ele autoriza o
Sebo On-Line — que se comporta como o dispositivo a ser autorizado — a iniciar
PIX na conta dele. O cliente autentica uma vez no seu banco pelo ``login_url``;
depois disso o pagamento é sem redirect.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..config import settings
from ..custauth import current_customer, ensure_self
from ..db import get_session
from ..integration import get_client, get_integration, is_configured
from ..models import Customer, Device
from ..payments import PaymentInitiatorError

router = APIRouter(prefix="/customers", tags=["dispositivo"])


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "OPEN_FINANCE_UNAVAILABLE",
            "message": "Pagamento por Open Finance não está disponível no momento.",
        },
    )


def _get_customer(session: Session, customer_id: int) -> Customer:
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    return customer


def get_device(session: Session, customer_id: int) -> Device | None:
    return session.exec(
        select(Device).where(Device.customer_id == customer_id)
    ).first()


def sync_device(session: Session, device: Device) -> Device:
    """Atualiza o status do dispositivo a partir da iniciadora (fonte da verdade)."""
    cfg = get_integration(session)
    if not is_configured(cfg):
        return device
    try:
        remote = get_client(cfg).list_devices()
    except PaymentInitiatorError:
        return device  # mantém o que temos se a iniciadora estiver fora
    match = next(
        (d for d in remote if d.get("enrollment_id") == device.enrollment_id), None
    )
    if match:
        device.status = match.get("status", device.status)
        device.account_id = match.get("account_id", device.account_id) or device.account_id
        session.add(device)
        session.commit()
        session.refresh(device)
    return device


def _out(device: Device | None) -> dict:
    if not device:
        return {"enrolled": False, "status": None, "enrollment_id": "", "account_id": ""}
    return {
        "enrolled": True,
        "status": device.status,
        "enrollment_id": device.enrollment_id,
        "account_id": device.account_id,
    }


@router.get("/{customer_id}/device")
def read_device(
    customer_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> dict:
    """Devolve o dispositivo do cliente, sincronizando o status com a iniciadora."""
    ensure_self(customer, customer_id)
    device = get_device(session, customer_id)
    if device and device.status != "REGISTERED":
        device = sync_device(session, device)
    return _out(device)


@router.post("/{customer_id}/device/enroll")
def enroll_device(
    customer_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> dict:
    """Inicia a autorização para este cliente (o pagador). Devolve o ``login_url``.

    O enrollment é pedido **para o cliente** que está comprando: passamos o nome
    dele como titular a autorizar. O cliente abre o ``login_url`` e autentica no
    seu banco; depois volta e o status é sincronizado por
    ``GET /customers/{id}/device``.
    """
    ensure_self(customer, customer_id)
    cfg = get_integration(session)
    if not is_configured(cfg):
        raise _unavailable()
    # Ao concluir, a iniciadora devolve o cliente para cá (a página "Minha
    # conta" do front), com o resultado na query — jornada sem página morta.
    redirect_uri = f"{settings.frontend_origin.rstrip('/')}/conta?enroll=return"
    try:
        # username = o titular (o cliente pagador); é ele quem autoriza o Sebo.
        result = get_client(cfg).start_enrollment(
            username=customer.name or customer.email,
            redirect_uri=redirect_uri,
        )
    except PaymentInitiatorError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "ENROLLMENT_FAILED", "message": str(exc)},
        ) from exc

    device = get_device(session, customer_id)
    if device:
        device.enrollment_id = result["enrollment_id"]
        device.status = "PENDING"
        device.account_id = ""
    else:
        device = Device(
            customer_id=customer_id,
            enrollment_id=result["enrollment_id"],
            status="PENDING",
        )
    session.add(device)
    session.commit()

    return {"enrollment_id": result["enrollment_id"], "login_url": result["login_url"]}
