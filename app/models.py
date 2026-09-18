"""Tabelas do Sebo On-Line (SQLModel/SQLite).

Sem ``Relationship``: como no payment-initiator, as tabelas guardam só as
chaves estrangeiras e os joins são feitos por consultas explícitas em
``store.py``. Evita instâncias destacadas (``DetachedInstanceError``) quando o
objeto sai da sessão e mantém o padrão do repositório-irmão.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IntegrationSettings(SQLModel, table=True):
    """Dados de integração com a iniciadora (aplicação pagadora).

    Linha única (id=1), editável no admin em tempo de execução. Enquanto não
    estiver completa, o pagamento por Open Finance fica indisponível para o
    cliente. Guarda as credenciais do lojista na iniciadora e os dados do
    recebedor do PIX (o próprio sebo).
    """

    id: Optional[int] = Field(default=1, primary_key=True)
    payment_initiator_url: str = ""
    payment_initiator_user: str = ""
    payment_initiator_password: str = ""
    sebo_name: str = ""
    sebo_cpf_cnpj: str = ""
    sebo_city: str = "SAO PAULO"  # cidade do recebedor (BR Code do PIX)
    sebo_pix_key_type: str = "CNPJ"
    sebo_pix_key_value: str = ""
    updated_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------


class Product(SQLModel, table=True):
    """Produto do catálogo — de qualquer tipo (o sebo anuncia de tudo).

    Os campos que governam a vitrine — ``name`` (busca), ``category`` (filtro),
    ``price`` e ``created_at`` (ordenação) — são cadastrados aqui, em cada
    produto. ``brand`` (marca/fabricante) e ``condition`` (novo, usado,
    seminovo) são filtros/atributos extras, opcionais, que servem a qualquer
    categoria de produto.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: str = ""
    category: str = Field(default="", index=True)
    price: float = 0.0
    stock: int = 0
    image_url: str = ""
    brand: str = ""
    condition: str = ""  # ex.: "novo", "seminovo", "usado - bom estado"
    active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------


class Customer(SQLModel, table=True):
    """Cliente do sebo. O e-mail identifica e é único.

    O próprio cliente se cadastra (autocadastro), com os dados que a loja usa na
    hora do pagamento junto à iniciadora — em especial ``cpf`` (identifica o
    pagador) e ``birth_date``.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""
    email: str = Field(index=True, unique=True)  # é o "usuário" do login
    password_hash: str = ""
    phone: str = ""
    cpf: str = ""
    birth_date: str = ""  # ISO (YYYY-MM-DD)
    created_at: datetime = Field(default_factory=_now)


class Address(SQLModel, table=True):
    """Endereço de entrega de um cliente."""

    id: Optional[int] = Field(default=None, primary_key=True)
    customer_id: int = Field(index=True, foreign_key="customer.id")
    label: str = "Principal"  # ex.: "Casa", "Trabalho"
    street: str = ""
    number: str = ""
    complement: str = ""
    district: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    is_default: bool = False


class PaymentMethod(SQLModel, table=True):
    """Cartão de crédito salvo do cliente.

    O pagamento do checkout é PIX via Open Finance (jornada JSR pela iniciadora,
    mostrada só no checkout). Os cartões ficam guardados como preferência do
    cliente. Guardamos apenas dados **não sensíveis**: bandeira, 4 últimos
    dígitos, titular e validade — nunca o número completo nem o CVV.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    customer_id: int = Field(index=True, foreign_key="customer.id")
    label: str = ""  # apelido, ex.: "Cartão principal"
    brand: str = ""  # Visa | Mastercard | Elo | Amex | Outra
    last4: str = ""  # 4 últimos dígitos
    holder: str = ""  # nome impresso no cartão
    expiry: str = ""  # validade MM/AA
    is_default: bool = False


# ---------------------------------------------------------------------------
# Carrinho
# ---------------------------------------------------------------------------


