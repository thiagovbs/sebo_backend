"""Gera o ``openapi.yaml`` a partir da aplicação, em OpenAPI 3.0.3.

    uv run python scripts/gen_openapi.py          # grava openapi.yaml
    uv run python scripts/gen_openapi.py --check  # só compara, não grava

Por que este script existe: o contrato é publicado à mão no portal do gateway,
e por isso o arquivo fica fora do versionamento. Sem uma receita, ele foi
editado à mão e **defasou** — chegou a ter oito caminhos e um schema a menos que
a aplicação, e publicar aquela versão apagaria rotas do contrato no portal.
Gerar é a correção de raiz: o arquivo passa a ser derivado, e a documentação da
API mora nos docstrings e nos ``Field(description=...)``, onde sobrevive.

Por que 3.0.3 e não o 3.1 que o FastAPI emite: o gateway da Sensedia consome
3.0.x. A conversão é pequena e está toda em ``para_30``, com cada regra
justificada — o que não aparecer na saída da aplicação hoje continua tratado,
porque um campo novo amanhã não pode virar contrato inválido em silêncio.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Permite rodar como ``python scripts/gen_openapi.py`` da raiz do backend.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

VERSAO_ALVO = "3.0.3"
DESTINO = Path(__file__).resolve().parent.parent / "openapi.yaml"


def para_30(no):
    """Converte recursivamente construções de JSON Schema 2020-12 para 3.0.

    Cada regra existe por um motivo:

    - ``anyOf`` com ``{"type": "null"}``: é como o 3.1 diz "opcional". No 3.0 o
      equivalente é ``nullable: true`` dentro do ramo, que é a forma que o
      arquivo já usava.
    - ``type: [X, "null"]``: a mesma ideia escrita como lista de tipos, que o
      3.0 não aceita.
    - ``contentMediaType``: como o 3.1 marca binário. No 3.0 é
      ``format: binary`` -- é o campo de upload de imagem do produto.
    - ``const``: virou ``enum`` de um item, que o 3.0 entende.
    - ``examples`` (lista): no 3.0 é ``example``, singular.
    - ``exclusiveMinimum``/``exclusiveMaximum`` numéricos: no 3.0 são booleanos
      acompanhando ``minimum``/``maximum``.
    """
    if isinstance(no, list):
        return [para_30(item) for item in no]
    if not isinstance(no, dict):
        return no

    no = {chave: para_30(valor) for chave, valor in no.items()}

    ramos = no.get("anyOf")
    if isinstance(ramos, list):
        sem_nulo = [r for r in ramos if not (isinstance(r, dict) and r.get("type") == "null")]
        if len(sem_nulo) != len(ramos):
            # Preserva a forma do arquivo anterior: o ramo nulo continua sendo um
            # item do anyOf, escrito como `nullable: true`.
            no["anyOf"] = sem_nulo + [{"nullable": True}]

    tipos = no.get("type")
    if isinstance(tipos, list):
        restantes = [t for t in tipos if t != "null"]
        no["type"] = restantes[0] if len(restantes) == 1 else restantes
        if len(restantes) != len(tipos):
            no["nullable"] = True

    if "contentMediaType" in no:
        no.pop("contentMediaType")
        if no.get("type") == "string":
            no["format"] = "binary"

    if "const" in no:
        no["enum"] = [no.pop("const")]

    exemplos = no.get("examples")
    if isinstance(exemplos, list) and exemplos:
        no.pop("examples")
        no["example"] = exemplos[0]

    for limite, par in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
        valor = no.get(limite)
        if isinstance(valor, (int, float)) and not isinstance(valor, bool):
            no[par] = valor
            no[limite] = True

    return no


def gerar() -> dict:
    spec = para_30(app.openapi())
    spec["openapi"] = VERSAO_ALVO
    return spec


class _Dumper(yaml.SafeDumper):
    """Escreve texto de várias linhas em bloco (``|``), não dobrado.

    A descrição da API e os docstrings das rotas são Markdown. No estilo
    padrão do PyYAML eles saem como uma string entre aspas cheia de ``\\n``, e
    quem abre o contrato para revisar não consegue lê-los.
    """


def _texto(dumper: yaml.SafeDumper, valor: str):
    if "\n" in valor:
        # Espaço no fim da linha é o que faria o YAML recusar o bloco.
        limpo = "\n".join(linha.rstrip() for linha in valor.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", limpo, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", valor)


_Dumper.add_representer(str, _texto)


def serializar(spec: dict) -> str:
    # `sort_keys=False` preserva a ordem que o FastAPI monta (openapi, info,
    # paths, components) e a ordem dos campos de cada schema, que é a ordem em
    # que eles foram declarados no código.
    return yaml.dump(spec, Dumper=_Dumper, sort_keys=False, allow_unicode=True)


def main() -> int:
    spec = gerar()
    texto = serializar(spec)
    caminhos = len(spec.get("paths", {}))
    schemas = len(spec.get("components", {}).get("schemas", {}))

    if "--check" in sys.argv:
        atual = DESTINO.read_text(encoding="utf-8") if DESTINO.exists() else ""
        if atual == texto:
            print(f"openapi.yaml em dia ({caminhos} caminhos, {schemas} schemas).")
            return 0
        print("openapi.yaml DIVERGE da aplicação. Rode sem --check para regerar.")
        return 1

    DESTINO.write_text(texto, encoding="utf-8", newline="\n")
    print(f"openapi.yaml gerado: {caminhos} caminhos, {schemas} schemas, OpenAPI {VERSAO_ALVO}.")
    print("Lembre de publicar no portal do gateway -- o arquivo é a fonte, o portal é a cópia.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
