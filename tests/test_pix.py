"""Geração do BR Code do PIX (copia e cola)."""

from app.pix import build_br_code, crc16


def test_crc16_matches_the_standard_check_value():
    # CRC16-CCITT-FALSE de "123456789" é 0x29B1 (vetor de referência).
    assert crc16("123456789") == "29B1"


def test_br_code_structure_and_checksum():
    code = build_br_code(
        key="12345678000199", merchant_name="Sebo On-Line",
        merchant_city="São Paulo", amount=45.0, txid="SEBO1",
    )
    assert code.startswith("000201")            # payload format indicator
    assert "br.gov.bcb.pix" in code
    assert "12345678000199" in code             # a chave PIX
    assert "5405" + "45.00" in code             # valor (tag 54, len 05)
    assert "5802BR" in code                      # país
    # o CRC final confere o restante do payload
    assert code[-4:] == crc16(code[:-4])


def test_br_code_amount_is_formatted_with_two_decimals():
    code = build_br_code(key="k", merchant_name="X", merchant_city="Y", amount=7.5)
    assert "54047.50" in code  # tag 54, len 04, "7.50"


def test_merchant_name_is_ascii_upper_safe():
    code = build_br_code(key="k", merchant_name="Sebo Ação Ção", merchant_city="X", amount=1.0)
    # sem acentos no payload
    assert "Ação" not in code
