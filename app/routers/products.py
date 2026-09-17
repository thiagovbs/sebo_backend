"""Catálogo: cadastro dos produtos e a vitrine com busca, filtro e ordenação."""

import base64
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func
from sqlmodel import Session, select

from ..adminauth import require_admin
from ..db import get_session
from ..models import Product
from ..schemas import ProductIn, ProductList, ProductOut

router = APIRouter(prefix="/products", tags=["produtos"])

# As escritas de catálogo são ações administrativas; a leitura é pública.
_admin = Depends(require_admin)

# Limite do upload de imagem. Em base64 o data URI fica ~33% maior e é gravado
# no próprio campo image_url (o front renderiza URL e data URI do mesmo jeito).
MAX_IMAGE_BYTES = 3 * 1024 * 1024

# As chaves são o contrato da vitrine; o valor é a coluna e a direção reais.
SORT_OPTIONS = {
    "recentes": (Product.created_at, "desc"),
    "nome": (Product.name, "asc"),
    "menor_preco": (Product.price, "asc"),
    "maior_preco": (Product.price, "desc"),
}
SortKey = Literal["recentes", "nome", "menor_preco", "maior_preco"]


@router.get("", response_model=ProductList)
def list_products(
    session: Session = Depends(get_session),
    q: Optional[str] = Query(None, description="Busca por nome (parcial)"),
    category: Optional[str] = Query(None, description="Filtra por categoria"),
    sort: SortKey = Query("recentes", description="Ordenação da vitrine"),
    include_inactive: bool = Query(False, description="Inclui produtos inativos"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
) -> ProductList:
    """Vitrine: aplica busca, filtro de categoria e ordenação, com paginação."""
    filters = []
    if not include_inactive:
        filters.append(Product.active == True)  # noqa: E712
    if q:
        filters.append(Product.name.ilike(f"%{q}%"))
    if category:
        filters.append(Product.category == category)

    total = session.exec(
        select(func.count()).select_from(Product).where(*filters)
    ).one()

    column, direction = SORT_OPTIONS[sort]
    order = column.desc() if direction == "desc" else column.asc()
    items = session.exec(
        select(Product)
        .where(*filters)
        .order_by(order)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return ProductList(
        items=[ProductOut.model_validate(p, from_attributes=True) for p in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/categories", response_model=list[str])
def list_categories(session: Session = Depends(get_session)) -> list[str]:
    """Categorias distintas em uso (para montar o filtro da vitrine)."""
    rows = session.exec(
        select(Product.category)
        .where(Product.category != "", Product.active == True)  # noqa: E712
        .distinct()
        .order_by(Product.category)
    ).all()
    return list(rows)


@router.post("/upload-image", dependencies=[_admin])
async def upload_image(file: UploadFile = File(...)) -> dict:
    """Recebe uma imagem, converte para base64 (data URI) e a devolve.

    O data URI é o que vai em ``image_url`` ao salvar o produto — assim a
    imagem enviada do computador fica **arquivada no banco** como base64, sem
    depender de link externo. Retornar o data URI (em vez de já gravar) deixa o
    mesmo fluxo servir tanto o cadastro (produto ainda sem id) quanto a edição.
    """
    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Envie um arquivo de imagem")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Arquivo vazio")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Imagem acima do limite de {MAX_IMAGE_BYTES // (1024 * 1024)} MB",
        )
    b64 = base64.b64encode(data).decode("ascii")
    return {
        "image_url": f"data:{content_type};base64,{b64}",
        "bytes": len(data),
        "content_type": content_type,
    }


@router.get("/{product_id}", response_model=ProductOut)
def get_product(
    product_id: int, session: Session = Depends(get_session)
) -> ProductOut:
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    return ProductOut.model_validate(product, from_attributes=True)


@router.post("", response_model=ProductOut, status_code=201, dependencies=[_admin])
def create_product(
    data: ProductIn, session: Session = Depends(get_session)
) -> ProductOut:
    """Cadastra um produto — inclusive categoria e demais opções da vitrine."""
    product = Product(**data.model_dump())
    session.add(product)
    session.commit()
    session.refresh(product)
    return ProductOut.model_validate(product, from_attributes=True)


@router.put("/{product_id}", response_model=ProductOut, dependencies=[_admin])
def update_product(
    product_id: int, data: ProductIn, session: Session = Depends(get_session)
) -> ProductOut:
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    for field, value in data.model_dump().items():
        setattr(product, field, value)
    session.add(product)
    session.commit()
    session.refresh(product)
    return ProductOut.model_validate(product, from_attributes=True)


@router.delete("/{product_id}", status_code=204, dependencies=[_admin])
def delete_product(product_id: int, session: Session = Depends(get_session)) -> None:
    """Baixa lógica: marca inativo em vez de apagar (preserva o histórico)."""
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    product.active = False
    session.add(product)
    session.commit()
