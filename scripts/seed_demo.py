"""Popula o banco com clientes e pedidos de exemplo, para o painel admin ter
dados (dashboard de vendas, pedidos, clientes) numa demonstração.

Uso, a partir de ``backend/``:

    uv run python -m scripts.seed_demo

Não faz parte do fluxo da aplicação e é idempotente: se já houver pedidos, não
duplica. Para recomeçar do zero, apague ``data/sebo.db`` e suba a aplicação uma
vez (o catálogo é semeado sozinho) antes de rodar este script.
"""

import random
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.custauth import hash_password
from app.db import engine, init_db
from app.models import Customer, Order, OrderItem, Product

DEMO_PASSWORD = "cliente123"  # senha dos clientes de exemplo (para poder logar)

CLIENTES = [
    ("Maria Leitora", "maria@sebo.com", "11999990001", "111.111.111-11"),
    ("João Comprador", "joao@sebo.com", "11999990002", "222.222.222-22"),
    ("Ana Colecionadora", "ana@sebo.com", "11999990003", "333.333.333-33"),
    ("Carlos Garimpeiro", "carlos@sebo.com", "11999990004", ""),
]

N_PEDIDOS = 22


def main() -> None:
    init_db()
    random.seed(7)

    with Session(engine) as s:
        products = s.exec(select(Product)).all()
        if not products:
            print("Catálogo vazio — suba a aplicação uma vez para semear os produtos.")
            return

        customers = {}
        for name, email, phone, cpf in CLIENTES:
            existing = s.exec(select(Customer).where(Customer.email == email)).first()
            if existing:
                customers[email] = existing
                continue
            c = Customer(
                name=name, email=email, phone=phone, cpf=cpf,
                password_hash=hash_password(DEMO_PASSWORD),
            )
            s.add(c)
            s.commit()
            s.refresh(c)
            customers[email] = c

        if s.exec(select(Order)).first():
            print("Já existem pedidos; nada a fazer (idempotente).")
            return

        now = datetime.now(timezone.utc)
        emails = list(customers)
        for _ in range(N_PEDIDOS):
            cust = customers[random.choice(emails)]
            created = now - timedelta(days=random.randint(0, 13), hours=random.randint(0, 20))
            r = random.random()
            status = "PAID" if r < 0.82 else ("AWAITING_PAYMENT" if r < 0.93 else "CANCELLED")

            order = Order(customer_id=cust.id, status=status, total=0.0, created_at=created)
            s.add(order)
            s.commit()
            s.refresh(order)

            total = 0.0
            for p in random.sample(products, k=random.randint(1, 3)):
                qty = random.randint(1, 2)
                s.add(OrderItem(order_id=order.id, product_id=p.id, name=p.name,
                                unit_price=p.price, quantity=qty))
                total += p.price * qty
            order.total = round(total, 2)
            if status == "PAID":
                order.payment_status = "COMPLETED"
                order.payment_id = f"pay-demo-{order.id}"
            s.add(order)
            s.commit()

        pagos = s.exec(select(Order).where(Order.status == "PAID")).all()
        print(f"OK — {len(customers)} clientes, {N_PEDIDOS} pedidos "
              f"({len(pagos)} pagos), receita R$ {round(sum(o.total for o in pagos), 2)}.")


if __name__ == "__main__":
    main()
