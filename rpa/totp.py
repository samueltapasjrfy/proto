"""Geração de códigos TOTP.

Formato esperado por padrão: base32 (RFC 4648 — A-Z, 2-7, padding opcional).
Aceita também prefixos explícitos quando o segredo vier em outro formato:

    base32:JBSWY3DPEHPK3PXP   (= JBSWY3DPEHPK3PXP, prefixo opcional)
    hex:4a30b91c...           (bytes em hexadecimal — par de chars por byte)
    ascii:senha_literal       (bytes ASCII do próprio texto)
    otpauth://totp/...?secret=JBSWY3DPEHPK3PXP&...

Espaços e hífens são removidos. Sem prefixo + chars fora do alfabeto base32
gera erro explícito (não tentamos adivinhar — uma tentativa errada de TOTP
gasta cota e pode bloquear a conta).
"""
from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import parse_qs, urlparse

import pyotp


class InvalidTOTPSecret(ValueError):
    pass


_BASE32_RE = re.compile(r"^[A-Z2-7]+=*$")
_HEX_RE = re.compile(r"^[0-9A-F]+$")


def _strip(secret: str) -> str:
    return re.sub(r"[\s\-]", "", str(secret))


def _from_otpauth(uri: str) -> str:
    parsed = urlparse(uri)
    qs = parse_qs(parsed.query)
    secret = qs.get("secret", [""])[0]
    if not secret:
        raise InvalidTOTPSecret("URI otpauth:// sem parâmetro 'secret'.")
    return _resolve(secret)


def _resolve(secret: str) -> str:
    raw = str(secret).strip()
    if raw.lower().startswith("otpauth://"):
        return _from_otpauth(raw)

    if ":" in raw and not raw.startswith("otpauth"):
        prefix, _, value = raw.partition(":")
        prefix = prefix.strip().lower()
        value = _strip(value).upper() if prefix in {"base32", "hex"} else value
    else:
        prefix = "base32"
        value = _strip(raw).upper()

    if prefix == "base32":
        if not _BASE32_RE.fullmatch(value):
            raise InvalidTOTPSecret(
                "Secret não é base32 válido (alfabeto: A-Z, 2-7). "
                "Se for hex, use prefixo 'hex:'. Se for ASCII literal, use 'ascii:'. "
                "Se você tem um QR code, cole o otpauth:// inteiro."
            )
        try:
            base64.b32decode(value, casefold=False)
        except binascii.Error as e:
            raise InvalidTOTPSecret(f"base32 inválido: {e}") from e
        return value

    if prefix == "hex":
        if not _HEX_RE.fullmatch(value) or len(value) % 2 != 0:
            raise InvalidTOTPSecret("Secret com prefixo hex: deve ter chars 0-9/a-f em pares.")
        return base64.b32encode(bytes.fromhex(value)).decode().rstrip("=")

    if prefix == "ascii":
        if not value:
            raise InvalidTOTPSecret("Secret com prefixo ascii: está vazio.")
        return base64.b32encode(value.encode()).decode().rstrip("=")

    raise InvalidTOTPSecret(f"Prefixo de TOTP secret desconhecido: '{prefix}:'.")


def gerar_codigo(secret: str) -> str:
    return pyotp.TOTP(_resolve(secret)).now()
