"""Cadastra/atualiza um cliente no clientes.json (senha e TOTP cifrados em repouso).

Uso interativo:
    python scripts/cadastrar_cliente.py

Uso não-interativo (útil para CI ou bootstrap em lote):
    python scripts/cadastrar_cliente.py \\
        --id 1 --nome "Fulano" --tribunal eproc_mg \\
        --usuario 12345678900 --senha "minhasenha" --totp "JBSWY3DPEHPK3PXP"
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# permite rodar `python scripts/cadastrar_cliente.py` direto da raiz do projeto
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpa.config import Settings  # noqa: E402
from rpa.crypto import Cipher  # noqa: E402
from rpa.storage import ClienteStore  # noqa: E402

TRIBUNAIS_SUPORTADOS = ("eproc_mg",)


def _prompt(label: str, default: str | None = None, *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        if secret:
            val = getpass.getpass(f"{label}{suffix}: ")
        else:
            val = input(f"{label}{suffix}: ").strip()
        if not val and default is not None:
            return default
        if val:
            return val
        print("  (valor obrigatório)")


def main() -> int:
    p = argparse.ArgumentParser(description="Cadastra/atualiza cliente no clientes.json (UPSERT por id).")
    p.add_argument("--id")
    p.add_argument("--nome")
    p.add_argument("--tribunal", choices=TRIBUNAIS_SUPORTADOS)
    p.add_argument("--usuario")
    p.add_argument("--senha")
    p.add_argument("--totp", help="TOTP secret (deixe vazio se a conta não tem 2FA)")
    args = p.parse_args()

    settings = Settings.load()
    store = ClienteStore(settings.clientes_file, Cipher(settings.master_key))

    cliente_id = args.id or _prompt("id do cliente")
    nome = args.nome or _prompt("nome", default=f"cliente {cliente_id}")
    tribunal = args.tribunal or _prompt("tribunal", default="eproc_mg")
    if tribunal not in TRIBUNAIS_SUPORTADOS:
        print(f"[ERRO] tribunal '{tribunal}' não suportado. Disponíveis: {TRIBUNAIS_SUPORTADOS}")
        return 1
    usuario = args.usuario or _prompt("usuario (CPF/login)")
    senha = args.senha if args.senha is not None else _prompt("senha", secret=True)
    totp = args.totp
    if totp is None:
        totp_input = input("totp secret (enter para pular): ").strip()
        totp = totp_input or None

    cliente = store.upsert(
        id=cliente_id,
        nome=nome,
        tribunal=tribunal,
        usuario=usuario,
        senha=senha,
        totp_secret=totp,
    )
    print(f"OK — cliente id={cliente.id} ({cliente.nome}) salvo em {settings.clientes_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
