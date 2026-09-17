"""Semeadura do catálogo na primeira subida (para a vitrine não nascer vazia).

O sebo anuncia de tudo: o catálogo inicial cobre várias categorias — cada item
já vem com categoria, marca, condição e preço, que são os campos usados pela
vitrine para buscar, filtrar e ordenar.
"""

from sqlmodel import Session, select

from .db import engine
from .models import Product

# (nome, marca, categoria, condição, preço, estoque)
_CATALOGO = [
    ("Fone Bluetooth XZ-500", "SoundPro", "Eletrônicos", "novo", 199.90, 10),
    ("Smartwatch Fit 2", "PulseTech", "Eletrônicos", "novo", 349.00, 6),
    ("Caixa de Som Portátil", "SoundPro", "Eletrônicos", "seminovo", 159.00, 5),
    ("Teclado Mecânico RGB", "KeyForge", "Informática", "novo", 289.00, 8),
    ("Mouse Sem Fio Ergo", "KeyForge", "Informática", "seminovo", 89.90, 12),
    ('Monitor 27" 144Hz', "VisionX", "Informática", "usado - ótimo estado", 899.00, 3),
    ("Cafeteira Italiana 6 xíc.", "Bialetti", "Casa & Cozinha", "novo", 129.00, 15),
    ("Jogo de Panelas Antiaderente", "CasaBella", "Casa & Cozinha", "novo", 259.00, 7),
    ("Luminária de Mesa LED", "Lumen", "Casa & Cozinha", "seminovo", 79.00, 9),
    ("Controle Sem Fio", "PlayEdge", "Games", "seminovo", 199.00, 9),
    ("Console Portátil Retro", "PixelBox", "Games", "usado - bom estado", 420.00, 4),
    ("Tênis de Corrida Aero", "Runmax", "Esporte", "novo", 279.00, 10),
    ("Mochila de Trilha 40L", "TrekGear", "Esporte", "seminovo", 189.00, 6),
    ("Jaqueta Corta-Vento", "UrbanWear", "Moda", "novo", 219.00, 8),
    ("Óculos de Sol Polarizado", "SunView", "Moda", "novo", 149.00, 11),
    ("Violão Acústico", "Harmony", "Instrumentos", "usado - ótimo estado", 640.00, 3),
    ("Dom Casmurro", "Machado de Assis", "Livros", "usado - bom estado", 28.00, 5),
    ("Sapiens", "Yuval N. Harari", "Livros", "usado", 45.00, 4),
]


def seed_if_empty() -> int:
    """Insere o catálogo inicial se ainda não houver produtos. Devolve quantos."""
    with Session(engine) as session:
        if session.exec(select(Product).limit(1)).first():
            return 0
        for idx, (name, brand, category, condition, price, stock) in enumerate(
            _CATALOGO, start=1
        ):
            session.add(
                Product(
                    name=name,
                    brand=brand,
                    category=category,
                    condition=condition,
                    price=price,
                    stock=stock,
                    description=f"{name} — {brand}. Produto {condition}.",
                    image_url=f"https://picsum.photos/seed/sebo{idx}/500/500",
                )
            )
        session.commit()
        return len(_CATALOGO)
