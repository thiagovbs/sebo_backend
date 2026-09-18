# Sebo On-Line — Backend

API do **Sebo On-Line**, a loja que fecha a demo Open Finance junto ao
`payment-initiator` (iniciadora PISP) e ao core-banking (detentora). Anuncia
produtos de **qualquer categoria**, novos ou usados, e o checkout paga via
**PIX Open Finance**.

**Stack:** FastAPI · SQLModel · SQLite · [uv](https://docs.astral.sh/uv/).
O frontend fica em outro repositório ([`sebo_frontend`](https://github.com/thiagovbs/sebo_frontend)).

```
┌────────────┐  REST   ┌───────────────────┐   PISP   ┌──────────────┐        ┌──────────────┐
│  Frontend  │────────▶│      Backend      │─────────▶│ payment-     │───────▶│ core-banking │
│ React/Vite │         │   (sebo_online)   │          │ initiator    │        │ (detentora)  │
└────────────┘         │  FastAPI + SQLite │          │ (iniciadora) │        └──────────────┘
                       └───────────────────┘          └──────────────┘

Três jornadas de PIX no checkout
────────────────────────────────
1) QR clássico       Backend gera o BR Code da chave do Sebo → cliente paga no
   (copia e cola)    app do banco. Não passa pela iniciadora.

2) OF com redirect   Backend → iniciadora cria o consentimento único → cliente
   (consent. único)  aprova na detentora e volta ao checkout → pedido reconciliado.

3) OF JSR            Cliente autoriza o Sebo uma vez (enrollment na detentora);
   (sem redirect)    depois Backend → iniciadora paga direto → pedido nasce pago.
```

## O que tem

- **Catálogo** — busca por nome, filtro por categoria e ordenação (recentes,
  nome, menor/maior preço). Imagem por link ou upload (o backend converte para
  base64 e arquiva no banco).
- **Clientes** — autocadastro com login (e-mail + senha), CPF, telefone, data de
  nascimento, **vários endereços** e **vários cartões** (só dados não sensíveis).
  Cada cliente só acessa os próprios dados (token).
- **Carrinho e pedidos** — um carrinho por cliente, com controle de estoque, e
  histórico de pedidos.
- **Três jornadas de PIX no checkout:**
  - **PIX QR clássico (copia e cola)** — a loja gera o BR Code da própria chave
    PIX; o cliente paga no app do banco. Não usa a iniciadora.
  - **PIX Open Finance com redirect (consentimento único)** — sobre um pedido
    PIX QR em aberto, o cliente pode autorizar direto no seu banco: a loja cria
    um consentimento único na iniciadora (`POST /payments`, amarrado ao CPF do
    cliente) e o leva à detentora para aprovar aquele pagamento; na volta, o
    pedido é reconciliado (`GET /payments/{consent_id}`) e marcado como pago.
  - **PIX Open Finance JSR (sem redirect)** — o cliente autoriza o Sebo uma vez
    (autenticando no banco) e paga **sem redirect**; o pedido nasce já pago.
- **Painel de admin** (`/admin`) — protegido por senha: dashboard de vendas,
  cadastro/edição de produtos, pedidos, clientes e a seção **Integração** (dados
  da iniciadora e do recebedor do PIX, editáveis em tempo de execução).

## Como rodar

### Local (uv)

```bash
cp .env.example .env      # ajuste os segredos (veja abaixo)
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8200
```

Sobe em `http://localhost:8200` (Swagger em `/docs`). Na primeira subida o
catálogo é semeado com ~18 produtos de várias categorias.

Dados de exemplo para o dashboard (opcional, idempotente):

```bash
uv run python -m scripts.seed_demo
```

### Docker Compose

```bash
docker compose up -d --build
```

O `.env` **não** entra na imagem: é lido em runtime do host (o compose injeta as
variáveis e monta o próprio arquivo em `/app/.env`). O SQLite fica no volume
`sebo-data` (`/app/data`), sobrevivendo a rebuilds.

## Configuração (`.env`)

| Variável | O que é |
|---|---|
| `DATABASE_URL` | Banco (padrão `sqlite:///./data/sebo.db`) |
| `PORT` | Porta do servidor (padrão `8200`) |
| `FRONTEND_ORIGIN` | Origem do frontend liberada no CORS (ex.: a URL no Render) |
| `ADMIN_PASSWORD` | Senha do painel `/admin` (troque em produção) |
| `ADMIN_SECRET` | Segredo que assina o token do admin |
| `ADMIN_TOKEN_HOURS` | Validade do token do admin (padrão 8) |
| `CUSTOMER_SECRET` | Segredo que assina o token de sessão do cliente |

Gere segredos próprios: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

> Os dados de integração com a iniciadora (URL/usuário/senha) e do **recebedor
> do PIX** (nome, CPF/CNPJ e chave) **não** ficam no `.env` — são configurados no
> admin (aba **Integração**), em tempo de execução, e ficam no banco. Enquanto
> não configurados, o pagamento por Open Finance fica indisponível para o
> cliente. A chave PIX + o documento do recebedor precisam existir/casar no
> core-banking, senão o pagamento JSR é recusado (`CREDITOR_MISMATCH`).

## Testes

```bash
uv run pytest
```

## Papéis na demo

- **Sebo On-Line** — a loja (este projeto). É o *lojista/recebedor*.
- **payment-initiator** — a iniciadora de pagamento (PISP); a loja é um usuário
  dela (`sebo_online`).
- **core-banking** — a detentora da conta, onde o *cliente* autentica e o PIX é
  efetivado.
