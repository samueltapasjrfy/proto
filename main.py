"""CLI principal.

Modos:
    # single-client (credenciais no .env — EPROC_MG_USUARIO/SENHA/TOTP_SECRET)
    python main.py login
    python main.py consultar 10038972720268130145

    # multi-client (clientes.json cifrado com RPA_MASTER_KEY)
    python main.py login --cliente 1
    python main.py consultar 10038972720268130145 --cliente 1
    python main.py listar
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from rpa.config import Settings
from rpa.crypto import Cipher
from rpa.db import (
    DBConfigError,
    buscar_item_para_protocolo,
    buscar_processo_por_cod_item,
    listar_arquivos_do_item,
    listar_protocolos_aptos,
    marcar_como_erro,
    marcar_em_execucao,
)
from rpa.fs import sanitizar_nome
from rpa.jurify import JurifyError, upload_recibo_painel
from rpa.logger import get as get_logger, setup as setup_logger
from rpa.pdf import cnj_no_pdf, extrair_cnjs, identificar_peticao_principal
from rpa.pdf_optimize import PdfMuitoGrandeError, otimizar_para_eproc
from rpa.s3 import baixar as s3_baixar
from rpa.storage import ENV_CLIENTE_ID, ClienteStore, CookieStore, EnvClienteStore

# Registro de adapters por tribunal — adicione aqui ao implementar novos tribunais.
from rpa.tribunais.eproc_mg import EprocMGAdapter, EprocMGAsyncFlow
from rpa.tribunais.eproc_rj import EprocRJAdapter, EprocRJAsyncFlow
from rpa.tribunais.eproc_rs import EprocRSAdapter, EprocRSAsyncFlow
from rpa.tribunais.eproc_sp import EprocSPAdapter, EprocSPAsyncFlow

ADAPTERS = {
    "eproc_mg": EprocMGAdapter,
    "eproc_rs": EprocRSAdapter,
    "eproc_sp": EprocSPAdapter,
    "eproc_rj": EprocRJAdapter,
}

# Flows async correspondentes — usados quando --workers > 1.
ASYNC_FLOWS = {
    "eproc_mg": EprocMGAsyncFlow,
    "eproc_rs": EprocRSAsyncFlow,
    "eproc_sp": EprocSPAsyncFlow,
    "eproc_rj": EprocRJAsyncFlow,
}

# Mapeia o `eproc_base` (URL) que vem da query → tribunal_id usado em ADAPTERS.
EPROC_BASE_TO_TRIBUNAL = {
    "https://eproc1g.tjmg.jus.br/eproc/": "eproc_mg",
    "https://eproc1g.tjrs.jus.br/eproc/": "eproc_rs",
    "https://eproc1g.tjsp.jus.br/eproc/": "eproc_sp",
    "https://eproc1g.tjrj.jus.br/eproc/": "eproc_rj",
}

# Código do tribunal no CNJ (dígitos 14-16 dos 20 = posição [-7:-4]) → tribunal_id.
# Usado pra rotear uma consulta avulsa pro eproc CERTO a partir do número.
CNJ_TRIBUNAL = {"813": "eproc_mg", "821": "eproc_rs", "826": "eproc_sp", "819": "eproc_rj"}

# Fábricas de credenciais por tribunal (cada eproc tem usuário/senha próprios no .env).
ENV_STORE_FACTORIES = {
    "eproc_mg": EnvClienteStore.from_env_eproc_mg,
    "eproc_rs": EnvClienteStore.from_env_eproc_rs,
    "eproc_sp": EnvClienteStore.from_env_eproc_sp,
    "eproc_rj": EnvClienteStore.from_env_eproc_rj,
}


def _tribunal_por_cnj(numero: str) -> str | None:
    """Detecta o tribunal pelo código no CNJ (20 dígitos). None se não bater."""
    d = re.sub(r"\D", "", numero or "")
    return CNJ_TRIBUNAL.get(d[-7:-4]) if len(d) == 20 else None


def _default_desde(dias: int = 7) -> str:
    """Default da janela: últimos N dias (não mais que isso, pra não puxar lixo)."""
    return (date.today() - timedelta(days=dias)).isoformat()


def _hoje() -> str:
    """Teto da janela: data de hoje (não puxa lixo cadastrado com data futura)."""
    return date.today().isoformat()


def _bootstrap_base() -> tuple[Settings, CookieStore]:
    settings = Settings.load()
    setup_logger(settings.logs_dir)
    return settings, CookieStore(settings.cookies_dir)


def _json_store(settings: Settings) -> ClienteStore:
    if not settings.master_key:
        raise RuntimeError(
            "Para usar --cliente é preciso definir RPA_MASTER_KEY no .env "
            "(senhas em data/clientes.json ficam cifradas). "
            "Sem --cliente, o RPA lê EPROC_MG_USUARIO/SENHA/TOTP_SECRET do .env."
        )
    return ClienteStore(settings.clientes_file, Cipher(settings.master_key))


def _resolver_cliente(args: argparse.Namespace, settings: Settings, log):
    """Devolve (cliente_store, cliente_id) baseado em --cliente ou .env."""
    if getattr(args, "cliente", None) is None:
        return EnvClienteStore.from_env_eproc_mg(), ENV_CLIENTE_ID
    return _json_store(settings), args.cliente


def _abrir_adapter(args, settings, cookie_store, log):
    """Retorna (adapter_cls, cliente, cliente_store) prontos pra abrir contexto."""
    cliente_store, cliente_id = _resolver_cliente(args, settings, log)
    cliente = cliente_store.get(cliente_id)
    adapter_cls = ADAPTERS.get(cliente.tribunal)
    if not adapter_cls:
        raise RuntimeError(f"Tribunal '{cliente.tribunal}' não tem adapter registrado.")
    return adapter_cls, cliente, cliente_store


def cmd_login(args: argparse.Namespace) -> int:
    settings, cookie_store = _bootstrap_base()
    log = get_logger("cli")
    try:
        adapter_cls, cliente, cliente_store = _abrir_adapter(args, settings, cookie_store, log)
    except (RuntimeError, KeyError) as e:
        log.error(str(e))
        return 1

    with adapter_cls(
        cliente,
        settings=settings,
        cliente_store=cliente_store,
        cookie_store=cookie_store,
        headless=args.headless,
    ) as adapter:
        try:
            resultado = adapter.login()
        except Exception as e:
            log.exception("falha no login: %s", e)
            return 2

    log.info("login OK (estado=%s, cookies=%d)", resultado["estado"], len(resultado["cookies"]))
    return 0


def cmd_consultar(args: argparse.Namespace) -> int:
    settings, cookie_store = _bootstrap_base()
    log = get_logger("cli")
    try:
        # Sem --cliente, roteia pro eproc do tribunal do CNJ (não force MG sempre).
        trib = _tribunal_por_cnj(args.numero) if getattr(args, "cliente", None) is None else None
        if trib:
            adapter_cls = ADAPTERS[trib]
            cliente_store = ENV_STORE_FACTORIES[trib]()
            cliente = cliente_store.get(ENV_CLIENTE_ID)
            log.info("consulta roteada pelo CNJ → %s", trib)
        else:
            adapter_cls, cliente, cliente_store = _abrir_adapter(args, settings, cookie_store, log)
    except (RuntimeError, KeyError) as e:
        log.error(str(e))
        return 1

    with adapter_cls(
        cliente,
        settings=settings,
        cliente_store=cliente_store,
        cookie_store=cookie_store,
        headless=args.headless,
    ) as adapter:
        try:
            adapter.login()
            resultado = adapter.consultar_processo(args.numero)
        except Exception as e:
            log.exception("falha na consulta: %s", e)
            return 2

    if resultado.encontrado:
        print(f"\n  ENCONTRADO  processo={resultado.numero_formatado or resultado.numero}")
        print(f"              titulo={resultado.titulo}")
        print(f"              url={resultado.url}")
        return 0
    else:
        print(f"\n  NÃO ENCONTRADO  processo={resultado.numero}")
        if resultado.erro:
            print(f"                  motivo={resultado.erro}")
        return 3


def _sanitizar_e_renomear(arquivo: Path, log) -> Path:
    """Se o nome tem caractere fora de [A-Za-z0-9._-], renomeia o arquivo no
    disco pra uma versão segura. Idempotente.
    """
    nome = arquivo.name
    seguro = sanitizar_nome(nome, fallback="documento.pdf")
    if seguro == nome:
        return arquivo
    destino = arquivo.with_name(seguro)
    if destino.exists() and destino != arquivo:
        stem, ext, i = destino.stem, destino.suffix, 1
        while destino.exists():
            destino = destino.with_name(f"{stem}_{i}{ext}")
            i += 1
    arquivo.rename(destino)
    log.info("arquivo renomeado: %r -> %r", nome, destino.name)
    return destino


def _resolver_arquivos(
    numero: str,
    arquivo: str | None,
    principal_override: str | None,
    log,
) -> tuple[Path, list[Path]]:
    """Resolve quais PDFs compõem o protocolo. Retorna (principal, [anexos]).

    Regras:
    - Se `arquivo` for passado, modo single-doc (principal=arquivo, sem anexos).
    - Senão, lista PDFs em ./<numero>/ ignorando `recibo*.pdf` (resultado de runs anteriores).
    - Se há 1 PDF: principal=esse, sem anexos.
    - Se há vários: identifica o principal por conteúdo (ou usa `--principal` se passado).
    """
    if arquivo:
        return Path(arquivo).expanduser().resolve(), []

    pasta = Path.cwd() / numero
    if not pasta.is_dir():
        raise FileNotFoundError(
            f"Sem --arquivo e a pasta {pasta} não existe. Crie a pasta com o PDF "
            f"ou passe --arquivo <path>."
        )
    pdfs = [
        p for p in sorted(pasta.glob("*.pdf"))
        if not p.name.lower().startswith("recibo")
    ]
    if not pdfs:
        raise FileNotFoundError(f"Nenhum PDF (não-recibo) em {pasta}/")
    if len(pdfs) == 1:
        return pdfs[0], []

    if principal_override:
        principal = Path(principal_override).expanduser().resolve()
        if principal not in pdfs:
            raise FileNotFoundError(f"--principal {principal} não está em {pasta}/")
    else:
        principal, breakdown = identificar_peticao_principal(pdfs, numero)
        log.info("identificação da petição principal por conteúdo:")
        for p, sc in breakdown.items():
            marker = " <-- PRINCIPAL" if p == principal else ""
            log.info(
                "  %-50s total=%3d  end=%2d cnj=%2d oab=%2d def=%2d%s",
                p.name, sc["total"], sc["enderecamento"], sc["cnj"],
                sc["oab"], sc["deferimento"], marker,
            )

    anexos = [p for p in pdfs if p != principal]
    return principal, anexos


def cmd_peticionar(args: argparse.Namespace) -> int:
    settings, cookie_store = _bootstrap_base()
    log = get_logger("cli")
    try:
        adapter_cls, cliente, cliente_store = _abrir_adapter(args, settings, cookie_store, log)
    except (RuntimeError, KeyError) as e:
        log.error(str(e))
        return 1

    try:
        principal, anexos = _resolver_arquivos(args.numero, args.arquivo, args.principal, log)
    except (FileNotFoundError, RuntimeError) as e:
        log.error(str(e))
        return 1

    numero_digits = re.sub(r"\D", "", args.numero)

    # SEGURANÇA: o CNJ tem que aparecer no conteúdo da peça principal.
    # Anexos (cálculos, comprovantes, guias) podem ou não ter — não validamos.
    try:
        if not cnj_no_pdf(principal, numero_digits):
            cnjs_no_pdf = sorted(extrair_cnjs(principal))
            log.error(
                "ABORTADO — CNJ %s não encontrado na peça principal %s. CNJs achados: %s",
                numero_digits, principal.name, cnjs_no_pdf or "(nenhum)",
            )
            return 4
        log.info("peça principal confere com o processo (CNJ %s presente)", numero_digits)
    except Exception as e:
        log.error("falha lendo PDF principal %s: %s", principal, e)
        return 4

    # Sanitiza todos os nomes (principal + anexos) antes de subir.
    principal = _sanitizar_e_renomear(principal, log)
    anexos = [_sanitizar_e_renomear(a, log) for a in anexos]

    if anexos:
        log.info("multi-doc: 1 peça principal + %d anexo(s)", len(anexos))

    with adapter_cls(
        cliente,
        settings=settings,
        cliente_store=cliente_store,
        cookie_store=cookie_store,
        headless=args.headless,
    ) as adapter:
        try:
            adapter.login()
            cons = adapter.consultar_processo(args.numero)
            if not cons.encontrado:
                log.error("processo não encontrado (%s)", cons.erro)
                return 3
            mov = adapter.movimentar_peticionar(evento=args.evento)

            recibo = None
            conf = None
            uploads_pulados = False

            # Idempotência conservadora: se a peça principal já está na tabela,
            # pulamos TODOS os uploads (assumimos que run anterior já confirmou
            # o conjunto inteiro). Operador limpa manualmente se quiser refazer.
            if adapter._contar_doc_na_tabela_selecionados(principal.name) > 0:
                log.warning(
                    "peça principal já está na tabela 'Documentos selecionados' — "
                    "pulando uploads para não duplicar"
                )
                uploads_pulados = True
            else:
                # Doc 1 = peça principal (tipo Petição)
                adapter.anexar_documento(arquivo=principal, tipo=args.tipo, documento=1)
                # Docs 2..N = anexos (tipo Anexo)
                for i, anex in enumerate(anexos, start=2):
                    adapter.adicionar_mais_documentos(novo_n=i)
                    adapter.anexar_documento(arquivo=anex, tipo=args.tipo_anexo, documento=i)
                if args.confirmar or args.peticionar:
                    conf = adapter.confirmar_documentos(
                        aguardar_nomes=[principal.name] + [a.name for a in anexos],
                    )

            if args.peticionar:
                recibo = adapter.peticionar_e_capturar_recibo(
                    numero=numero_digits,
                    pasta_destino=principal.parent,
                    nome_arquivo_anexo=principal.name,
                )
            elif args.preview:
                # Sem --peticionar: salva screenshot do estado pra você verificar.
                preview = principal.parent / f"preview{numero_digits}.png"
                adapter.page.screenshot(path=str(preview), full_page=True)
                log.info("preview salvo em %s", preview)
        except Exception as e:
            log.exception("falha em peticionar: %s", e)
            return 2

    print(f"\n  PETIÇÃO  processo={cons.numero_formatado or cons.numero}")
    print(f"           evento={mov['evento']!r}")
    print(f"           prazos_desmarcados={mov.get('prazos_desmarcados', 0)}")
    print(f"           principal={principal.name}")
    if anexos:
        print(f"           anexos ({len(anexos)}):")
        for a in anexos:
            print(f"             - {a.name}")
    if uploads_pulados:
        print(f"           uploads pulados (docs já na tabela do run anterior)")
    if recibo:
        print(f"           PROTOCOLADA — recibo={recibo}")
    elif conf:
        print(f"           confirmado (sem --peticionar): {conf['title']!r}")
    else:
        print("           (sem --confirmar — apenas anexado)")
    return 0


def _pasta_item(cnj_digits: str, cod_item: int) -> Path:
    """Pasta de trabalho ISOLADA por CodItem (documentos + recibo).

    Cada CodItem é um protocolo distinto — nunca pode reaproveitar arquivos de
    outro CodItem, mesmo do MESMO processo (mesmo IdProc/CNJ). Por isso a pasta
    inclui o CodItem, não só o CNJ. Mantém o CNJ no nome pra leitura humana.
    """
    return Path.cwd() / f"{cnj_digits}_{cod_item}"


def _preparar_item(cod_item: int, args, log) -> dict | None:
    """Fase de preparação (sem browser): consulta DB, baixa S3, identifica principal.

    Retorna dict com chaves cod_item/cnj_digits/principal/anexos quando o item
    está pronto pro browser. Retorna None se algum gate falhar (e loga o motivo).
    """
    if getattr(args, "ignorar_filtro_migracao", False):
        item = buscar_item_para_protocolo(cod_item)
        if not item:
            log.error(
                "CodItem=%s: não passou nos checks essenciais (status, tipo, arquivos)",
                cod_item,
            )
            return None
    else:
        itens = listar_protocolos_aptos(
            cod_item=cod_item,
            dt_cadastro_minimo=args.desde,
            dt_cadastro_maximo=_hoje(),
        )
        if not itens:
            log.error("CodItem=%s: não está entre os aptos (fora dos filtros da query)", cod_item)
            return None
        item = itens[0]
    cnj_digits = re.sub(r"\D", "", str(item.get("NumProcessoCNJ") or ""))
    if not cnj_digits:
        log.error("CodItem=%s: sem NumProcessoCNJ", cod_item)
        return None
    eproc_base = item.get("eproc_base") or ""
    tribunal_id = EPROC_BASE_TO_TRIBUNAL.get(eproc_base)
    if not tribunal_id:
        log.error("CodItem=%s: eproc_base %r sem adapter registrado", cod_item, eproc_base)
        return None

    # Pasta de trabalho POR CodItem (não por CNJ): cada CodItem é um protocolo
    # distinto, com documentos próprios. Compartilhar a pasta por CNJ fazia o 2º
    # protocolo do mesmo processo reusar documentos E recibo do 1º (s3_baixar tem
    # pular_se_existir=True; o recibo é recibo<CNJ>.pdf). Isolar por CodItem
    # garante que isso nunca mais aconteça.
    pasta = _pasta_item(cnj_digits, cod_item)
    pasta.mkdir(parents=True, exist_ok=True)

    try:
        arquivos = listar_arquivos_do_item(cod_item)
    except Exception as e:
        log.exception("CodItem=%s: erro listando arquivos: %s", cod_item, e)
        return None
    if not arquivos:
        log.error("CodItem=%s: sem arquivos em tbarquivosprocesso", cod_item)
        return None

    paths: list[Path] = []
    for arq in arquivos:
        nome = sanitizar_nome(arq.get("NomeArquivo") or "", fallback=f"arquivo_{arq.get('CodArquivo','x')}")
        destino = pasta / nome
        try:
            s3_baixar(
                bucket=arq["BucketS3"],
                key=arq["NomeArquivoBucketS3"],
                access_key=arq["AcesseKey"],
                secret_key=arq["SecretKey"],
                region=arq["Region"],
                destino=destino,
                pular_se_existir=True,
            )
            paths.append(destino)
        except Exception as e:
            log.error("CodItem=%s: falha baixando '%s': %s", cod_item, arq.get("NomeArquivo"), e)

    # Dedup: tbarquivosprocesso às vezes tem múltiplas linhas pro mesmo arquivo
    # (re-upload, bug do Jurify, etc.). Se o nome final é igual, o destino local
    # também é, e mandar o mesmo PDF duas vezes faz o eproc rejeitar a submissão.
    antes = len(paths)
    paths = list(dict.fromkeys(paths))
    if len(paths) < antes:
        log.warning(
            "CodItem=%s: removidos %d arquivo(s) duplicado(s) — %d → %d",
            cod_item, antes - len(paths), antes, len(paths),
        )

    pdfs = [p for p in paths if p.suffix.lower() == ".pdf" and not p.name.lower().startswith("recibo")]
    if not pdfs:
        log.error("CodItem=%s: nenhum PDF utilizável após download", cod_item)
        return None

    if len(pdfs) == 1:
        principal: Path = pdfs[0]
        anexos: list[Path] = []
        log.info("CodItem=%s: single-doc — %s", cod_item, principal.name)
    else:
        principal, breakdown = identificar_peticao_principal(pdfs, cnj_digits)
        log.info("CodItem=%s: identificação por conteúdo (threshold=%d):", cod_item, args.threshold_principal)
        for p, sc in breakdown.items():
            marker = " <-- PRINCIPAL" if p == principal else ""
            log.info(
                "  %-50s total=%3d  end=%2d cnj=%2d oab=%2d def=%2d%s",
                p.name, sc["total"], sc["enderecamento"], sc["cnj"],
                sc["oab"], sc["deferimento"], marker,
            )
        if breakdown[principal]["total"] < args.threshold_principal:
            log.error(
                "CodItem=%s: ABORT — score do melhor candidato (%d) abaixo do "
                "threshold (%d). Não dá pra confiar na identificação.",
                cod_item, breakdown[principal]["total"], args.threshold_principal,
            )
            return None
        anexos = [p for p in pdfs if p != principal]

    tolerar = bool(getattr(args, "tolerar_erro_material", False))
    pular = bool(getattr(args, "pular_validacao_cnj", False))
    if pular:
        log.warning(
            "CodItem=%s: VALIDAÇÃO DE CNJ NO PDF DESLIGADA (--pular-validacao-cnj) — "
            "alvo %s sem checar conteúdo do PDF '%s'. Use só com autorização explícita do "
            "advogado responsável; o RPA fica sem rede de segurança contra PDFs do processo errado.",
            cod_item, cnj_digits, principal.name,
        )
    elif not cnj_no_pdf(principal, cnj_digits, tolerar_erro_material=tolerar):
        log.error("CodItem=%s: ABORT — CNJ %s não aparece no PDF principal %s",
                  cod_item, cnj_digits, principal.name)
        if not tolerar:
            log.error("  → Se for mero erro material (1 dígito faltando/sobrando), "
                      "rode com --tolerar-erro-material")
        log.error("  → Se o documento não tem o CNJ declarado (campo em branco ou "
                  "outro motivo confirmado pelo advogado), rode com --pular-validacao-cnj")
        return None

    principal = _sanitizar_e_renomear(principal, log)
    anexos = [_sanitizar_e_renomear(a, log) for a in anexos]

    # Otimização de PDFs acima do limite do eproc:
    # - comprime com Ghostscript (/ebook → /screen)
    # - se ainda acima, divide em partes ≤ limite
    # - se mesmo dividido alguma parte ficar > limite: aborta o item
    try:
        partes_principal = otimizar_para_eproc(principal, limite_mb=args.pdf_limite_mb)
    except PdfMuitoGrandeError as e:
        log.error("CodItem=%s: ABORT — peça principal '%s' não cabe no limite: %s",
                  cod_item, principal.name, e)
        return None
    # Se principal foi dividido, parte 1 fica como principal e o restante
    # entra no início dos anexos (preservando ordem).
    if len(partes_principal) > 1:
        log.info("CodItem=%s: principal dividida em %d partes — partes 2..N viram anexos no início",
                 cod_item, len(partes_principal))
    principal = partes_principal[0]
    anexos_de_split_principal = partes_principal[1:]

    # Aplica a mesma otimização em cada anexo
    anexos_otimizados: list[Path] = []
    for a in anexos:
        try:
            partes = otimizar_para_eproc(a, limite_mb=args.pdf_limite_mb)
            anexos_otimizados.extend(partes)
        except PdfMuitoGrandeError as e:
            log.error("CodItem=%s: ABORT — anexo '%s' não cabe no limite: %s",
                      cod_item, a.name, e)
            return None

    anexos = anexos_de_split_principal + anexos_otimizados

    return {
        "cod_item": cod_item,
        "cnj_digits": cnj_digits,
        "principal": principal,
        "anexos": anexos,
        "num_processo": item.get("NumProcesso"),
        "tribunal_id": tribunal_id,
        "eproc_base": eproc_base,
    }


def _finalizar_sucessos(sucessos, log) -> tuple[list, list]:
    """Fase 3: sobe o recibo no painel Jurify + marca concluído, pra cada sucesso
    que tem recibo. Retorna (finalizados, falhas_finalizacao). Idempotente: se o
    item já está concluído, o upload_recibo_painel não refaz nada.
    """
    finalizados: list[tuple[int, dict]] = []
    falhas_finalizacao: list[tuple[int, str]] = []
    pendentes = [(c, cnj, r) for c, cnj, r in sucessos if r is not None]
    if pendentes:
        log.info("FASE 3 — finalizando %d item(ns) no painel Jurify", len(pendentes))
    for cod, cnj, recibo in pendentes:
        try:
            resultado = upload_recibo_painel(
                recibo_path=recibo, cod_item=cod, nome_arquivo_painel=f"recibo-{cnj}-{cod}.pdf",
            )
            if resultado.get("ja_concluido"):
                log.info("CodItem=%s: painel já estava em status concluído — nada feito", cod)
            else:
                log.info("CodItem=%s: painel atualizado — CodArquivo=%s s3://%s/%s",
                         cod, resultado["cod_arqv"], resultado["bucket"], resultado["keyfile"])
            finalizados.append((cod, resultado))
        except JurifyError as e:
            log.error("CodItem=%s: falha finalizando painel — %s", cod, e)
            falhas_finalizacao.append((cod, str(e)))
        except Exception as e:
            log.exception("CodItem=%s: erro inesperado finalizando painel", cod)
            falhas_finalizacao.append((cod, f"{type(e).__name__}: {e}"))
    return finalizados, falhas_finalizacao


def cmd_processar(args: argparse.Namespace) -> int:
    """Processa em lote uma lista de CodItems: DB → S3 → eproc.

    Fase 1: sem browser, prepara cada item (query, download, identifica principal).
    Fase 2: agrupa por tribunal e abre 1 adapter por grupo (login único por tribunal),
            loop fazendo consulta/movimentação/upload/confirmar/[peticionar].
    Fase 3: painel Jurify (independente de tribunal).
    """
    from collections import defaultdict

    settings, cookie_store = _bootstrap_base()
    log = get_logger("cli")

    # ---- Fase 1: preparação ----
    log.info("=" * 64)
    log.info("FASE 1 — preparando %d item(ns)", len(args.cod_items))
    log.info("=" * 64)
    a_processar: list[dict] = []
    bloqueados: list[int] = []
    for cod in args.cod_items:
        log.info("-- CodItem=%s --", cod)
        prep = _preparar_item(cod, args, log)
        if prep is None:
            bloqueados.append(cod)
        else:
            a_processar.append(prep)

    if not a_processar:
        log.error("nenhum item passou a fase de preparação — nada pra fazer no browser")
        _imprimir_resumo_processar(bloqueados, [], [])
        return 3

    # Agrupa por tribunal — vamos abrir um adapter por grupo (1 login por tribunal)
    por_tribunal: dict[str, list[dict]] = defaultdict(list)
    for prep in a_processar:
        por_tribunal[prep["tribunal_id"]].append(prep)

    # ---- Fase 2: browser, um login por tribunal ----
    log.info("=" * 64)
    log.info(
        "FASE 2 — protocolizando %d item(ns) em %d tribunal(is) (peticionar=%s)",
        len(a_processar), len(por_tribunal), args.peticionar,
    )
    log.info("=" * 64)
    # Sinaliza pra operação humana que o robô pegou os itens — evita execução
    # manual paralela. Se algo falhar adiante, voltam pro pool com status 10.
    for prep in a_processar:
        try:
            marcar_em_execucao(prep["cod_item"])
        except Exception as e:
            log.warning("CodItem=%s: falha marcando em execução (11) — %s",
                        prep["cod_item"], e)
    sucessos: list[tuple[int, str, Path | None]] = []
    falhas: list[tuple[int, str]] = []

    # Fase 3 (finalização no Jurify) — acumuladores. No caminho paralelo a Fase 3
    # roda INCREMENTALMENTE (cada tribunal finaliza assim que seu subprocesso
    # retorna), pra que um subprocesso travado não bloqueie a finalização dos
    # outros. No serial, roda no fim.
    finalizados: list[tuple[int, dict]] = []
    falhas_finalizacao: list[tuple[int, str]] = []
    fase3_ativa = bool(args.finalizar and args.peticionar)

    if args.parallel_tribunais and len(por_tribunal) > 1:
        from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutTimeout, as_completed
        log.info("paralelizando %d tribunal(is) em processos separados",
                 len(por_tribunal))
        # Timeout escalonado pelo maior grupo: lotes grandes têm mais tempo;
        # um subprocesso travado num lote pequeno é abandonado rápido (os recibos
        # já estão salvos → recuperável com `finalizar`). Override: RPA_PARALLEL_TIMEOUT_S.
        maior = max(len(v) for v in por_tribunal.values())
        timeout_s = int(os.getenv("RPA_PARALLEL_TIMEOUT_S", str(600 + 120 * maior)))
        log.info("timeout por aguardo de subprocesso: %ds (maior grupo=%d itens)", timeout_s, maior)
        # `vars(args)` é pickle-safe (só primitivos do argparse).
        args_dict = vars(args)
        with ProcessPoolExecutor(max_workers=len(por_tribunal)) as exe:
            futures = {
                exe.submit(_executar_grupo_em_processo, tid, itens, args_dict): tid
                for tid, itens in por_tribunal.items()
            }
            pendentes = set(futures)
            try:
                for fut in as_completed(futures, timeout=timeout_s):
                    pendentes.discard(fut)
                    tid = futures[fut]
                    try:
                        s, f = fut.result()
                        sucessos.extend(s)
                        falhas.extend(f)
                        # Fase 3 incremental: finaliza JÁ os itens deste tribunal.
                        if fase3_ativa:
                            fin, ff = _finalizar_sucessos(s, log)
                            finalizados.extend(fin)
                            falhas_finalizacao.extend(ff)
                    except Exception as e:
                        log.exception("processo do tribunal %s falhou: %s", tid, e)
                        falhas.extend(
                            (it["cod_item"], f"processo do tribunal {tid} falhou: {e}")
                            for it in por_tribunal[tid]
                        )
            except FutTimeout:
                # Um ou mais subprocessos travaram. Abandona — seus recibos já
                # estão salvos localmente e dá pra recuperar com `main.py finalizar`.
                travados = [futures[f] for f in pendentes]
                log.error("TIMEOUT (%ds): subprocesso(s) travado(s): %s — abandonando. "
                          "Itens recuperáveis via `finalizar` (recibos já salvos).",
                          timeout_s, travados)
                for f in pendentes:
                    f.cancel()
                    falhas.extend(
                        (it["cod_item"], f"subprocesso {futures[f]} travado (timeout {timeout_s}s)")
                        for it in por_tribunal[futures[f]]
                    )
    else:
        for tribunal_id, itens in por_tribunal.items():
            s, f = _executar_grupo(tribunal_id, itens, args, settings, cookie_store, log)
            sucessos.extend(s)
            falhas.extend(f)
        if fase3_ativa:
            finalizados, falhas_finalizacao = _finalizar_sucessos(sucessos, log)

    # Devolve pro pool (status 10) tudo que falhou em Fase 2 ou Fase 3 —
    # próxima rodada pega de novo. Sucesso da Fase 3 já move pra 9 via SP.
    for cod, _ in list(falhas) + list(falhas_finalizacao):
        try:
            marcar_como_erro(cod)
        except Exception as e:
            log.warning("CodItem=%s: falha marcando como erro (10) — %s", cod, e)

    _imprimir_resumo_processar(bloqueados, sucessos, falhas, finalizados, falhas_finalizacao)
    erro_geral = bool(bloqueados or falhas or falhas_finalizacao)
    return 0 if not erro_geral else 3


def _executar_grupo(
    tribunal_id: str, itens: list[dict], args, settings, cookie_store, log,
) -> tuple[list, list]:
    """Roda a Fase 2 de um único tribunal — usado tanto no caminho serial
    quanto dentro de subprocessos paralelos. Retorna (sucessos, falhas)
    em vez de mutar listas in-place pra ser amigável a `ProcessPoolExecutor`.
    """
    sucessos: list = []
    falhas: list = []

    adapter_cls = ADAPTERS.get(tribunal_id)
    if adapter_cls is None:
        log.error("tribunal %r sem adapter registrado", tribunal_id)
        falhas.extend((it["cod_item"], f"sem adapter para {tribunal_id}") for it in itens)
        return sucessos, falhas

    log.info("-" * 64)
    log.info("Tribunal: %s — %d item(ns) — %s",
             tribunal_id, len(itens), adapter_cls.LOGIN_URL)
    log.info("-" * 64)
    try:
        cliente_store = EnvClienteStore.from_env(tribunal_id)
    except RuntimeError as e:
        log.error("credenciais do tribunal %s ausentes: %s", tribunal_id, e)
        falhas.extend((it["cod_item"], f"credenciais ausentes {tribunal_id}") for it in itens)
        return sucessos, falhas
    cliente = cliente_store.get(ENV_CLIENTE_ID)

    if args.workers <= 1:
        _processar_grupo_sync(
            adapter_cls, cliente, cliente_store, cookie_store,
            settings, args, log, itens, sucessos, falhas,
        )
    else:
        _processar_grupo_async(
            adapter_cls, cliente, cliente_store, cookie_store,
            settings, args, log, tribunal_id, itens, sucessos, falhas,
        )
    return sucessos, falhas


def _executar_grupo_em_processo(
    tribunal_id: str, itens: list[dict], args_dict: dict,
) -> tuple[list, list]:
    """Entry-point pickle-safe pra `ProcessPoolExecutor.submit`. Reconstrói
    settings/logger/cookie_store dentro do subprocesso (não dá pra serializar
    handles abertos) e delega pra `_executar_grupo`.

    Usa logger com suffix=tribunal_id pra que cada subprocesso escreva no
    seu próprio `rpa-{tribunal}.log`, evitando que rotações concorrentes
    sobrescrevam mensagens de outros tribunais.
    """
    import argparse as _ap
    settings = Settings.load()
    setup_logger(settings.logs_dir, suffix=tribunal_id)
    cookie_store = CookieStore(settings.cookies_dir)
    args = _ap.Namespace(**args_dict)
    log = get_logger(f"cli.{tribunal_id}")
    return _executar_grupo(tribunal_id, itens, args, settings, cookie_store, log)


def _processar_grupo_sync(
    adapter_cls, cliente, cliente_store, cookie_store,
    settings, args, log, itens,
    sucessos: list, falhas: list,
) -> None:
    """Loop sync original — 1 browser, items sequenciais. Comportamento default."""
    with adapter_cls(
        cliente,
        settings=settings,
        cliente_store=cliente_store,
        cookie_store=cookie_store,
        headless=args.headless,
    ) as adapter:
        try:
            adapter.login()
        except Exception as e:
            log.exception("login falhou — pulando todos do grupo: %s", e)
            falhas.extend((it["cod_item"], f"login falhou: {e}") for it in itens)
            return

        for prep in itens:
            cod = prep["cod_item"]
            try:
                log.info("-- CodItem=%s (CNJ %s, %s) --",
                         cod, prep["cnj_digits"], prep.get("tribunal_id"))
                cons = adapter.consultar_processo(prep["cnj_digits"])
                if not cons.encontrado:
                    log.error("CodItem=%s: processo não encontrado", cod)
                    falhas.append((cod, "processo não encontrado"))
                    continue

                adapter.movimentar_peticionar(evento=args.evento)

                if adapter._contar_doc_na_tabela_selecionados(prep["principal"].name) > 0:
                    log.warning("CodItem=%s: peça já na tabela — pulando uploads", cod)
                else:
                    adapter.anexar_documento(arquivo=prep["principal"], tipo=args.tipo, documento=1)
                    for i, anex in enumerate(prep["anexos"], start=2):
                        adapter.adicionar_mais_documentos(novo_n=i)
                        adapter.anexar_documento(arquivo=anex, tipo=args.tipo_anexo, documento=i)
                    adapter.confirmar_documentos(
                        aguardar_nomes=[prep["principal"].name] + [a.name for a in prep["anexos"]],
                    )

                if args.peticionar:
                    recibo = adapter.peticionar_e_capturar_recibo(
                        numero=prep["cnj_digits"],
                        pasta_destino=prep["principal"].parent,
                        nome_arquivo_anexo=prep["principal"].name,
                    )
                    log.info("CodItem=%s: PROTOCOLADO — %s", cod, recibo)
                    sucessos.append((cod, prep["cnj_digits"], recibo))
                else:
                    log.info("CodItem=%s: confirmado (sem --peticionar)", cod)
                    sucessos.append((cod, prep["cnj_digits"], None))
            except Exception as e:
                log.exception("CodItem=%s: falhou no browser — %s", cod, e)
                falhas.append((cod, f"{type(e).__name__}: {e}"))


def _processar_grupo_async(
    adapter_cls, cliente, cliente_store, cookie_store,
    settings, args, log, tribunal_id, itens,
    sucessos: list, falhas: list,
) -> None:
    """Modelo A: login sync → captura cookies → workers async (várias abas) paralelos.

    - Login serial (igual ao sync): cria browser sync, faz login completo, captura
      cookies do BrowserContext, fecha browser sync.
    - Workers async: 1 browser Chromium async, 1 context com os cookies injetados,
      N workers (= args.workers) consumindo a fila com semáforo. Cada worker
      tem sua própria Page e roda o pipeline completo no item designado.
    - Jitter aleatório no início de cada worker pra distribuir requests no tempo.
    """
    # eproc-SP penaliza concorrência server-side dentro da mesma sessão:
    # workers em Fase 2 (Movimentar/Peticionar) "ocupam" a sessão e novas
    # consultas vindas de outras abas caem na tela vazia (timeout). Solução:
    # 1 sessão (login) por worker — N logins sequenciais (TOTP timing guard
    # cuida das colisões de janela), depois N BrowserContexts isolados em
    # paralelo. MG/RS seguem com 1 sessão compartilhada (sem essa penalidade).
    # SP e RJ penalizam concorrência server-side dentro da mesma sessão (SP cai
    # em tela de Consulta vazia; RJ trava o menu lateral). Pra ambos, isola
    # sessão por worker. MG/RS seguem com sessão compartilhada.
    sessoes_isoladas = tribunal_id in {"eproc_sp", "eproc_rj"} and args.workers > 1
    n_logins = args.workers if sessoes_isoladas else 1
    log.info(
        "login serial pra %s (%d sessão(ões)) — depois %d workers async paralelos",
        tribunal_id, n_logins, args.workers,
    )
    cookies_por_sessao: list[list] = []
    for sid in range(n_logins):
        # Entre logins SP, aguarda a próxima janela TOTP — o Keycloak rejeita
        # o mesmo código se reutilizado, então não dá pra fazer N logins na
        # mesma janela 30s. A 1ª sessão usa a janela atual; as próximas
        # esperam a virada.
        if sessoes_isoladas and sid > 0:
            import time as _t
            espera = 30 - (_t.time() % 30) + 1
            log.info(
                "aguardando %.1fs próxima janela TOTP antes da sessão %d",
                espera, sid + 1,
            )
            _t.sleep(espera)
        try:
            with adapter_cls(
                cliente, settings=settings,
                cliente_store=cliente_store, cookie_store=cookie_store,
                headless=args.headless,
            ) as login_adapter:
                login_adapter.login()
                cookies = login_adapter.context.cookies() if login_adapter.context else []
                cookies_por_sessao.append(cookies)
                log.info(
                    "login sessão %d/%d OK — %d cookies",
                    sid + 1, n_logins, len(cookies),
                )
        except Exception as e:
            log.exception("login sessão %d falhou: %s", sid + 1, e)

    if not cookies_por_sessao:
        log.error("nenhum login bem-sucedido — pulando grupo")
        falhas.extend((it["cod_item"], "nenhum login funcionou") for it in itens)
        return

    flow_cls = ASYNC_FLOWS.get(tribunal_id)
    if flow_cls is None:
        log.error("tribunal %r sem AsyncFlow registrado — caindo no sync", tribunal_id)
        _processar_grupo_sync(
            adapter_cls, cliente, cliente_store, cookie_store,
            settings, args, log, itens, sucessos, falhas,
        )
        return

    resultados = asyncio.run(_rodar_workers_async(
        cookies_por_sessao=cookies_por_sessao,
        flow_cls=flow_cls,
        base_url=adapter_cls.LOGIN_URL,
        itens=itens,
        args=args,
        settings=settings,
        log=log,
        tribunal_id=tribunal_id,
    ))

    for r in resultados:
        cod = r["cod_item"]
        if r["ok"]:
            sucessos.append((cod, r["cnj_digits"], r.get("recibo")))
        else:
            falhas.append((cod, r.get("erro") or "erro desconhecido"))


async def _rodar_workers_async(
    cookies_por_sessao, flow_cls, base_url, itens, args, settings, log, tribunal_id,
) -> list[dict]:
    """Roda os workers async em paralelo.

    `cookies_por_sessao` é list[list[cookie]]. Se vier 1 sessão, todos os
    workers compartilham 1 BrowserContext (MG/RS). Se vier N sessões, cria
    N contexts isolados e cada worker usa o context cujo índice corresponde
    ao seu wid (SP — evita interferência server-side entre sessões).
    """
    from playwright.async_api import async_playwright

    headless = settings.headless if args.headless is None else args.headless

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        contexts = []
        for cookies in cookies_por_sessao:
            ctx = await browser.new_context()
            await ctx.add_cookies(cookies)
            contexts.append(ctx)

        log.info(
            "disparando %d itens em %d worker(s) async (%d sessão(ões))",
            len(itens), args.workers, len(contexts),
        )

        if len(contexts) == 1:
            # 1 sessão compartilhada (MG/RS) — N workers pegam da fila via semáforo
            sem = asyncio.Semaphore(args.workers)
            ctx_compartilhado = contexts[0]

            async def _worker(wid: int, prep: dict) -> dict:
                async with sem:
                    await asyncio.sleep(random.uniform(0.1, 0.4) + wid * 0.05)
                    worker_log = get_logger(f"{tribunal_id}.w{wid}")
                    page = await ctx_compartilhado.new_page()
                    try:
                        flow = flow_cls(page, worker_log, settings.logs_dir, base_url)
                        return await flow.processar_item(prep, args)
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            pass

            tasks = [_worker(wid, prep) for wid, prep in enumerate(itens)]
            results = await asyncio.gather(*tasks, return_exceptions=False)
        else:
            # N sessões isoladas (SP) — N "lanes", cada uma dona do seu context,
            # todas consumindo da mesma fila. Garante que cada sessão processa
            # itens sequencialmente, sem concorrência interna.
            fila: asyncio.Queue = asyncio.Queue()
            for prep in itens:
                fila.put_nowait(prep)

            async def _lane(lid: int, ctx) -> list[dict]:
                await asyncio.sleep(random.uniform(0.1, 0.4) + lid * 0.05)
                worker_log = get_logger(f"{tribunal_id}.w{lid}")
                out: list[dict] = []
                while True:
                    try:
                        prep = fila.get_nowait()
                    except asyncio.QueueEmpty:
                        return out
                    page = await ctx.new_page()
                    try:
                        flow = flow_cls(page, worker_log, settings.logs_dir, base_url)
                        out.append(await flow.processar_item(prep, args))
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            pass

            lane_results = await asyncio.gather(
                *[_lane(i, contexts[i]) for i in range(len(contexts))],
                return_exceptions=False,
            )
            results = [r for lst in lane_results for r in lst]

        for ctx in contexts:
            await ctx.close()
        await browser.close()
        return list(results)


def _imprimir_resumo_processar(
    bloqueados: list[int],
    sucessos: list[tuple[int, str, Path | None]],
    falhas: list[tuple[int, str]],
    finalizados: list[tuple[int, dict]] | None = None,
    falhas_finalizacao: list[tuple[int, str]] | None = None,
) -> None:
    print("\n" + "=" * 64)
    print("RESUMO")
    print("=" * 64)
    print(f"bloqueados em pré-checks: {len(bloqueados)}")
    for c in bloqueados:
        print(f"  ✗ {c}")
    print(f"protocolados no eproc: {len(sucessos)}")
    for c, cnj, rec in sucessos:
        rec_str = rec.name if rec else "(sem --peticionar)"
        print(f"  ✓ {c} → CNJ {cnj} — {rec_str}")
    print(f"falhas no browser: {len(falhas)}")
    for c, msg in falhas:
        print(f"  ✗ {c} — {msg}")
    if finalizados is not None:
        print(f"finalizados no painel Jurify: {len(finalizados)}")
        for c, res in finalizados:
            if res.get("ja_concluido"):
                tag = "(já estava concluído)"
            else:
                tag = f"CodArquivo={res['cod_arqv']} key={res['keyfile']}"
            print(f"  ✓ {c} — {tag}")
    if falhas_finalizacao:
        print(f"falhas finalizando painel: {len(falhas_finalizacao)}")
        for c, msg in falhas_finalizacao:
            print(f"  ✗ {c} — {msg}")


def cmd_finalizar(args: argparse.Namespace) -> int:
    """Standalone — só Fase 3: pra cada CodItem, sobe o recibo já gerado no
    bucket Jurify e marca o item como concluído no painel.

    Resolve o recibo por convenção: `./<NumProcessoCNJ>/recibo<NumProcessoCNJ>.pdf`.
    Idempotente (item já em status 9 vira no-op).
    """
    settings = Settings.load()
    setup_logger(settings.logs_dir)
    log = get_logger("cli")

    sucessos: list[tuple[int, dict]] = []
    falhas: list[tuple[int, str]] = []

    for cod in args.cod_items:
        try:
            proc = buscar_processo_por_cod_item(cod)
            if not proc:
                log.error("CodItem=%s não existe em tbitens", cod)
                falhas.append((cod, "cod_item inexistente"))
                continue
            cnj = re.sub(r"\D", "", str(proc.get("NumProcessoCNJ") or ""))
            if not cnj:
                log.error("CodItem=%s sem NumProcessoCNJ", cod)
                falhas.append((cod, "sem CNJ"))
                continue

            if args.recibo:
                recibo_path = Path(args.recibo).expanduser().resolve()
            else:
                # Recibo na pasta ISOLADA do CodItem (./<CNJ>_<CodItem>/). NUNCA
                # cair no recibo<CNJ>.pdf compartilhado — itens diferentes do mesmo
                # processo têm recibos diferentes. Se o recibo deste CodItem não
                # existe, FALHA (não finaliza com recibo de outro item).
                recibo_path = _pasta_item(cnj, cod) / f"recibo{cnj}.pdf"

            if not recibo_path.exists():
                log.error("CodItem=%s: recibo não encontrado em %s — NÃO finaliza "
                          "(cada CodItem precisa do PRÓPRIO recibo).", cod, recibo_path)
                falhas.append((cod, f"recibo ausente: {recibo_path}"))
                continue

            resultado = upload_recibo_painel(
                recibo_path=recibo_path,
                cod_item=cod,
                nome_arquivo_painel=f"recibo-{cnj}-{cod}.pdf",
            )
            if resultado.get("ja_concluido"):
                log.info("CodItem=%s: painel já estava concluído — nada feito", cod)
            else:
                log.info(
                    "CodItem=%s: painel finalizado — CodArquivo=%s s3://%s/%s",
                    cod, resultado["cod_arqv"], resultado["bucket"], resultado["keyfile"],
                )
            sucessos.append((cod, resultado))
        except JurifyError as e:
            log.error("CodItem=%s: %s", cod, e)
            falhas.append((cod, str(e)))
        except Exception as e:
            log.exception("CodItem=%s: erro inesperado", cod)
            falhas.append((cod, f"{type(e).__name__}: {e}"))

    print("\n" + "=" * 64)
    print("RESUMO — finalizar")
    print("=" * 64)
    print(f"finalizados: {len(sucessos)}")
    for c, res in sucessos:
        if res.get("ja_concluido"):
            tag = "(já estava concluído)"
        else:
            tag = f"CodArquivo={res['cod_arqv']} key={res['keyfile']}"
        print(f"  ✓ {c} — {tag}")
    print(f"falhas: {len(falhas)}")
    for c, msg in falhas:
        print(f"  ✗ {c} — {msg}")
    return 0 if not falhas else 3


def cmd_baixar_aptos(args: argparse.Namespace) -> int:
    """Lista aptos no MySQL, cria pasta ./<CNJ>/ por processo, baixa cada arquivo do S3."""
    settings = Settings.load()
    setup_logger(settings.logs_dir)
    log = get_logger("cli")
    try:
        itens = listar_protocolos_aptos(
            dt_cadastro_minimo=args.desde,
            dt_cadastro_maximo=_hoje(),
            cod_item=args.cod_item,
            limit=args.limit,
        )
    except DBConfigError as e:
        log.error(str(e))
        return 1
    except Exception as e:
        log.exception("erro consultando MySQL: %s", e)
        return 2

    if not itens:
        log.info("nenhum protocolo apto encontrado")
        print("(nenhum protocolo apto a processar)")
        return 0

    total_arquivos = 0
    erros = 0
    for it in itens:
        cod_item = it["CodItem"]
        num = it.get("NumProcesso") or "(sem NumProcesso)"
        cnj_digits = re.sub(r"\D", "", str(it.get("NumProcessoCNJ") or ""))
        if not cnj_digits:
            log.error("CodItem=%s sem NumProcessoCNJ — pulando", cod_item)
            erros += 1
            continue

        pasta = Path.cwd() / cnj_digits
        pasta.mkdir(parents=True, exist_ok=True)
        log.info("processo %s (CodItem=%s) -> %s", num, cod_item, pasta)

        try:
            arquivos = listar_arquivos_do_item(cod_item)
        except Exception as e:
            log.exception("erro listando arquivos do CodItem=%s: %s", cod_item, e)
            erros += 1
            continue

        if not arquivos:
            log.warning("nenhum arquivo em tbarquivosprocesso para CodItem=%s", cod_item)
            continue

        log.info("%d arquivo(s) a baixar", len(arquivos))
        for arq in arquivos:
            nome_original = arq.get("NomeArquivo") or f"arquivo_{arq.get('CodArquivo','x')}.bin"
            nome_local = sanitizar_nome(nome_original, fallback=f"arquivo_{arq.get('CodArquivo','x')}")
            destino = pasta / nome_local
            try:
                baixou = s3_baixar(
                    bucket=arq["BucketS3"],
                    key=arq["NomeArquivoBucketS3"],
                    access_key=arq["AcesseKey"],
                    secret_key=arq["SecretKey"],
                    region=arq["Region"],
                    destino=destino,
                    pular_se_existir=not args.force,
                )
                if baixou:
                    log.info("  ✓ baixado: %s", destino.name)
                    total_arquivos += 1
                else:
                    log.info("  · já existe (pulado): %s", destino.name)
            except Exception as e:
                log.exception("  ✗ falha em %s: %s", nome_original, e)
                erros += 1

    print(
        f"\nResumo: {len(itens)} item(ns) processado(s), "
        f"{total_arquivos} arquivo(s) baixado(s), {erros} erro(s)."
    )
    return 0 if erros == 0 else 3


def cmd_listar_aptos(args: argparse.Namespace) -> int:
    """Roda a query do MySQL e mostra os itens aptos a processar."""
    settings = Settings.load()
    setup_logger(settings.logs_dir)
    log = get_logger("cli")
    try:
        itens = listar_protocolos_aptos(
            dt_cadastro_minimo=args.desde,
            dt_cadastro_maximo=_hoje(),
            cod_item=args.cod_item,
            limit=args.limit,
        )
    except DBConfigError as e:
        log.error(str(e))
        return 1
    except Exception as e:
        log.exception("erro consultando MySQL: %s", e)
        return 2

    if not itens:
        print("(nenhum protocolo apto a processar)")
        return 0

    print(f"\n{len(itens)} protocolo(s) apto(s):\n")
    print(f"  {'CodItem':<10} {'IdProc':<10} {'NumProcesso':<26} CNJ (digits)")
    for it in itens:
        print(
            f"  {str(it.get('CodItem','')):<10} "
            f"{str(it.get('IdProc','')):<10} "
            f"{str(it.get('NumProcesso','')):<26} "
            f"{it.get('NumProcessoCNJ','')}"
        )
    return 0


def cmd_listar(_: argparse.Namespace) -> int:
    settings, _ = _bootstrap_base()
    log = get_logger("cli")
    try:
        cliente_store = _json_store(settings)
    except RuntimeError as e:
        log.error(str(e))
        return 1

    clientes = cliente_store.list()
    if not clientes:
        print("(nenhum cliente cadastrado em clientes.json — use scripts/cadastrar_cliente.py)")
        return 0
    print(f"{'ID':<6} {'TRIBUNAL':<12} {'USUARIO':<20} NOME")
    for c in sorted(clientes, key=lambda x: x.id):
        print(f"{c.id:<6} {c.tribunal:<12} {c.usuario:<20} {c.nome}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rpa", description="RPA protocolo eletrônico (eproc, ...)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="executa login no tribunal")
    p_login.add_argument(
        "--cliente",
        default=None,
        help="id do cliente no clientes.json (multi-client). Sem este flag, lê credenciais do .env.",
    )
    p_login.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="força headless on/off (sobrescreve .env)",
    )
    p_login.set_defaults(func=cmd_login)

    p_cons = sub.add_parser("consultar", help="loga e consulta um processo pelo número CNJ")
    p_cons.add_argument("numero", help="número CNJ do processo (20 dígitos, com ou sem pontuação)")
    p_cons.add_argument("--cliente", default=None, help="id do cliente no clientes.json")
    p_cons.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="força headless on/off",
    )
    p_cons.set_defaults(func=cmd_consultar)

    p_pet = sub.add_parser(
        "peticionar",
        help="loga, consulta processo, abre Movimentar/Peticionar e anexa PDF",
    )
    p_pet.add_argument("numero", help="número CNJ do processo")
    p_pet.add_argument(
        "--arquivo",
        default=None,
        help="caminho do PDF único (modo single-doc). Sem isso, lê todos os PDFs "
             "(exceto recibo*.pdf) de ./<numero>/ e identifica o principal por conteúdo.",
    )
    p_pet.add_argument(
        "--principal",
        default=None,
        help="path do PDF principal (override da identificação automática)",
    )
    p_pet.add_argument(
        "--evento",
        default="PETIÇÃO",
        help="rótulo exato do evento no autocomplete (default: PETIÇÃO)",
    )
    p_pet.add_argument(
        "--tipo",
        default="Petição",
        help="tipo da peça principal (default: Petição)",
    )
    p_pet.add_argument(
        "--tipo-anexo",
        default="Anexo",
        help="tipo dos anexos quando multi-doc (default: Anexo)",
    )
    p_pet.add_argument(
        "--preview",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="quando rodar sem --peticionar, salva preview<numero>.png na pasta "
             "do processo pra você verificar antes de protocolizar (default: True)",
    )
    p_pet.add_argument(
        "--confirmar",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="clica em 'Confirmar seleção de documentos' (default: True). "
             "Use --no-confirmar pra apenas anexar sem submeter.",
    )
    p_pet.add_argument(
        "--peticionar",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="depois de confirmar, clica em 'Peticionar' (protocolo definitivo) e "
             "salva screenshot da tela resultante como recibo<numero>.pdf na pasta do processo.",
    )
    p_pet.add_argument("--cliente", default=None, help="id do cliente no clientes.json")
    p_pet.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="força headless on/off",
    )
    p_pet.set_defaults(func=cmd_peticionar)

    p_aptos = sub.add_parser(
        "listar-aptos",
        help="consulta o MySQL e lista protocolos aptos a processar (eproc-MG)",
    )
    p_aptos.add_argument(
        "--desde",
        default=_default_desde(),
        help=f"data mínima de cadastro (default: últimos 7 dias = {_default_desde()})",
    )
    p_aptos.add_argument(
        "--cod-item",
        type=int,
        default=None,
        help="filtra por um CodItem específico (útil em teste)",
    )
    p_aptos.add_argument(
        "--limit",
        type=int,
        default=None,
        help="limita o número de linhas retornadas",
    )
    p_aptos.set_defaults(func=cmd_listar_aptos)

    p_proc = sub.add_parser(
        "processar",
        help="pipeline completo em lote: DB → S3 → eproc (com peticionar opcional)",
    )
    p_proc.add_argument("cod_items", nargs="+", type=int, help="CodItems a processar")
    p_proc.add_argument(
        "--desde",
        default=_default_desde(),
        help=f"data mínima de cadastro (default: últimos 7 dias = {_default_desde()})",
    )
    p_proc.add_argument("--evento", default="PETIÇÃO")
    p_proc.add_argument("--tipo", default="Petição", help="tipo da peça principal")
    p_proc.add_argument("--tipo-anexo", default="Anexo", help="tipo dos anexos")
    p_proc.add_argument(
        "--threshold-principal",
        type=int,
        default=30,
        help="score mínimo (do identificador por conteúdo) pra confirmar a peça principal. "
             "Default 30 = pelo menos o endereçamento ao juiz precisa estar presente.",
    )
    p_proc.add_argument(
        "--tolerar-erro-material",
        action="store_true",
        help="aceita CNJ no PDF com diferença de 1 char inserido OU removido "
             "(mero erro material de digitação). Nunca aceita substituição. "
             "Use só item por item, em revisão explícita.",
    )
    p_proc.add_argument(
        "--pular-validacao-cnj",
        action="store_true",
        help="DESLIGA a validação de CNJ no conteúdo do PDF. Use só quando o "
             "documento legitimamente não tem o CNJ declarado (campo em branco, "
             "PDF gerado sem header, etc.) e o advogado autorizou explicitamente. "
             "Loga WARNING bem visível pra auditoria.",
    )
    p_proc.add_argument(
        "--workers",
        type=int,
        default=1,
        help="número de abas paralelas por tribunal (default: 1 = sequencial). "
             "Recomendado: 3. Login fica serial; apenas a Fase 2 (consulta/upload/"
             "peticionar) paraleliza, compartilhando a mesma sessão.",
    )
    p_proc.add_argument(
        "--pdf-limite-mb",
        type=int,
        default=10,
        help="limite em MB por arquivo PDF. PDFs maiores passam por comprimir "
             "(Ghostscript /ebook → /screen) e, se ainda grandes, são divididos "
             "em partes ≤ limite mantendo ordem. Default: 10MB (margem segura).",
    )
    p_proc.add_argument(
        "--peticionar",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="clica em 'Peticionar' (protocolo definitivo). Default: False (apenas confirmar).",
    )
    p_proc.add_argument(
        "--finalizar",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="após --peticionar, sobe o recibo no bucket Jurify e marca o CodItem "
             "como concluído no painel (default: True). Sem --peticionar, é no-op.",
    )
    p_proc.add_argument("--cliente", default=None)
    p_proc.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    p_proc.add_argument(
        "--parallel-tribunais",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="executa os tribunais em processos paralelos (cada tribunal num "
             "Python independente). Reduz tempo total quando há vários tribunais "
             "no batch. Custo: mais memória (1 Playwright por processo).",
    )
    p_proc.add_argument(
        "--ignorar-filtro-migracao",
        action="store_true",
        help="pula o filtro 'já migrou pro eproc' (regex de início do CNJ + ano "
             "de cadastro) do listar_protocolos_aptos. Pra usar com CodItems "
             "confirmados via script de descoberta (ex.: consultar_migracao_mg.py). "
             "Mantém todos os outros checks (status, tipo, arquivos).",
    )
    p_proc.set_defaults(func=cmd_processar)

    p_fin = sub.add_parser(
        "finalizar",
        help="standalone Fase 3: sobe recibo pro painel Jurify e marca CodItem como concluído",
    )
    p_fin.add_argument("cod_items", nargs="+", type=int, help="CodItems a finalizar")
    p_fin.add_argument(
        "--recibo",
        default=None,
        help="path do recibo PDF (override). Sem isso, usa ./<CNJ>/recibo<CNJ>.pdf",
    )
    p_fin.set_defaults(func=cmd_finalizar)

    p_baixar = sub.add_parser(
        "baixar-aptos",
        help="lista aptos, cria ./<CNJ>/ por processo e baixa os arquivos do S3",
    )
    p_baixar.add_argument("--desde", default=_default_desde())
    p_baixar.add_argument("--cod-item", type=int, default=None)
    p_baixar.add_argument("--limit", type=int, default=None)
    p_baixar.add_argument(
        "--force",
        action="store_true",
        help="re-baixa mesmo se o arquivo local já existir (default: pula)",
    )
    p_baixar.set_defaults(func=cmd_baixar_aptos)

    p_list = sub.add_parser("listar", help="lista clientes do clientes.json")
    p_list.set_defaults(func=cmd_listar)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
