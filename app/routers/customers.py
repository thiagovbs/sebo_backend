"""Clientes: cadastro, endereços, formas de pagamento e histórico de pedidos."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..custauth import (
    create_customer_token,
    current_customer,
    ensure_self,
    hash_password,
    verify_password,
)
from ..db import get_session
from ..models import Address, Customer, Order, OrderItem, PaymentMethod
from ..schemas import (
    AddressIn,
    AddressOut,
    CustomerAuthOut,
    CustomerLoginIn,
    CustomerOut,
    OrderOut,
    PaymentMethodIn,
    PaymentMethodOut,
    RegisterIn,
)
from ..serializers import order_to_out

router = APIRouter(prefix="/customers", tags=["clientes"])


def _email_taken(session: Session) -> HTTPException:
    return HTTPException(status_code=409, detail="Já existe cliente com esse e-mail")


def _get_customer(session: Session, customer_id: int) -> Customer:
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    return customer


def _auth_out(customer: Customer) -> CustomerAuthOut:
    token = create_customer_token(customer.id)
    return CustomerAuthOut(
        token=token["token"],
        expires_in=token["expires_in"],
        customer=CustomerOut.model_validate(customer, from_attributes=True),
    )


# ---------------------------------------------------------------------------
# Autocadastro / login
# ---------------------------------------------------------------------------


@router.post("/register", response_model=CustomerAuthOut, status_code=201)
def register(data: RegisterIn, session: Session = Depends(get_session)) -> CustomerAuthOut:
    """Autocadastro: cria o cliente (com senha) e já devolve o token de acesso.

    É o que o próprio cliente preenche. Tudo numa transação: se o e-mail já
    existe, nada é gravado.
    """
    customer = Customer(
        name=data.name,
        email=data.email,
        password_hash=hash_password(data.password),
        phone=data.phone,
        cpf=data.cpf,
        birth_date=data.birth_date,
    )
    session.add(customer)
    try:
        session.flush()  # obtém o id sem encerrar a transação
    except IntegrityError as exc:
        session.rollback()
        raise _email_taken(session) from exc

    # Só o primeiro marcado como padrão vale; se nenhum, o primeiro da lista.
    _apply_first_default(data.addresses)
    for addr in data.addresses:
        session.add(Address(customer_id=customer.id, **addr.model_dump()))

    _apply_first_default(data.payment_methods)
    for pm in data.payment_methods:
        session.add(PaymentMethod(customer_id=customer.id, **pm.model_dump()))

    session.commit()
    session.refresh(customer)
    return _auth_out(customer)


@router.post("/login", response_model=CustomerAuthOut)
def login(data: CustomerLoginIn, session: Session = Depends(get_session)) -> CustomerAuthOut:
    """Login do cliente: e-mail + senha -> token de acesso."""
    customer = session.exec(
        select(Customer).where(Customer.email == data.email)
    ).first()
    if not customer or not verify_password(data.password, customer.password_hash):
        # Mesma resposta para e-mail inexistente e senha errada.
        raise HTTPException(
            status_code=401,
            detail="E-mail ou senha inválidos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _auth_out(customer)


@router.get("/me", response_model=CustomerOut)
def me(customer: Customer = Depends(current_customer)) -> CustomerOut:
    """Devolve o cliente do token (o front usa para restaurar a sessão)."""
    return CustomerOut.model_validate(customer, from_attributes=True)


def _apply_first_default(items: list) -> None:
    """Garante no máximo um ``is_default``; se nenhum, promove o primeiro."""
    if not items:
        return
    marked = [i for i in items if getattr(i, "is_default", False)]
    if not marked:
        items[0].is_default = True
    else:
        for extra in marked[1:]:
            extra.is_default = False


# ---------------------------------------------------------------------------
# Endereços
# ---------------------------------------------------------------------------


@router.get("/{customer_id}/addresses", response_model=list[AddressOut])
def list_addresses(
    customer_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> list[AddressOut]:
    ensure_self(customer, customer_id)
    rows = session.exec(
        select(Address).where(Address.customer_id == customer_id)
    ).all()
    return [AddressOut.model_validate(a, from_attributes=True) for a in rows]


@router.post("/{customer_id}/addresses", response_model=AddressOut, status_code=201)
def add_address(
    customer_id: int,
    data: AddressIn,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> AddressOut:
    ensure_self(customer, customer_id)
    address = Address(customer_id=customer_id, **data.model_dump())
    if address.is_default:
        _clear_default_addresses(session, customer_id)
    session.add(address)
    session.commit()
    session.refresh(address)
    return AddressOut.model_validate(address, from_attributes=True)


@router.delete("/{customer_id}/addresses/{address_id}", status_code=204)
def delete_address(
    customer_id: int,
    address_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> None:
    ensure_self(customer, customer_id)
    address = session.get(Address, address_id)
    if not address or address.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="Endereço não encontrado")
    session.delete(address)
    session.commit()


def _clear_default_addresses(session: Session, customer_id: int) -> None:
    for other in session.exec(
        select(Address).where(
            Address.customer_id == customer_id, Address.is_default == True  # noqa: E712
        )
    ).all():
        other.is_default = False
        session.add(other)


# ---------------------------------------------------------------------------
# Formas de pagamento
# ---------------------------------------------------------------------------


@router.get("/{customer_id}/payment-methods", response_model=list[PaymentMethodOut])
def list_payment_methods(
    customer_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> list[PaymentMethodOut]:
    ensure_self(customer, customer_id)
    rows = session.exec(
        select(PaymentMethod).where(PaymentMethod.customer_id == customer_id)
    ).all()
    return [PaymentMethodOut.model_validate(p, from_attributes=True) for p in rows]


@router.post(
    "/{customer_id}/payment-methods", response_model=PaymentMethodOut, status_code=201
)
def add_payment_method(
    customer_id: int,
    data: PaymentMethodIn,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> PaymentMethodOut:
    ensure_self(customer, customer_id)
    method = PaymentMethod(customer_id=customer_id, **data.model_dump())
    if method.is_default:
        for other in session.exec(
            select(PaymentMethod).where(
                PaymentMethod.customer_id == customer_id,
                PaymentMethod.is_default == True,  # noqa: E712
            )
        ).all():
            other.is_default = False
            session.add(other)
    session.add(method)
    session.commit()
    session.refresh(method)
    return PaymentMethodOut.model_validate(method, from_attributes=True)


@router.delete(
    "/{customer_id}/payment-methods/{method_id}", status_code=204
)
def delete_payment_method(
    customer_id: int,
    method_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> None:
    ensure_self(customer, customer_id)
    method = session.get(PaymentMethod, method_id)
    if not method or method.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="Forma de pagamento não encontrada")
    session.delete(method)
    session.commit()


# ---------------------------------------------------------------------------
# Histórico de pedidos
# ---------------------------------------------------------------------------


@router.get("/{customer_id}/orders", response_model=list[OrderOut])
def customer_orders(
    customer_id: int,
    session: Session = Depends(get_session),
    customer: Customer = Depends(current_customer),
) -> list[OrderOut]:
    """Histórico de pedidos do cliente, do mais recente para o mais antigo."""
    ensure_self(customer, customer_id)
    orders = session.exec(
        select(Order)
        .where(Order.customer_id == customer_id)
        .order_by(Order.created_at.desc())
    ).all()

    result: list[OrderOut] = []
    for order in orders:
        items = session.exec(
            select(OrderItem).where(OrderItem.order_id == order.id)
        ).all()
        result.append(order_to_out(order, items))
    return result
