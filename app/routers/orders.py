"""Pedidos e pagamento: quatro métodos, com desenhos diferentes de propósito.

- **cartão de crédito**: autoriza ANTES de persistir (como o JSR). Recusa do
  emissor não cria pedido nem baixa estoque, e o carrinho fica intacto. Aceita
  cartão salvo do cadastro ou um novo, digitado na hora -- e o número do cartão
  não é gravado nem registrado em log em nenhum dos dois caminhos.
- **boleto**: cria o pedido AWAITING_PAYMENT e emite a linha digitável, que é
  fotografada no pedido. O dinheiro chega depois, ou não chega -- e quem confirma
  que chegou é a LOJA, em `POST /admin/orders/{id}/confirm-payment`. O cliente
  não declara pagamento: ele não é quem vê o extrato.
- **PIX QR**: mesma forma do boleto, com BR Code em vez de linha digitável.
- **PIX JSR (Open Finance)**: paga sem redirect e nasce PAID.

O texto abaixo descreve o JSR, que foi o primeiro:

O cliente é o titular da conta e autoriza o Sebo (que age como o dispositivo) a
iniciar PIX na conta dele, uma vez (ver ``routers/devices.py``). No checkout:

1. valida o carrinho e o estoque e calcula o total;
2. se o cliente não autorizou o pagamento, responde ``409 need_enrollment``
   (o front leva o cliente a autorizar antes);
3. **paga sem redirect** (``/payments/jsr``) — se a iniciadora estiver fora ou o
   dispositivo não estiver REGISTERED, nada é persistido e o carrinho fica intacto;
4. como o JSR conclui na hora, o pedido nasce já ``PAID``.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select

from ..boleto import BoletoError, campo_livre
from ..boleto import emitir as emitir_boleto
from ..card import CardError, autorizar, autorizar_salvo
from ..config import settings
from ..custauth import current_customer, ensure_self
from ..db import get_session
from ..integration import (
    boleto_available,
    boleto_missing,
    get_client,
    get_integration,
    is_configured,
    pix_qr_available,
)
from ..models import Cart, CartItem, Customer, Order, OrderItem, PaymentMethod, Product
from ..omnicommerce import enqueue_order_event, flush_in_background
from ..payments import PaymentInitiatorError
from ..pix import build_br_code
from ..routers.devices import get_device
from ..schemas import CheckoutIn, OrderOut
from ..serializers import order_to_out

router = APIRouter(prefix="/orders", tags=["pedidos"])


def _items_of(session: Session, order: Order) -> list[OrderItem]:
    return session.exec(select(OrderItem).where(OrderItem.order_id == order.id)).all()


def _notify_omnicommerce(
    session: Session, order: Order, background: BackgroundTasks
) -> None:
    """Enfileira o aviso do pedido e tenta entregar depois de responder.

    Chamado sempre DEPOIS de os itens estarem gravados: o omnicommerce lê o
    pedido assim que recebe o aviso, e um pedido sem itens seria recusado.
    """
    if enqueue_order_event(session, order) is None:
        return
    session.commit()
    session.refresh(order)
    background.add_task(flush_in_background)


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
    background: BackgroundTasks,
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

    # --- Cartão de crédito ------------------------------------------------
    if data.method == "card":
        referencia = f"cliente-{data.customer_id}"
        try:
            if data.payment_method_id is not None:
                salvo = session.get(PaymentMethod, data.payment_method_id)
                # O cartão precisa ser DESTE cliente: sem esta conferência, um
                # id de outro cadastro cobraria o cartão de outra pessoa.
                if not salvo or salvo.customer_id != data.customer_id:
                    raise HTTPException(status_code=404, detail="Cartão não encontrado")
                autorizacao = autorizar_salvo(
                    brand=salvo.brand,
                    last4=salvo.last4,
                    holder=salvo.holder,
                    expiry=salvo.expiry,
                    valor=total,
                    referencia=referencia,
                )
            elif data.card is not None:
                autorizacao = autorizar(
                    numero=data.card.number,
                    holder=data.card.holder,
                    expiry=data.card.expiry,
                    cvv=data.card.cvv,
                    valor=total,
                    referencia=referencia,
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Escolha um cartão salvo ou informe os dados de um novo.",
                )
        except CardError as exc:
            # Dado errado é de quem digitou: a mensagem volta para a tela.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not autorizacao["approved"]:
            # Recusa do emissor: nada persistido, carrinho intacto. 402 é o
            # código para "pagamento exigido e não obtido".
            raise HTTPException(
                status_code=402,
                detail={"error": "CARD_DECLINED", "message": autorizacao["reason"]},
            )

        # Aprovado: só agora o pedido existe.
        order = Order(
            customer_id=data.customer_id,
            address_id=data.address_id,
            status="PAID",
            total=total,
            payment_method="card",
            payment_status="COMPLETED",
            card_brand=autorizacao["brand"],
            card_last4=autorizacao["last4"],
            card_authorization=autorizacao["authorization_code"],
        )
        session.add(order)
        session.commit()
        session.refresh(order)

        # Guardar o cartão é opção de quem compra, e só depois da aprovação:
        # salvar um cartão recusado deixaria lixo no cadastro.
        if data.card is not None and data.card.save:
            primeiro = not session.exec(
                select(PaymentMethod).where(
                    PaymentMethod.customer_id == data.customer_id
                )
            ).first()
            apelido = data.card.label or (
                autorizacao["brand"] + " ****" + autorizacao["last4"]
            )
            session.add(
                PaymentMethod(
                    customer_id=data.customer_id,
                    label=apelido[:60],
                    brand=autorizacao["brand"],
                    last4=autorizacao["last4"],
                    holder=autorizacao["holder"],
                    expiry=autorizacao["expiry"],
                    is_default=primeiro,
                )
            )
            session.commit()

        _persist_items(order)
        session.refresh(order)
        _notify_omnicommerce(session, order, background)
        return order_to_out(order, _items_of(session, order))

    # --- Boleto bancário --------------------------------------------------
    if data.method == "boleto":
        if not boleto_available(cfg):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "BOLETO_UNAVAILABLE",
                    "message": "Boleto indisponível: a loja não cadastrou os dados do"
                    " beneficiário. Faltam: " + ", ".join(boleto_missing(cfg)) + ".",
                },
            )
        # Confere a FORMA dos dados da conta antes de criar o pedido: agência
        # com dígito demais só apareceria na montagem, e aí já haveria pedido.
        try:
            campo_livre(
                agencia=cfg.boleto_agency,
                conta=cfg.boleto_account,
                carteira=cfg.boleto_wallet,
                nosso_numero="0",
            )
        except BoletoError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "BOLETO_INVALID_SETTINGS", "message": str(exc)},
            ) from exc

        order = Order(
            customer_id=data.customer_id,
            address_id=data.address_id,
            status="AWAITING_PAYMENT",
            total=total,
            payment_method="boleto",
            payment_status="AWAITING_PAYMENT",
        )
        session.add(order)
        session.commit()
        session.refresh(order)

        # O nosso número deriva do pedido, então o boleto só pode ser montado
        # depois de o pedido existir -- mesma ordem do PIX QR.
        try:
            documento = emitir_boleto(
                order_id=order.id,
                valor=total,
                banco=cfg.boleto_bank_code,
                agencia=cfg.boleto_agency,
                conta=cfg.boleto_account,
                carteira=cfg.boleto_wallet,
                dias_para_vencer=cfg.boleto_days_to_due,
            )
        except BoletoError as exc:
            # Desfaz o pedido: melhor não existir do que existir sem boleto,
            # esperando um pagamento que ninguém consegue fazer.
            session.delete(order)
            session.commit()
            raise HTTPException(
                status_code=409,
                detail={"error": "BOLETO_INVALID_SETTINGS", "message": str(exc)},
            ) from exc

        order.boleto_digitable_line = documento["digitable_line"]
        order.boleto_barcode = documento["barcode"]
        order.boleto_our_number = documento["our_number"]
        order.boleto_due_date = documento["due_date"]
        session.add(order)
        _persist_items(order)
        session.refresh(order)
        _notify_omnicommerce(session, order, background)
        return order_to_out(order, _items_of(session, order))

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
        _notify_omnicommerce(session, order, background)
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
    _notify_omnicommerce(session, order, background)
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
    background: BackgroundTasks,
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

    status_anterior = order.status
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
    if order.status != status_anterior:
        _notify_omnicommerce(session, order, background)
    return order_to_out(order, _items_of(session, order))


@router.post("/{order_id}/cancel", response_model=OrderOut)
def cancel_order(
    order_id: int,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> OrderOut:
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
    _notify_omnicommerce(session, order, background)
    return order_to_out(order, _items_of(session, order))
