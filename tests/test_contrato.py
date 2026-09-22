"""O contrato que o portal do gateway importa.

O arquivo `openapi.yaml` fica fora do versionamento (é publicado à mão), então
o que se protege aqui é a RECEITA: o gerador. E o que quebra em silêncio não é
o schema das rotas -- é o que não é OpenAPI.

O import do portal é este mesmo documento, e é nele que moram o **destino** (o
endereço do backend que o gateway chama) e a **cadeia de interceptors**. Um
contrato sem eles não é "só documentação incompleta": republicar apaga os dois
da API, e o gateway fica sem para onde encaminhar. Ninguém percebe olhando o
arquivo, porque as rotas continuam todas lá.

O outro item é o `servers`: mais de uma entrada corrompe o base path no portal,
e o conserto custou excluir e recriar a API.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from gen_openapi import (  # noqa: E402
    CICLO_DE_VIDA, DESTINO_BACKEND, INTERCEPTORS, METODOS, gerar,
)

CONTRATO = gerar()


def test_um_unico_server_com_o_base_path_do_gateway():
    """Duas entradas em `servers` quebram o base path -- já custou recriar a API."""
    servers = CONTRATO["servers"]
    assert len(servers) == 1
    assert servers[0]["url"] == "https://api-assets.sensedia.com/sebo/api"


def test_o_contrato_leva_o_destino_e_os_interceptors():
    """Sem isto, publicar apaga o destino e a cadeia no portal."""
    assert CONTRATO["x-destination"] == DESTINO_BACKEND
    assert CONTRATO["x-destination"].startswith("http")
    assert CONTRATO["x-interceptors"] == INTERCEPTORS
    assert CONTRATO["x-life-cycle"] == CICLO_DE_VIDA
    assert CONTRATO["x-api-broken"] is False


def test_o_conteudo_do_interceptor_vai_como_texto_json():
    """O portal lê `x-interceptor-content` como string; objeto ali não é aceito."""
    for interceptor in CONTRATO["x-interceptors"].values():
        assert isinstance(interceptor["x-interceptor-content"], str)
        assert interceptor["x-interceptor-execution-point"] in ("FIRST", "SECOND")


def test_todo_caminho_tem_recurso_e_toda_operacao_aponta_para_ele():
    """`x-resource-name` é o que agrupa os métodos sob o mesmo recurso."""
    assert set(CONTRATO["x-resources"]) == set(CONTRATO["paths"])
    for caminho, operacoes in CONTRATO["paths"].items():
        for metodo, operacao in operacoes.items():
            if metodo not in METODOS:
                continue
            assert operacao["x-resource-name"] == caminho
            assert operacao["x-api-broken"] is False
            # Vazio, e não ausente: é como o export do portal escreve.
            assert operacao["x-interceptors"] == {}


def test_a_rota_nova_entra_no_contrato_sozinha():
    """O arquivo é derivado da aplicação: rota nova não depende de lembrar."""
    assert "/orders/{order_id}/boleto" in CONTRATO["paths"]
    assert "/orders/{order_id}/boleto" in CONTRATO["x-resources"]


def test_nenhum_placeholder_com_angulos():
    """O gateway barra `<` e `>`: o placeholder do caminho é `{chave}`."""
    for caminho in CONTRATO["paths"]:
        assert "<" not in caminho and ">" not in caminho
