"""Montagem dos schemas de saída compostos (reaproveitados entre routers)."""

from .boleto import BoletoError, formatar_linha
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
        card_brand=order.card_brand,
        card_last4=order.card_last4,
        card_authorization=order.card_authorization,
        boleto_digitable_line=order.boleto_digitable_line,
        # A versão pontuada é derivada na saída, e não guardada: são os mesmos
        # 47 dígitos, e duas cópias do mesmo dado divergem na primeira correção.
        boleto_digitable_line_formatted=linha_formatada(order.boleto_digitable_line),
        boleto_barcode=order.boleto_barcode,
        boleto_our_number=order.boleto_our_number,
        boleto_due_date=order.boleto_due_date,
    )


def linha_formatada(linha: str) -> str:
    """A linha digitável com a pontuação em que ela é impressa.

    Pública porque a ficha do boleto imprime a mesma linha do pedido: duas
    pontuações diferentes para o mesmo número confundiriam quem confere."""
    if not linha:
        return ""
    try:
        return formatar_linha(linha)
    except BoletoError:
        # Linha gravada por versão anterior ou corrompida: mostra o que há, sem
        # derrubar a leitura do pedido por causa da pontuação.
        return linha
