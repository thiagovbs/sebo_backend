"""Schemas de entrada e saída da API (Pydantic)."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------------------
# Produtos
# ---------------------------------------------------------------------------


class ProductIn(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    category: str = ""
    price: float = Field(..., ge=0)
    stock: int = Field(0, ge=0)
    image_url: str = ""
    brand: str = ""
    condition: str = ""
    active: bool = True


class ProductOut(ProductIn):
    id: int
    created_at: datetime


class ProductPublishIn(ProductIn):
    """Produto publicado por outro sistema.

    O ``sku`` é obrigatório e é o que torna a publicação idempotente: quem
    republica o mesmo SKU atualiza o produto, não cria outro. Sem ele, uma
    retentativa depois de uma resposta perdida duplicaria o anúncio.
    """

    sku: str = Field(..., min_length=1, max_length=60)


class ProductList(BaseModel):
    items: list[ProductOut]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Clientes, endereços e formas de pagamento
# ---------------------------------------------------------------------------


class CustomerIn(BaseModel):
    name: str = ""
    email: EmailStr
    phone: str = ""
    cpf: str = ""
    birth_date: str = ""


class CustomerOut(BaseModel):
    id: int
    name: str
    email: str
    phone: str
    cpf: str
    birth_date: str = ""
    created_at: datetime


class AddressIn(BaseModel):
    label: str = "Principal"
    street: str = ""
    number: str = ""
    complement: str = ""
    district: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    is_default: bool = False


class AddressOut(AddressIn):
    id: int
    customer_id: int


class PaymentMethodIn(BaseModel):
    """Cartão de crédito. Só dados não sensíveis (sem número completo/CVV)."""

    label: str = ""
    brand: str = ""
    last4: str = ""
    holder: str = ""
    expiry: str = ""
    is_default: bool = False


class PaymentMethodOut(PaymentMethodIn):
    id: int
    customer_id: int


class RegisterIn(BaseModel):
    """Autocadastro: dados pessoais + endereços e formas de pagamento (N cada)."""

    name: str = ""
    email: EmailStr
    password: str = Field(..., min_length=6, description="Senha de acesso do cliente")
    phone: str = ""
    cpf: str = ""
    birth_date: str = ""
    addresses: list[AddressIn] = Field(default_factory=list)
    payment_methods: list[PaymentMethodIn] = Field(default_factory=list)


class CustomerLoginIn(BaseModel):
    email: EmailStr
    password: str


class CustomerAuthOut(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in: int
    customer: CustomerOut


# ---------------------------------------------------------------------------
# Carrinho
# ---------------------------------------------------------------------------


class CartItemIn(BaseModel):
    product_id: int
    quantity: int = Field(1, ge=1)


class CartItemOut(BaseModel):
    id: int
    product_id: int
    name: str
    quantity: int
    unit_price: float
    line_total: float
    image_url: str = ""


class CartOut(BaseModel):
    customer_id: int
    items: list[CartItemOut]
    total: float


# ---------------------------------------------------------------------------
# Pedidos / checkout
# ---------------------------------------------------------------------------


class IntegrationIn(BaseModel):
    """Edição da config de integração. A senha só é trocada se vier preenchida."""

    payment_initiator_url: str = ""
    payment_initiator_user: str = ""
    payment_initiator_password: Optional[str] = None
    sebo_name: str = ""
    sebo_cpf_cnpj: str = ""
    sebo_city: str = "SAO PAULO"
    sebo_pix_key_type: str = "CNPJ"
    sebo_pix_key_value: str = ""
    # Emissor do boleto: beneficiário e conta. Editável no admin porque trocar
    # de banco ou de endereço não pode exigir deploy.
    boleto_bank_code: str = ""
    boleto_bank_name: str = ""
    boleto_agency: str = ""
    boleto_account: str = ""
    boleto_wallet: str = ""
    boleto_days_to_due: int = 3
    boleto_instructions: str = ""
    sebo_street: str = ""
    sebo_number: str = ""
    sebo_complement: str = ""
    sebo_district: str = ""
    sebo_state: str = ""
    sebo_zip_code: str = ""


class IntegrationOut(BaseModel):
    payment_initiator_url: str
    payment_initiator_user: str
    has_password: bool  # nunca devolvemos a senha em si
    sebo_name: str
    sebo_cpf_cnpj: str
    sebo_city: str
    sebo_pix_key_type: str
    sebo_pix_key_value: str
    boleto_bank_code: str
    boleto_bank_name: str
    boleto_agency: str
    boleto_account: str
    boleto_wallet: str
    boleto_days_to_due: int
    boleto_instructions: str
    sebo_street: str
    sebo_number: str
    sebo_complement: str
    sebo_district: str
    sebo_state: str
    sebo_zip_code: str
    configured: bool       # JSR disponível
    pix_qr_available: bool  # PIX QR clássico disponível
    boleto_available: bool  # dados do emissor completos
    # O que falta para o boleto, em nome de campo legível: sem isto o admin
    # olha um método indisponível sem saber o que preencher.
    boleto_missing: list[str] = []


class CardIn(BaseModel):
    """Cartão digitado no checkout.

    O número e o código de segurança entram, são validados e NÃO são gravados
    em lugar nenhum -- nem em log. O que pode sobrar (se ``save``) é bandeira,
    quatro últimos dígitos, nome e validade, que é o que o cadastro do cliente
    já guardava antes disto existir.
    """

    number: str
    holder: str
    expiry: str  # MM/AA
    cvv: str
    label: str = ""
    save: bool = False


class CheckoutIn(BaseModel):
    customer_id: int
    address_id: Optional[int] = None
    method: str = "jsr"  # jsr | pix_qr | card | boleto
    # Cartão salvo do cliente. Exclusivo com ``card``: ou se usa um cartão
    # guardado, ou se digita outro.
    payment_method_id: Optional[int] = None
    card: Optional[CardIn] = None


class OrderItemOut(BaseModel):
    product_id: int
    name: str
    unit_price: float
    quantity: int
    line_total: float


class OrderOut(BaseModel):
    id: int
    customer_id: int
    address_id: Optional[int]
    status: str
    total: float
    created_at: datetime
    items: list[OrderItemOut]
    payment_method: str = "jsr"
    payment_login_url: str = ""
    payment_status: str = ""
    payment_request_id: str = ""
    payment_id: str = ""
    pix_code: str = ""
    card_brand: str = ""
    card_last4: str = ""
    card_authorization: str = ""
    boleto_digitable_line: str = ""
    boleto_digitable_line_formatted: str = ""
    boleto_barcode: str = ""
    boleto_our_number: str = ""
    boleto_due_date: str = ""
