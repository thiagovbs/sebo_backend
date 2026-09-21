"""API do Sebo On-Line: catálogo, clientes, carrinho e checkout via PIX."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .adminauth import warn_on_dev_defaults
from .custauth import warn_on_dev_secret
from .config import settings
from .db import init_db
from .routers import (
    admin,
    cart,
    customers,
    devices,
    integration,
    openfinance,
    orders,
    products,
)
from .seed import seed_if_empty

# A descrição da API mora aqui porque o contrato publicado no portal do gateway
# é gerado desta aplicação (scripts/gen_openapi.py). Enquanto ela existia só no
# portal, era a parte do contrato que nenhuma regeneração sabia reproduzir -- e
# que uma publicação apagaria sem avisar.
DESCRICAO = """\
API REST do Sebo On-Line, a loja que fecha a demonstração de Open Finance /
Pagamentos (PISP). Expõe o catálogo de produtos, a conta do cliente, o carrinho
e o checkout.

Construída em FastAPI + SQLModel (SQLite). Integra-se com o payment-initiator
(iniciadora PISP) e, por meio dele, com o core-banking (detentora da conta).

## Autenticação

Três realms independentes, cada um com o seu esquema Bearer no contrato:

- **AdminBearer** -- senha do painel (`POST /admin/login`); exigido nas rotas
  administrativas e nas escritas de catálogo.
- **CustomerBearer** -- login do cliente por e-mail e senha
  (`POST /customers/login`); cada cliente acessa apenas os próprios dados.
- **ServiceBearer** -- token de serviço (`INTEGRATION_TOKEN`) das rotas
  `/integration`: é máquina falando com máquina, não login de pessoa.

A loja também se autentica na iniciadora como o lojista `sebo_online`,
configurado em tempo de execução no admin e não exposto na API.

## Formas de pagamento

- **PIX Open Finance (JSR)** -- iniciado na iniciadora, sem redirect; exige o
  dispositivo do cliente autorizado.
- **PIX QR / copia e cola** -- exige só os dados do recebedor.
- **Cartão de crédito** -- um cartão já salvo do cliente ou um novo digitado no
  checkout. A autorização é simulada e o número do cartão não é armazenado.
- **Boleto** -- linha digitável e código de barras no padrão FEBRABAN, gerados
  com os dados do emissor configurados no admin.

PIX QR e boleto são confirmados **pelo admin**
(`POST /admin/orders/{order_id}/confirm-payment`), não pelo cliente: quem
recebe o dinheiro é quem declara que ele entrou.

## Recursos

- **Produtos** (`/products`) -- vitrine com busca, filtro por categoria e
  ordenação; CRUD e upload de imagem (armazenada em base64) restritos ao admin.
- **Clientes** (`/customers`) -- autocadastro e login, múltiplos endereços e
  cartões, histórico de pedidos.
- **Carrinho** (`/cart`) -- um por cliente, com controle de itens e estoque.
- **Pedidos** (`/orders`) -- checkout nas quatro formas de pagamento, consulta e
  cancelamento.
- **Dispositivo** (`/customers/{customer_id}/device`) -- autorização JSR: o
  cliente autoriza a loja a iniciar PIX na conta dele (enrollment via
  iniciadora).
- **Open Finance** (`/open-finance/status`) -- indica quais métodos de pagamento
  estão disponíveis conforme a configuração da integração.
- **Admin** (`/admin`) -- dashboard de vendas, pedidos, clientes, confirmação de
  pagamento e a seção de integração (iniciadora, recebedor do PIX e emissor do
  boleto).
- **Integração** (`/integration`) -- leitura de pedidos e de produtos por outro
  sistema (omnicommerce) e descarga da fila de avisos.
- **Infra** (`/health`) -- verificação de disponibilidade.

## Papéis na demo

- **Sebo On-Line** -- a loja (esta API): lojista e recebedor do PIX.
- **payment-initiator** -- a iniciadora de pagamento (PISP).
- **core-banking** -- a detentora da conta, onde o cliente autentica e o PIX é
  efetivado.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Popula o catálogo na primeira subida, para a vitrine não nascer vazia.
    seed_if_empty()
    # Avisa se o painel/cliente estiverem com segredos de desenvolvimento.
    warn_on_dev_defaults()
    warn_on_dev_secret()
    yield


# Título, versão e `servers` são os do contrato publicado: o portal do gateway
# tinha esses valores e o arquivo local nunca teve, então cada regeneração os
# perdia.
#
# `servers` tem UMA entrada, e é a do gateway. Acrescentar um servidor local
# (`http://localhost:8200`, para o "Try it out" do /docs) fez o portal recusar
# a gravação com erro genérico e corromper o base path da API -- foi preciso
# apagar e recriar a API na plataforma para sair disso. O portal deriva o base
# path de `servers`, então aqui só entra o endereço pelo qual a API é
# publicada.
app = FastAPI(
    title="[Demo] Sebo Online",
    version="1.0.0",
    description=DESCRICAO,
    servers=[
        {"url": "https://api-assets.sensedia.com/sebo/api", "description": "Produção"},
    ],
    lifespan=lifespan,
)

# A vitrine React (Vite) roda em outra origem no desenvolvimento.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(products.router)
app.include_router(customers.router)
app.include_router(devices.router)
app.include_router(cart.router)
app.include_router(orders.router)
app.include_router(admin.router)
app.include_router(openfinance.router)
app.include_router(integration.router)


@app.get("/health", tags=["infra"])
def health() -> dict:
    return {"status": "ok", "service": "sebo-online"}
