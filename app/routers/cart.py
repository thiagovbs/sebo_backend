"""Carrinho: um por cliente, com itens que fotografam o preço na adição."""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..custauth import current_customer, ensure_self
from ..db import get_session
from ..models import Cart, CartItem, Customer, Product
from ..schemas import CartItemIn, CartItemOut, CartOut

router = APIRouter(prefix="/cart", tags=["carrinho"])


def _get_or_create_cart(session: Session, customer_id: int) -> Cart:
    if not session.get(Customer, customer_id):
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    cart = session.exec(
        select(Cart).where(Cart.customer_id == customer_id)
    ).first()
    if not cart:
        cart = Cart(customer_id=customer_id)
        session.add(cart)
        session.commit()
        session.refresh(cart)
    return cart


def _build_cart_out(session: Session, cart: Cart) -> CartOut:
    items = session.exec(select(CartItem).where(CartItem.cart_id == cart.id)).all()
    out_items: list[CartItemOut] = []
    total = 0.0
    for item in items:
        product = session.get(Product, item.product_id)
        line_total = round(item.unit_price * item.quantity, 2)
        total += line_total
        out_items.append(
            CartItemOut(
                id=item.id,
                product_id=item.product_id,
                name=product.name if product else "(produto removido)",
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=line_total,
                image_url=product.image_url if product else "",
            )
        )
    return CartOut(customer_id=cart.customer_id, items=out_items, total=round(total, 2))


@router.get("/{customer_id}", response_model=CartOut)
def get_cart(
    customer_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> CartOut:
    ensure_self(customer, customer_id)
    cart = _get_or_create_cart(session, customer_id)
    return _build_cart_out(session, cart)


@router.post("/{customer_id}/items", response_model=CartOut, status_code=201)
def add_item(
    customer_id: int,
    data: CartItemIn,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> CartOut:
    """Adiciona um produto. Se já estiver no carrinho, soma a quantidade."""
    ensure_self(customer, customer_id)
    cart = _get_or_create_cart(session, customer_id)
    product = session.get(Product, data.product_id)
    if not product or not product.active:
        raise HTTPException(status_code=404, detail="Produto indisponível")
    if product.stock < data.quantity:
        raise HTTPException(
            status_code=409, detail=f"Estoque insuficiente (disponível: {product.stock})"
        )

    existing = session.exec(
        select(CartItem).where(
            CartItem.cart_id == cart.id, CartItem.product_id == data.product_id
        )
    ).first()
    if existing:
        existing.quantity += data.quantity
        existing.unit_price = product.price
        session.add(existing)
    else:
        session.add(
            CartItem(
                cart_id=cart.id,
                product_id=data.product_id,
                quantity=data.quantity,
                unit_price=product.price,
            )
        )
    session.commit()
    return _build_cart_out(session, cart)


@router.put("/{customer_id}/items/{item_id}", response_model=CartOut)
def update_item(
    customer_id: int,
    item_id: int,
    data: CartItemIn,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> CartOut:
    """Ajusta a quantidade de um item (``quantity`` >= 1)."""
    ensure_self(customer, customer_id)
    cart = _get_or_create_cart(session, customer_id)
    item = session.get(CartItem, item_id)
    if not item or item.cart_id != cart.id:
        raise HTTPException(status_code=404, detail="Item não está no carrinho")
    product = session.get(Product, item.product_id)
    if product and product.stock < data.quantity:
        raise HTTPException(
            status_code=409, detail=f"Estoque insuficiente (disponível: {product.stock})"
        )
    item.quantity = data.quantity
    session.add(item)
    session.commit()
    return _build_cart_out(session, cart)


@router.delete("/{customer_id}/items/{item_id}", response_model=CartOut)
def remove_item(
    customer_id: int,
    item_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> CartOut:
    ensure_self(customer, customer_id)
    cart = _get_or_create_cart(session, customer_id)
    item = session.get(CartItem, item_id)
    if not item or item.cart_id != cart.id:
        raise HTTPException(status_code=404, detail="Item não está no carrinho")
    session.delete(item)
    session.commit()
    return _build_cart_out(session, cart)


@router.delete("/{customer_id}", status_code=204)
def clear_cart(
    customer_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> None:
    ensure_self(customer, customer_id)
    cart = _get_or_create_cart(session, customer_id)
    for item in session.exec(select(CartItem).where(CartItem.cart_id == cart.id)).all():
        session.delete(item)
    session.commit()
