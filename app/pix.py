"""Gera o BR Code do PIX (copia e cola / QR Code estático).

Formato EMV® MPM (padrão do Banco Central para PIX): campos TLV (id + tamanho +
valor) e, no fim, o CRC16-CCITT-FALSE. É o "PIX normal": a loja gera o código a
partir da própria chave + valor, e o cliente paga no app do banco. Não passa
pela iniciadora.
"""

import unicodedata


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def crc16(payload: str) -> str:
    """CRC16-CCITT-FALSE (poly 0x1021, init 0xFFFF), em hex maiúsculo 4 dígitos."""
    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def _ascii(text: str) -> str:
    """Remove acentos e caracteres fora do conjunto seguro do BR Code."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in ascii_text if c.isalnum() or c in " .-").strip()


def _txid(value: str) -> str:
    """txid: alfanumérico, até 25 chars; '***' quando vazio."""
    clean = "".join(c for c in value if c.isalnum())[:25]
    return clean or "***"


def build_br_code(
    key: str, merchant_name: str, merchant_city: str, amount: float, txid: str = "***"
) -> str:
    """Monta o payload PIX (copia e cola) para a chave e o valor informados."""
    name = (_ascii(merchant_name) or "RECEBEDOR")[:25]
    city = (_ascii(merchant_city) or "CIDADE")[:15]

    merchant_account = _tlv("26", _tlv("00", "br.gov.bcb.pix") + _tlv("01", key))
    additional = _tlv("62", _tlv("05", _txid(txid)))

    payload = (
        _tlv("00", "01")           # payload format indicator
        + _tlv("01", "11")         # estático (chave + valor)
        + merchant_account
        + _tlv("52", "0000")       # merchant category code
        + _tlv("53", "986")        # moeda: BRL
        + _tlv("54", f"{amount:.2f}")
        + _tlv("58", "BR")         # país
        + _tlv("59", name)         # nome do recebedor
        + _tlv("60", city)         # cidade
        + additional
        + "6304"                    # tag do CRC + tamanho fixo
    )
    return payload + crc16(payload)
