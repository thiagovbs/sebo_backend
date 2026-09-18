"""Pedidos e pagamento: checkout paga via jornada JSR na payment-initiator.

O cliente é o titular da conta e autoriza o Sebo (que age como o dispositivo) a
iniciar PIX na conta dele, uma vez (ver ``routers/devices.py``). No checkout:

1. valida o carrinho e o estoque e calcula o total;
2. se o cliente não autorizou o pagamento, responde ``409 need_enrollment``
   (o front leva o cliente a autorizar antes);
3. **paga sem redirect** (``/payments/jsr``) — se a iniciadora estiver fora ou o
   dispositivo não estiver REGISTERED, nada é persistido e o carrinho fica intacto;
4. como o JSR conclui na hora, o pedido nasce já ``PAID``.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..config import settings
from ..custauth import current_customer, ensure_self
from ..db import get_session
from ..integration import get_client, get_integration, is_configured, pix_qr_available
from ..models import Cart, CartItem, Customer, Order, OrderItem, Product
from ..payments import PaymentInitiatorError
from ..pix import build_br_code
from ..routers.devices import get_device
from ..schemas import CheckoutIn, OrderOut
from ..serializers import order_to_out

router = APIRouter(prefix="/orders", tags=["pedidos"])


def _items_of(session: Session, order: Order) -> list[OrderItem]:
    return session.exec(select(OrderItem).where(OrderItem.order_id == order.id)).all()


def _need_enrollment(message: str, login_url: str = "") -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "NEED_ENROLLMENT",
            "need_enrollment": True,
            "message": message,
            "login_url": login_url,
        },
    )


@router.post("/checkout", response_model=OrderOut, status_code=201)
def checkout(
    data: CheckoutIn,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> OrderOut:
    ensure_self(customer, data.customer_id)

    cart = session.exec(
        select(Cart).where(Cart.customer_id == data.customer_id)
    ).first()
    cart_items = (
        session.exec(select(CartItem).where(CartItem.cart_id == cart.id)).all()
        if cart
        else []
    )
    if not cart_items:
        raise HTTPException(status_code=400, detail="Carrinho vazio")

    # 1. Confere estoque e calcula o total, guardando os produtos para o débito.
    total = 0.0
    linhas: list[tuple[Product, CartItem]] = []
    for item in cart_items:
        product = session.get(Product, item.product_id)
        if not product or not product.active:
            raise HTTPException(
                status_code=409, detail=f"Produto {item.product_id} indisponível"
            )
        if product.stock < item.quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Estoque insuficiente de '{product.name}' "
                f"(disponível: {product.stock})",
            )
        total += item.unit_price * item.quantity
        linhas.append((product, item))
    total = round(total, 2)

    cfg = get_integration(session)

    def _persist_items(order: Order) -> None:
        """Grava os itens (snapshot) e baixa o estoque; esvazia o carrinho."""
        for product, item in linhas:
            session.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    name=product.name,
                    unit_price=item.unit_price,
                    quantity=item.quantity,
                )
            )
            product.stock -= item.quantity
            session.add(product)
            session.delete(item)
        session.commit()

    # --- PIX QR clássico (copia e cola) -----------------------------------
    if data.method == "pix_qr":
        if not pix_qr_available(cfg):
            raise HTTPException(
                status_code=409,
                detail={"error": "PIX_QR_UNAVAILABLE",
                        "message": "Pagamento por PIX (QR) indisponível no momento."},
            )
        order = Order(
            customer_id=data.customer_id,
            address_id=data.address_id,
            status="AWAITING_PAYMENT",
            total=total,
            payment_method="pix_qr",
            payment_status="AWAITING_PAYMENT",
        )
        session.add(order)
        session.commit()
        session.refresh(order)
        # BR Code com o pedido como txid (o recebedor identifica o pagamento).
        order.pix_code = build_br_code(
            key=cfg.sebo_pix_key_value,
            merchant_name=cfg.sebo_name,
            merchant_city=cfg.sebo_city,
            amount=total,
            txid=f"SEBO{order.id}",
        )
        session.add(order)
        _persist_items(order)
        session.refresh(order)
        return order_to_out(order, _items_of(session, order))

    # --- PIX JSR (Open Finance, sem redirect) -----------------------------
    # 2. Open Finance precisa estar configurado (dados de integração no admin).
    if not is_configured(cfg):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "OPEN_FINANCE_UNAVAILABLE",
                "message": "Pagamento por Open Finance não está disponível no momento.",
            },
        )

    # 3. Precisa que o cliente (titular) tenha autorizado o Sebo a pagar por JSR.
    device = get_device(session, data.customer_id)
    if not device:
        raise _need_enrollment("Autorize o pagamento por PIX para concluir a compra.")

    # 4. Paga sem redirect ANTES de persistir. Iniciadora fora => 502; dispositivo
    #    não REGISTERED => 409 need_enrollment; ambos deixam o carrinho intacto.
    try:
        result = get_client(cfg).pay_jsr(f"{total:.2f}", device.enrollment_id)
    except PaymentInitiatorError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "PAYMENT_FAILED",
                "message": str(exc),
                "initiator": exc.detail,
            },
        ) from exc

    if result.get("need_enrollment"):
        device.status = "PENDING"
        session.add(device)
        session.commit()
        raise _need_enrollment(
            result.get("message", "Conclua o vínculo do dispositivo."),
            result.get("login_url", ""),
        )

    device.status = "REGISTERED"
    session.add(device)

    # 5. JSR concluiu: grava o pedido já pago, baixa o estoque e esvazia o carrinho.
    order = Order(
        customer_id=data.customer_id,
        address_id=data.address_id,
        status="PAID",
        total=total,
        payment_method="jsr",
        payment_id=result.get("payment_id", ""),
        payment_consent_id=result.get("consent_id", ""),
        payment_status=result.get("status", "COMPLETED"),
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    _persist_items(order)
    session.refresh(order)
    return order_to_out(order, _items_of(session, order))


@router.get("", response_model=list[OrderOut])
def list_orders(session: Session = Depends(get_session)) -> list[OrderOut]:
    orders = session.exec(select(Order).order_by(Order.created_at.desc())).all()
    return [order_to_out(o, _items_of(session, o)) for o in orders]


@router.get("/{order_id}", response_model=OrderOut)
def get_order(order_id: int, session: Session = Depends(get_session)) -> OrderOut:
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    return order_to_out(order, _items_of(session, order))


@router.post("/{order_id}/confirm-pix", response_model=OrderOut)
def confirm_pix(
    order_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> OrderOut:
    """Confirma o recebimento do PIX (QR) e marca o pedido como pago.

    Numa loja real, quem confirma é o PSP (webhook) ao receber o PIX. Nesta
    demo, sem PSP, a confirmação é acionada aqui — o estoque já foi baixado no
    checkout.
    """
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    ensure_self(customer, order.customer_id)
    if order.payment_method != "pix_qr":
        raise HTTPException(status_code=409, detail="Pedido não é PIX QR")
    if order.status == "PAID":
        return order_to_out(order, _items_of(session, order))
    if order.status != "AWAITING_PAYMENT":
        raise HTTPException(status_code=409, detail=f"Pedido está {order.status}")

    order.status = "PAID"
    order.payment_status = "COMPLETED"
    session.add(order)
    session.commit()
    session.refresh(order)
    return order_to_out(order, _items_of(session, order))


@router.post("/{order_id}/openfinance", response_model=OrderOut)
def start_openfinance_redirect(
    order_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> OrderOut:
    """Inicia a jornada de pagamento com **redirect** (consentimento único).

    Alternativa ao copia-e-cola para um pedido PIX QR ainda em aberto: a loja
    pede à iniciadora um pagamento (``POST /payments``) do valor do pedido para a
    chave do Sebo, amarrado ao CPF do cliente, e devolve a ``authorisation_url``
    (em ``payment_login_url``). O cliente autoriza na detentora e volta.
    """
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    ensure_self(customer, order.customer_id)
    if order.payment_method != "pix_qr":
        raise HTTPException(status_code=409, detail="Pedido não é PIX QR")
    if order.status != "AWAITING_PAYMENT":
        raise HTTPException(status_code=409, detail=f"Pedido está {order.status}")

    cfg = get_integration(session)
    if not is_configured(cfg):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "OPEN_FINANCE_UNAVAILABLE",
                "message": "Pagamento por Open Finance não está disponível no momento.",
            },
        )

    # Ao aprovar na detentora, o cliente volta para o checkout deste pedido.
    redirect_uri = (
        f"{settings.frontend_origin.rstrip('/')}/checkout?order={order.id}&pay=return"
    )
    try:
        result = get_client(cfg).create_payment(
            f"{order.total:.2f}",
            debtor_cpf=customer.cpf or "",
            redirect_uri=redirect_uri,
        )
    except PaymentInitiatorError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "PAYMENT_FAILED", "message": str(exc), "initiator": exc.detail},
        ) from exc

    order.payment_consent_id = result["consent_id"]
    order.payment_login_url = result["authorisation_url"]
    order.payment_status = result.get("status", "AWAITING_AUTHORISATION")
    session.add(order)
    session.commit()
    session.refresh(order)
    return order_to_out(order, _items_of(session, order))


@router.post("/{order_id}/confirm-openfinance", response_model=OrderOut)
def confirm_openfinance(
    order_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> OrderOut:
    """Reconciliação: consulta o pagamento na iniciadora e marca o pedido.

    Chamado quando o cliente volta da detentora. A fonte da verdade é a
    iniciadora (``GET /payments/{consent_id}``), não o parâmetro da URL: se há
    ``payment_id`` e o status não é recusado, o pedido vira ``PAID``.
    """
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    ensure_self(customer, order.customer_id)
    if order.payment_method != "pix_qr":
        raise HTTPException(status_code=409, detail="Pedido não é PIX QR")
    if order.status == "PAID":
        return order_to_out(order, _items_of(session, order))
    if not order.payment_consent_id:
        raise HTTPException(
            status_code=409, detail="Pagamento por Open Finance não foi iniciado"
        )

    cfg = get_integration(session)
    try:
        st = get_client(cfg).get_status(order.payment_consent_id)
    except PaymentInitiatorError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "STATUS_FAILED", "message": str(exc), "initiator": exc.detail},
        ) from exc

    status = (st.get("status") or "").upper()
    order.payment_status = status
    order.payment_id = st.get("payment_id", "") or order.payment_id
    # Submetido (tem payment_id) e não recusado => pago. O pedido nasceu
    # AWAITING_PAYMENT com o estoque já baixado no checkout; só falta o pago.
    if order.payment_id and status not in ("REJECTED", "CANCELLED"):
        order.status = "PAID"
    session.add(order)
    session.commit()
    session.refresh(order)
    return order_to_out(order, _items_of(session, order))


@router.post("/{order_id}/cancel", response_model=OrderOut)
def cancel_order(order_id: int, session: Session = Depends(get_session)) -> OrderOut:
    """Cancela um pedido ainda não pago e devolve os itens ao estoque."""
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if order.status == "PAID":
        raise HTTPException(status_code=409, detail="Pedido já foi pago")
    if order.status == "CANCELLED":
        return order_to_out(order, _items_of(session, order))

    for item in _items_of(session, order):
        product = session.get(Product, item.product_id)
        if product:
            product.stock += item.quantity
            session.add(product)
    order.status = "CANCELLED"
    session.add(order)
    session.commit()
    session.refresh(order)
    return order_to_out(order, _items_of(session, order))
