"""Área administrativa: métricas de venda, pedidos e clientes.

Quase tudo aqui é leitura, agregando o que já está no banco. O cadastro de
produtos reusa o CRUD de ``/products`` (com ``include_inactive=true`` para o
admin ver também os inativos).

A exceção é a **confirmação de pagamento** de PIX QR e boleto. Ela mora aqui, e
não no cliente, porque quem recebe o dinheiro é a loja e é ela que confere o
extrato -- o cliente declarando "já paguei" marcaria o pedido como pago sem
ninguém ter visto entrar. Numa loja real este é o lugar onde o webhook do PSP ou
o arquivo de retorno do banco chegariam.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..adminauth import check_admin_password, create_admin_token, require_admin
from ..db import get_session
from ..integration import (
    boleto_available,
    boleto_missing,
    get_client,
    get_integration,
    is_configured,
    pix_qr_available,
)
from ..omnicommerce import enqueue_order_event, flush_in_background
from ..payments import PaymentInitiatorError
from ..models import (
    Address,
    Customer,
    Device,
    IntegrationSettings,
    Order,
    OrderItem,
    PaymentMethod,
    Product,
)
from ..schemas import IntegrationIn, IntegrationOut

router = APIRouter(prefix="/admin", tags=["admin"])

# Métodos em que o dinheiro chega DEPOIS do pedido, e que por isso têm
# confirmação. Cartão e PIX JSR nascem pagos; o Open Finance com redirect se
# reconcilia contra a iniciadora, sem ninguém declarar nada.
CONFIRMAVEIS = ("pix_qr", "boleto")

# Todas as rotas de leitura/gestão exigem o admin, exceto POST /admin/login.
_admin = Depends(require_admin)

# Pedido conta como venda concretizada quando está pago.
PAID = "PAID"


class AdminLogin(BaseModel):
    password: str = Field(..., description="Senha do painel de admin")


@router.post("/login")
def admin_login(body: AdminLogin) -> dict:
    """Troca a senha do painel por um token de acesso administrativo."""
    if not check_admin_password(body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Senha inválida"
        )
    return create_admin_token()


def _digitos(valor: str) -> str:
    return "".join(c for c in (valor or "") if c.isdigit())


def _integration_out(cfg: IntegrationSettings) -> IntegrationOut:
    return IntegrationOut(
        payment_initiator_url=cfg.payment_initiator_url,
        payment_initiator_user=cfg.payment_initiator_user,
        has_password=bool(cfg.payment_initiator_password),
        sebo_name=cfg.sebo_name,
        sebo_cpf_cnpj=cfg.sebo_cpf_cnpj,
        sebo_city=cfg.sebo_city,
        sebo_pix_key_type=cfg.sebo_pix_key_type,
        sebo_pix_key_value=cfg.sebo_pix_key_value,
        boleto_bank_code=cfg.boleto_bank_code,
        boleto_bank_name=cfg.boleto_bank_name,
        boleto_agency=cfg.boleto_agency,
        boleto_account=cfg.boleto_account,
        boleto_wallet=cfg.boleto_wallet,
        boleto_days_to_due=cfg.boleto_days_to_due,
        boleto_instructions=cfg.boleto_instructions,
        sebo_street=cfg.sebo_street,
        sebo_number=cfg.sebo_number,
        sebo_complement=cfg.sebo_complement,
        sebo_district=cfg.sebo_district,
        sebo_state=cfg.sebo_state,
        sebo_zip_code=cfg.sebo_zip_code,
        configured=is_configured(cfg),
        pix_qr_available=pix_qr_available(cfg),
        boleto_available=boleto_available(cfg),
        boleto_missing=boleto_missing(cfg),
    )


@router.get("/integration", response_model=IntegrationOut, dependencies=[_admin])
def read_integration(session: Session = Depends(get_session)) -> IntegrationOut:
    """Dados de integração com a iniciadora (a senha nunca é devolvida)."""
    return _integration_out(get_integration(session))


@router.put("/integration", response_model=IntegrationOut, dependencies=[_admin])
def update_integration(
    data: IntegrationIn, session: Session = Depends(get_session)
) -> IntegrationOut:
    """Grava a config em tempo de execução. A senha só muda se vier preenchida."""
    cfg = get_integration(session)
    cfg.payment_initiator_url = data.payment_initiator_url.strip()
    cfg.payment_initiator_user = data.payment_initiator_user.strip()
    if data.payment_initiator_password:  # vazio/None => mantém a senha atual
        cfg.payment_initiator_password = data.payment_initiator_password
    cfg.sebo_name = data.sebo_name.strip()
    cfg.sebo_cpf_cnpj = data.sebo_cpf_cnpj.strip()
    cfg.sebo_city = data.sebo_city.strip() or "SAO PAULO"
    cfg.sebo_pix_key_type = data.sebo_pix_key_type.strip() or "CNPJ"
    cfg.sebo_pix_key_value = data.sebo_pix_key_value.strip()
    # Emissor do boleto. Só dígitos onde o padrão FEBRABAN só aceita dígitos:
    # deixar passar ponto ou barra aqui viraria erro na emissão, longe de quem
    # digitou.
    cfg.boleto_bank_code = _digitos(data.boleto_bank_code)
    cfg.boleto_bank_name = data.boleto_bank_name.strip()
    cfg.boleto_agency = _digitos(data.boleto_agency)
    cfg.boleto_account = _digitos(data.boleto_account)
    cfg.boleto_wallet = _digitos(data.boleto_wallet)
    # Prazo entre 0 e 60 dias: fora disso é erro de digitação, e um vencimento
    # absurdo só apareceria no boleto do cliente.
    cfg.boleto_days_to_due = max(0, min(60, data.boleto_days_to_due))
    cfg.boleto_instructions = data.boleto_instructions.strip()[:500]
    cfg.sebo_street = data.sebo_street.strip()
    cfg.sebo_number = data.sebo_number.strip()
    cfg.sebo_complement = data.sebo_complement.strip()
    cfg.sebo_district = data.sebo_district.strip()
    cfg.sebo_state = data.sebo_state.strip().upper()[:2]
    cfg.sebo_zip_code = _digitos(data.sebo_zip_code)
    cfg.updated_at = datetime.now(timezone.utc)
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return _integration_out(cfg)


@router.get("/stats", dependencies=[_admin])
def stats(days: int = 14, session: Session = Depends(get_session)) -> dict:
    """Painel de vendas: KPIs, receita por dia, status e produtos mais vendidos."""
    orders = session.exec(select(Order)).all()
    paid = [o for o in orders if o.status == PAID]

    revenue = round(sum(o.total for o in paid), 2)
    paid_count = len(paid)
    avg_ticket = round(revenue / paid_count, 2) if paid_count else 0.0

    # Contagem por status (todos os pedidos).
    by_status: dict[str, int] = defaultdict(int)
    for o in orders:
        by_status[o.status] += 1

    # Receita por dia nos últimos ``days`` dias (só pedidos pagos).
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    daily: dict[date, float] = {start + timedelta(d): 0.0 for d in range(days)}
    for o in paid:
        d = _as_date(o.created_at)
        if d in daily:
            daily[d] += o.total
    revenue_by_day = [
        {"date": d.isoformat(), "revenue": round(v, 2)} for d, v in sorted(daily.items())
    ]

    # Produtos mais vendidos (por quantidade), somando os itens de pedidos pagos.
    paid_ids = {o.id for o in paid}
    sold_qty: dict[str, int] = defaultdict(int)
    sold_rev: dict[str, float] = defaultdict(float)
    if paid_ids:
        for item in session.exec(select(OrderItem)).all():
            if item.order_id in paid_ids:
                sold_qty[item.name] += item.quantity
                sold_rev[item.name] += item.unit_price * item.quantity
    top_products = sorted(
        (
            {"name": name, "quantity": qty, "revenue": round(sold_rev[name], 2)}
            for name, qty in sold_qty.items()
        ),
        key=lambda p: p["quantity"],
        reverse=True,
    )[:5]

    total_products = len(session.exec(select(Product)).all())
    active_products = len(
        session.exec(select(Product).where(Product.active == True)).all()  # noqa: E712
    )
    total_customers = len(session.exec(select(Customer)).all())

    return {
        "kpis": {
            "revenue": revenue,
            "paid_orders": paid_count,
            "total_orders": len(orders),
            "avg_ticket": avg_ticket,
            "customers": total_customers,
            "active_products": active_products,
            "total_products": total_products,
        },
        "orders_by_status": dict(by_status),
        "revenue_by_day": revenue_by_day,
        "top_products": top_products,
    }


@router.get("/orders", dependencies=[_admin])
def all_orders(session: Session = Depends(get_session)) -> list[dict]:
    """Todos os pedidos, do mais recente ao mais antigo, com o cliente e os itens."""
    orders = session.exec(select(Order).order_by(Order.created_at.desc())).all()
    customers = {c.id: c for c in session.exec(select(Customer)).all()}

    result: list[dict] = []
    for o in orders:
        items = session.exec(select(OrderItem).where(OrderItem.order_id == o.id)).all()
        customer = customers.get(o.customer_id)
        result.append(
            {
                "id": o.id,
                "status": o.status,
                "total": o.total,
                "created_at": o.created_at.isoformat(),
                "payment_status": o.payment_status,
                "payment_method": o.payment_method,
                # O que a tela precisa para saber se há pagamento a confirmar e
                # para mostrar de que cobrança se trata.
                "awaiting_confirmation": o.status == "AWAITING_PAYMENT"
                and o.payment_method in CONFIRMAVEIS,
                "boleto_due_date": o.boleto_due_date,
                "boleto_our_number": o.boleto_our_number,
                "card_brand": o.card_brand,
                "card_last4": o.card_last4,
                "customer": _customer_brief(customer),
                "items": [
                    {"name": i.name, "quantity": i.quantity, "unit_price": i.unit_price}
                    for i in items
                ],
            }
        )
    return result


@router.post("/orders/{order_id}/confirm-payment", dependencies=[_admin])
def confirm_payment(
    order_id: int,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> dict:
    """Registra que o pagamento de um pedido caiu, e o marca como pago.

    Vale para PIX QR e boleto, que são os métodos em que o dinheiro chega
    depois: o cartão e o PIX JSR nascem pagos, e o Open Finance com redirect se
    reconcilia contra a iniciadora.

    Numa loja real quem dispara isto é o PSP (webhook do PIX) ou o arquivo de
    retorno do banco (boleto). Nesta demo não há nenhum dos dois, então a loja
    registra à mão -- mas é a LOJA, que vê o extrato, e não o cliente.

    Idempotente: confirmar um pedido já pago devolve o mesmo resultado, porque
    dois cliques no mesmo botão não podem virar dois avisos de venda.
    """
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if order.payment_method not in CONFIRMAVEIS:
        raise HTTPException(
            status_code=409,
            detail=f"Pedido pago por {order.payment_method} não tem confirmação manual.",
        )
    if order.status == "PAID":
        return {"id": order.id, "status": order.status, "already_paid": True}
    if order.status != "AWAITING_PAYMENT":
        raise HTTPException(status_code=409, detail=f"Pedido está {order.status}")

    order.status = "PAID"
    order.payment_status = "COMPLETED"
    session.add(order)
    session.commit()
    session.refresh(order)

    # O estoque já foi baixado na emissão; o que falta é avisar o omnicommerce,
    # que é quem replica a venda aos outros canais.
    if enqueue_order_event(session, order) is not None:
        session.commit()
        background.add_task(flush_in_background)
    return {"id": order.id, "status": order.status, "already_paid": False}


@router.get("/customers", dependencies=[_admin])
def all_customers(session: Session = Depends(get_session)) -> list[dict]:
    """Clientes cadastrados, com nº de pedidos, nº de pagos e total gasto."""
    customers = session.exec(select(Customer).order_by(Customer.name)).all()
    orders = session.exec(select(Order)).all()

    orders_by_customer: dict[int, list[Order]] = defaultdict(list)
    for o in orders:
        orders_by_customer[o.customer_id].append(o)

    result: list[dict] = []
    for c in customers:
        cust_orders = orders_by_customer.get(c.id, [])
        paid = [o for o in cust_orders if o.status == PAID]
        result.append(
            {
                "id": c.id,
                "name": c.name,
                "email": c.email,
                "phone": c.phone,
                "cpf": c.cpf,
                "created_at": c.created_at.isoformat(),
                "orders_count": len(cust_orders),
                "paid_count": len(paid),
                "total_spent": round(sum(o.total for o in paid), 2),
            }
        )
    return result


@router.get("/customers/{customer_id}", dependencies=[_admin])
def customer_detail(customer_id: int, session: Session = Depends(get_session)) -> dict:
    """Ficha completa do cliente: dados cadastrais + histórico de pedidos.

    Reúne dados pessoais, endereços, cartões, o status da autorização de
    pagamento e todos os pedidos (com itens), para a visão do admin.
    """
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    addresses = session.exec(
        select(Address).where(Address.customer_id == customer_id)
    ).all()
    methods = session.exec(
        select(PaymentMethod).where(PaymentMethod.customer_id == customer_id)
    ).all()
    device = session.exec(
        select(Device).where(Device.customer_id == customer_id)
    ).first()
    orders = session.exec(
        select(Order)
        .where(Order.customer_id == customer_id)
        .order_by(Order.created_at.desc())
    ).all()

    orders_out = []
    for o in orders:
        items = session.exec(
            select(OrderItem).where(OrderItem.order_id == o.id)
        ).all()
        orders_out.append(
            {
                "id": o.id,
                "status": o.status,
                "total": o.total,
                "created_at": o.created_at.isoformat(),
                "payment_status": o.payment_status,
                "payment_id": o.payment_id,
                "items": [
                    {"name": i.name, "quantity": i.quantity, "unit_price": i.unit_price}
                    for i in items
                ],
            }
        )

    paid = [o for o in orders if o.status == PAID]
    return {
        "customer": {
            "id": customer.id,
            "name": customer.name,
            "email": customer.email,
            "phone": customer.phone,
            "cpf": customer.cpf,
            "birth_date": customer.birth_date,
            "created_at": customer.created_at.isoformat(),
        },
        "addresses": [
            {
                "id": a.id, "label": a.label, "street": a.street, "number": a.number,
                "complement": a.complement, "district": a.district, "city": a.city,
                "state": a.state, "zip_code": a.zip_code, "is_default": a.is_default,
            }
            for a in addresses
        ],
        "payment_methods": [
            {
                "id": m.id, "label": m.label, "brand": m.brand, "last4": m.last4,
                "holder": m.holder, "expiry": m.expiry, "is_default": m.is_default,
            }
            for m in methods
        ],
        "device": {
            "enrolled": device is not None,
            "status": device.status if device else None,
            "enrollment_id": device.enrollment_id if device else "",
        },
        "orders": orders_out,
        "stats": {
            "orders_count": len(orders),
            "paid_count": len(paid),
            "total_spent": round(sum(o.total for o in paid), 2),
        },
    }


@router.delete("/customers/{customer_id}/device", dependencies=[_admin])
def delete_customer_device(
    customer_id: int, session: Session = Depends(get_session)
) -> dict:
    """Apaga a autorização de pagamento (PIX Open Finance) do cliente.

    Remove o ``Device`` (o ``enrollment_id`` que a loja usa em ``/payments/jsr``).
    O cliente volta a "não autorizado" e pode autorizar de novo. Não revoga na
    iniciadora/core (lá não há endpoint de revogação); do lado da loja, a
    autorização deixa de existir.
    """
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    device = session.exec(
        select(Device).where(Device.customer_id == customer_id)
    ).first()
    initiator_revoked = None
    if device:
        # Revoga também na iniciadora (best-effort): remove o vínculo lá e pede a
        # revogação na detentora. Uma falha aqui não bloqueia a limpeza local.
        if device.enrollment_id:
            cfg = get_integration(session)
            if is_configured(cfg):
                try:
                    get_client(cfg).revoke_enrollment(device.enrollment_id)
                    initiator_revoked = True
                except PaymentInitiatorError:
                    initiator_revoked = False
        session.delete(device)
        session.commit()
    return {"enrolled": False, "initiator_revoked": initiator_revoked}


def _as_date(value: datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _customer_brief(customer: Customer | None) -> dict | None:
    if not customer:
        return None
    return {"id": customer.id, "name": customer.name, "email": customer.email}