class Device(SQLModel, table=True):
    """Autorização de pagamento do cliente na iniciadora (jornada JSR).

    O cliente é o titular da conta; ele autoriza o **Sebo On-Line** (que age como
    o dispositivo) a iniciar PIX na conta dele. A autorização é feita uma vez, o
    cliente autenticando no seu banco; a partir daí o pagamento é sem redirect.
    Guarda o ``enrollment_id`` que a loja usa ao chamar ``/payments/jsr``. Uma
    autorização por cliente (a mais recente vale).
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    customer_id: int = Field(index=True, unique=True, foreign_key="customer.id")
    enrollment_id: str = Field(default="", index=True)
    status: str = "PENDING"  # PENDING | REGISTERED
    account_id: str = ""
    created_at: datetime = Field(default_factory=_now)


class Cart(SQLModel, table=True):
    """Carrinho ativo de um cliente (um por cliente)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    customer_id: int = Field(index=True, unique=True, foreign_key="customer.id")
    created_at: datetime = Field(default_factory=_now)


class CartItem(SQLModel, table=True):
    """Item do carrinho. ``unit_price`` é fotografado na hora de adicionar."""

    id: Optional[int] = Field(default=None, primary_key=True)
    cart_id: int = Field(index=True, foreign_key="cart.id")
    product_id: int = Field(foreign_key="product.id")
    quantity: int = 1
    unit_price: float = 0.0


# ---------------------------------------------------------------------------
# Pedidos e pagamento
# ---------------------------------------------------------------------------


class Order(SQLModel, table=True):
    """Pedido gerado a partir do carrinho no checkout.

    O bloco ``payment_*`` guarda o vínculo com o payment-initiator: ``login_url``
    é para onde o cliente vai autenticar no banco; ``request_id`` é o ``state``
    que identifica o fluxo lá; e ``payment_status`` é atualizado ao consultar a
    iniciadora.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    customer_id: int = Field(index=True, foreign_key="customer.id")
    address_id: Optional[int] = Field(default=None, foreign_key="address.id")
    status: str = Field(default="CREATED", index=True)
    # CREATED | AWAITING_PAYMENT | PAID | CANCELLED | FAILED
    total: float = 0.0
    created_at: datetime = Field(default_factory=_now, index=True)

    payment_method: str = "jsr"  # jsr | pix_qr
    payment_request_id: str = ""
    payment_consent_id: str = ""
    payment_id: str = ""
    payment_login_url: str = ""
    payment_status: str = ""
    pix_code: str = ""  # BR Code (copia e cola) quando payment_method == pix_qr

    # Avança a cada gravação (``onupdate`` é do próprio SQLAlchemy, então nenhum
    # ponto de escrita precisa lembrar de atualizar). É o carimbo que o
    # omnicommerce usa para descartar evento que chega fora de ordem.
    updated_at: datetime = Field(
        default_factory=_now, sa_column_kwargs={"onupdate": _now}
    )


class OrderItem(SQLModel, table=True):
    """Item do pedido. Nome e preço são fotografados do produto na compra."""

    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(index=True, foreign_key="order.id")
    product_id: int = Field(foreign_key="product.id")
    name: str = ""
    unit_price: float = 0.0
    quantity: int = 1


# ---------------------------------------------------------------------------
# Integração com o omnicommerce
# ---------------------------------------------------------------------------


class OutboxNotification(SQLModel, table=True):
    """Aviso de pedido a entregar ao omnicommerce.

    Existe para não perder venda: o POST direto falharia em silêncio numa
    queda de rede. A linha é gravada junto com a mudança do pedido e só sai
    da fila quando o destino confirma.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(index=True, foreign_key="order.id")
    topic: str = "orders"
    # Vai no campo ``sent`` do aviso e é estável entre tentativas: é o que dá
    # identidade ao evento do outro lado, evitando duplicar na repetição.
    sent_at: datetime = Field(default_factory=_now)
    status: str = Field(default="PENDING", index=True)  # PENDING | DELIVERED | FAILED
    attempts: int = 0
    next_attempt_at: datetime = Field(default_factory=_now, index=True)
    last_error: str = ""
    delivered_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)
