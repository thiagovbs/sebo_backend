"""Cliente do payment-initiator (PISP Open Finance).

O sebo é o *lojista*: autentica na iniciadora, dispara o pagamento PIX com o
próprio sebo como recebedor e depois consulta o status. As credenciais e os
dados do recebedor **vêm da configuração de integração** (editável no admin em
tempo de execução), não do ``.env``. Cada configuração tem seu próprio cliente,
com token JWT reaproveitado entre requisições e renovado ao expirar/401.
"""

import threading

import httpx


class PaymentInitiatorError(RuntimeError):
    """A iniciadora respondeu erro ou está inacessível."""

    def __init__(self, message: str, *, status: int | None = None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class PaymentInitiatorClient:
    """Fala com o payment-initiator usando uma configuração de integração."""

    def __init__(self, cfg) -> None:
        self.base_url = (cfg.payment_initiator_url or "").rstrip("/")
        self.user = cfg.payment_initiator_user
        self.password = cfg.payment_initiator_password
        self.sebo_name = cfg.sebo_name
        self.sebo_cpf_cnpj = cfg.sebo_cpf_cnpj
        self.sebo_pix_key_type = cfg.sebo_pix_key_type
        self.sebo_pix_key_value = cfg.sebo_pix_key_value
        self._token: str | None = None
        self._lock = threading.Lock()

    # -- Autenticação ------------------------------------------------------

    def _login(self) -> str:
        url = f"{self.base_url}/auth/login"
        body = {"username": self.user, "password": self.password}
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, json=body)
        except httpx.RequestError as exc:
            raise PaymentInitiatorError(
                f"Iniciadora inacessível: {exc.__class__.__name__}"
            ) from exc
        if resp.status_code != 200:
            raise PaymentInitiatorError(
                "Falha ao autenticar o lojista na iniciadora",
                status=resp.status_code,
                detail=_safe_json(resp),
            )
        token = resp.json().get("access_token", "")
        if not token:
            raise PaymentInitiatorError("Iniciadora não devolveu access_token")
        return token

    def _get_token(self, *, force: bool = False) -> str:
        with self._lock:
            if force or not self._token:
                self._token = self._login()
            return self._token

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, *, json: dict | None = None) -> httpx.Response:
        """Chama a iniciadora; em 401 refaz o login uma vez e repete."""
        url = f"{self.base_url}{path}"
        for attempt in (1, 2):
            token = self._get_token(force=attempt == 2)
            try:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.request(
                        method, url, json=json, headers=self._headers(token)
                    )
            except httpx.RequestError as exc:
                raise PaymentInitiatorError(
                    f"Iniciadora inacessível: {exc.__class__.__name__}"
                ) from exc
            if resp.status_code == 401 and attempt == 1:
                continue  # token expirou/rejeitado: relogar e tentar de novo
            return resp
        return resp  # pragma: no cover

    # -- Operações ---------------------------------------------------------

    def create_payment(
        self, amount: str, description: str, debtor: dict | None = None
    ) -> dict:
        """Inicia o pagamento PIX com redirect. Devolve request_id + login_url.

        ``debtor`` leva os dados do pagador (nome, CPF, chave PIX) montados a
        partir do cadastro do cliente. A iniciadora ignora campos que ainda não
        consome, então mandá-los já deixa a integração pronta para quando ela
        passar a identificar o pagador.
        """
        body = {
            "amount": amount,
            "currency": "BRL",
            "creditor_name": self.sebo_name,
            "creditor_cpf_cnpj": self.sebo_cpf_cnpj,
            "creditor_key": {
                "type": self.sebo_pix_key_type,
                "value": self.sebo_pix_key_value,
            },
        }
        if debtor:
            body["debtor"] = debtor
        resp = self._request("POST", "/payments", json=body)
        if resp.status_code not in (200, 201):
            raise PaymentInitiatorError(
                "Iniciadora recusou a criação do pagamento",
                status=resp.status_code,
                detail=_safe_json(resp),
            )
        data = resp.json()
        return {
            "request_id": data.get("request_id", ""),
            "login_url": data.get("login_url", ""),
        }

    # -- Jornada JSR (dispositivo do titular) ------------------------------

    def start_enrollment(
        self, username: str, account_number: str = "", redirect_uri: str = ""
    ) -> dict:
        """Inicia a autorização do pagador. Devolve enrollment_id + login_url.

        ``username`` é o titular (o cliente que vai pagar), que autoriza o Sebo —
        o site age como o dispositivo a ser autorizado. O cliente abre o
        ``login_url`` e autentica no seu banco; a iniciadora conclui o cadastro e
        o marca como REGISTERED.

        ``redirect_uri`` (opcional) é para onde a iniciadora deve devolver o
        navegador ao concluir — assim o cliente volta direto ao Sebo em vez de
        parar numa página da iniciadora. Precisa estar na allow-list dela.
        """
        body = {"username": username, "account_number": account_number}
        if redirect_uri:
            body["redirect_uri"] = redirect_uri
        resp = self._request("POST", "/enrollments", json=body)
        if resp.status_code not in (200, 201):
            raise PaymentInitiatorError(
                "Iniciadora recusou o cadastro do dispositivo",
                status=resp.status_code,
                detail=_safe_json(resp),
            )
        data = resp.json()
        return {
            "enrollment_id": data.get("enrollment_id", ""),
            "login_url": data.get("login_url", ""),
        }

    def list_devices(self) -> list[dict]:
        """Lista os dispositivos do lojista na iniciadora (para sincronizar status)."""
        resp = self._request("GET", "/enrollments")
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []

    def pay_jsr(self, amount: str, enrollment_id: str) -> dict:
        """Paga sem redirect com o dispositivo vinculado (jornada JSR).

        Sucesso devolve ``{payment_id, consent_id, status}`` (a iniciadora
        conclui na hora). Se o dispositivo não estiver REGISTERED, devolve
        ``{need_enrollment: True, login_url}`` para o cliente concluir o vínculo.
        """
        body = {
            "amount": amount,
            "currency": "BRL",
            "creditor_name": self.sebo_name,
            "creditor_cpf_cnpj": self.sebo_cpf_cnpj,
            "creditor_key": {
                "type": self.sebo_pix_key_type,
                "value": self.sebo_pix_key_value,
            },
            "enrollment_id": enrollment_id,
        }
        resp = self._request("POST", "/payments/jsr", json=body)
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "payment_id": data.get("payment_id", ""),
                "consent_id": data.get("consent_id", ""),
                "status": data.get("status", "COMPLETED"),
            }
        if resp.status_code in (404, 409):
            data = _safe_json(resp)
            return {
                "need_enrollment": True,
                "login_url": data.get("login_url", ""),
                "message": data.get("message", "Dispositivo não vinculado"),
            }
        raise PaymentInitiatorError(
            "Iniciadora recusou o pagamento JSR",
            status=resp.status_code,
            detail=_safe_json(resp),
        )

    def get_status(self, identifier: str) -> dict:
        """Consulta um pagamento por request_id, consent_id ou payment_id."""
        resp = self._request("GET", f"/payments/{identifier}")
        if resp.status_code == 404:
            return {"status": "NOT_FOUND"}
        if resp.status_code != 200:
            raise PaymentInitiatorError(
                "Iniciadora falhou ao consultar o pagamento",
                status=resp.status_code,
                detail=_safe_json(resp),
            )
        return resp.json()


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:300]}


# Cache de clientes por configuração: cada combinação de credenciais/recebedor
# tem seu próprio cliente (com token). Ao editar a config no admin, a assinatura
# muda e um novo cliente é criado — sem reaproveitar token de credencial antiga.
_clients: dict[tuple, PaymentInitiatorClient] = {}
_clients_lock = threading.Lock()


def _signature(cfg) -> tuple:
    return (
        cfg.payment_initiator_url,
        cfg.payment_initiator_user,
        cfg.payment_initiator_password,
        cfg.sebo_name,
        cfg.sebo_cpf_cnpj,
        cfg.sebo_pix_key_type,
        cfg.sebo_pix_key_value,
    )


def get_client(cfg) -> PaymentInitiatorClient:
    """Devolve (criando se preciso) o cliente da iniciadora para esta config."""
    key = _signature(cfg)
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = PaymentInitiatorClient(cfg)
            _clients[key] = client
        return client
