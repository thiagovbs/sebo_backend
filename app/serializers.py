"""Montagem dos schemas de saída compostos (reaproveitados entre routers)."""

from .models import Order, OrderItem
from .schemas import OrderItemOut, OrderOut


def order_to_out(order: Order, items: list[OrderItem]) -> OrderOut:
    return OrderOut(
        id=order.id,
        customer_id=order.customer_id,
        address_id=order.address_id,
        status=order.status,
        total=order.total,
        created_at=order.created_at,
        items=[
            OrderItemOut(
                product_id=i.product_id,
                name=i.name,
                unit_price=i.unit_price,
                quantity=i.quantity,
                line_total=round(i.unit_price * i.quantity, 2),
            )
            for i in items
        ],
        payment_method=order.payment_method,
        payment_login_url=order.payment_login_url,
        payment_status=order.payment_status,
        payment_request_id=order.payment_request_id,
        payment_id=order.payment_id,
        pix_code=order.pix_code,
    )
