"""API do Sebo On-Line: catálogo, clientes, carrinho e checkout via PIX."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .adminauth import warn_on_dev_defaults
from .custauth import warn_on_dev_secret
from .config import settings
from .db import init_db
from .routers import admin, cart, customers, devices, openfinance, orders, products
from .seed import seed_if_empty


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Popula o catálogo na primeira subida, para a vitrine não nascer vazia.
    seed_if_empty()
    # Avisa se o painel/cliente estiverem com segredos de desenvolvimento.
    warn_on_dev_defaults()
    warn_on_dev_secret()
    yield


app = FastAPI(title="Sebo On-Line", version="0.1.0", lifespan=lifespan)

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


@app.get("/health", tags=["infra"])
def health() -> dict:
    return {"status": "ok", "service": "sebo-online"}
