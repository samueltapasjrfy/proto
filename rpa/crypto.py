"""Cifra simétrica para senhas e TOTP no clientes.json."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    pass


class Cipher:
    def __init__(self, master_key: str):
        try:
            self._fernet = Fernet(master_key.encode() if isinstance(master_key, str) else master_key)
        except Exception as e:
            raise CryptoError(f"RPA_MASTER_KEY inválida: {e}") from e

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None or plaintext == "":
            return None
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str | None) -> str | None:
        if ciphertext is None or ciphertext == "":
            return None
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as e:
            raise CryptoError("Falha ao decifrar — RPA_MASTER_KEY incorreta ou dado corrompido.") from e
