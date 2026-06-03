"""Otimização de PDFs grandes pra caber no limite do eproc.

Pipeline em 3 níveis (aplicados em ordem, parando assim que couber):
  1. Compressão Ghostscript com perfil /ebook (~150 DPI, perda mínima)
  2. Compressão Ghostscript com perfil /screen (~72 DPI, perda maior mas
     ainda legível pra documentos jurídicos)
  3. Divisão adaptativa em partes ≤ limite, mantendo ordem das páginas

Se mesmo após o split alguma parte estiver acima do limite (página única
gigantesca, por exemplo), levanta `PdfMuitoGrandeError` pra abortar o item.

Pré-requisito: binário `gs` (Ghostscript) no PATH. Em macOS: `brew install ghostscript`.
"""
from __future__ import annotations

import logging
import math
import shutil
import subprocess
from pathlib import Path

import pypdf

_log = logging.getLogger("rpa.pdf_optimize")

LIMITE_MB_DEFAULT = 10  # eproc-RS rejeitou um 14MB; 10MB é margem segura


class PdfMuitoGrandeError(RuntimeError):
    """PDF não cabe no limite mesmo após todas as estratégias de otimização."""


class GhostscriptIndisponivelError(RuntimeError):
    """gs não está no PATH — instale com `brew install ghostscript`."""


def _tamanho_mb(path: Path) -> float:
    return path.stat().st_size / 1_000_000


def _gs_disponivel() -> bool:
    return shutil.which("gs") is not None


def _comprimir_gs(entrada: Path, saida: Path, preset: str) -> bool:
    """Roda Ghostscript com um perfil. Retorna True se gerou saída válida."""
    cmd = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS=/{preset}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        f"-sOutputFile={saida}",
        str(entrada),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    except subprocess.CalledProcessError as e:
        _log.error("gs falhou (%s): %s", preset, e.stderr.decode("utf-8", "replace")[:200])
        return False
    except subprocess.TimeoutExpired:
        _log.error("gs (%s) timeout em '%s'", preset, entrada.name)
        return False
    return saida.exists() and saida.stat().st_size > 0


def _dividir_pdf(
    entrada: Path,
    limite_bytes: int,
    pasta_saida: Path,
    stem_destino: str | None = None,
) -> list[Path]:
    """Divide o PDF em partes que caibam no limite, mantendo ordem das páginas.

    `stem_destino` é o nome base das partes (sem `.pdf`). Se omitido, usa o
    nome do arquivo de entrada — útil pra ignorar nomes de temporários quando
    chamado depois de comprimir.

    Estratégia adaptativa: estima páginas por parte pela razão tamanho/limite;
    se uma parte ficar acima, reduz pela metade e regenera. Falha se alguma
    página sozinha for maior que o limite (não há o que cortar).
    """
    reader = pypdf.PdfReader(str(entrada))
    total = len(reader.pages)
    if total <= 1:
        raise PdfMuitoGrandeError(
            f"'{entrada.name}' tem 1 página única acima do limite — não dá pra dividir."
        )

    stem = stem_destino or entrada.stem

    # Estimativa inicial pelo tamanho
    razao = math.ceil(entrada.stat().st_size / limite_bytes)
    paginas_por_parte = max(1, total // razao)

    partes: list[Path] = []
    pasta_saida.mkdir(parents=True, exist_ok=True)
    inicio = 0
    parte_num = 1
    while inicio < total:
        fim = min(inicio + paginas_por_parte, total)
        writer = pypdf.PdfWriter()
        for p in range(inicio, fim):
            writer.add_page(reader.pages[p])
        out = pasta_saida / f"{stem}_p{parte_num}.pdf"
        with out.open("wb") as f:
            writer.write(f)

        if out.stat().st_size > limite_bytes:
            # Parte ainda grande — ou reduz, ou aborta se já é página única.
            if fim - inicio == 1:
                # Página única + ainda grande
                out.unlink(missing_ok=True)
                for p in partes:
                    p.unlink(missing_ok=True)
                raise PdfMuitoGrandeError(
                    f"'{entrada.name}': página {inicio + 1} sozinha ({_tamanho_mb(out):.1f}MB) "
                    f"excede {limite_bytes / 1_000_000:.1f}MB."
                )
            out.unlink(missing_ok=True)
            paginas_por_parte = max(1, paginas_por_parte // 2)
            continue  # mesma `inicio`, paginas_por_parte menor

        partes.append(out)
        inicio = fim
        parte_num += 1

    return partes


def otimizar_para_eproc(
    arquivo: Path,
    limite_mb: int = LIMITE_MB_DEFAULT,
) -> list[Path]:
    """Devolve uma lista de paths que caibam todos no limite.

    - Se o arquivo já cabe: `[arquivo]` (no-op)
    - Caso contrário, tenta `/ebook`, depois `/screen`, depois split adaptativo.
    - Em caso de sucesso, **substitui o arquivo original** pela versão final
      (compressão) ou pelas partes (split). O `recibo*` é gerado depois e fica
      separado, não tem risco de mistura.

    Levanta `PdfMuitoGrandeError` se mesmo após todas as estratégias alguma
    parte estiver acima do limite. Levanta `GhostscriptIndisponivelError` se
    `gs` não estiver no PATH.
    """
    if not _gs_disponivel():
        raise GhostscriptIndisponivelError(
            "Binário `gs` não encontrado no PATH. Instale com `brew install ghostscript`."
        )

    arquivo = Path(arquivo).resolve()
    tamanho = _tamanho_mb(arquivo)
    if tamanho <= limite_mb:
        return [arquivo]

    limite_bytes = limite_mb * 1_000_000
    _log.info("PDF acima do limite (%s = %.1fMB > %dMB) — otimizando",
              arquivo.name, tamanho, limite_mb)

    # --- Estratégia 1: gs /ebook (~150 DPI) ---
    tmp_ebook = arquivo.with_name(f"{arquivo.stem}.gs-ebook.tmp.pdf")
    if _comprimir_gs(arquivo, tmp_ebook, "ebook"):
        tam_ebook = _tamanho_mb(tmp_ebook)
        _log.info("  /ebook: %.1fMB", tam_ebook)
        if tam_ebook <= limite_mb:
            tmp_ebook.replace(arquivo)
            return [arquivo]
    else:
        tmp_ebook.unlink(missing_ok=True)
        tmp_ebook = None

    # --- Estratégia 2: gs /screen (~72 DPI, mais agressivo) ---
    tmp_screen = arquivo.with_name(f"{arquivo.stem}.gs-screen.tmp.pdf")
    if _comprimir_gs(arquivo, tmp_screen, "screen"):
        tam_screen = _tamanho_mb(tmp_screen)
        _log.info("  /screen: %.1fMB", tam_screen)
        if tam_screen <= limite_mb:
            if tmp_ebook and tmp_ebook.exists():
                tmp_ebook.unlink()
            tmp_screen.replace(arquivo)
            return [arquivo]
    else:
        tmp_screen.unlink(missing_ok=True)
        tmp_screen = None

    # --- Estratégia 3: split do /screen comprimido em partes ---
    # Se /screen falhou, usamos o /ebook como base; se ambos falharam, o original.
    base = arquivo
    if tmp_screen and tmp_screen.exists():
        base = tmp_screen
    elif tmp_ebook and tmp_ebook.exists():
        base = tmp_ebook

    _log.info("  comprimido ainda > limite — dividindo '%s' (%.1fMB)",
              base.name, _tamanho_mb(base))
    # `stem_destino` = nome do arquivo original (sem _gs-ebook.tmp / _gs-screen.tmp).
    partes = _dividir_pdf(base, limite_bytes, arquivo.parent, stem_destino=arquivo.stem)

    # Cleanup do original e temporários
    if tmp_ebook and tmp_ebook.exists():
        tmp_ebook.unlink()
    if tmp_screen and tmp_screen.exists() and tmp_screen != base:
        tmp_screen.unlink()
    if arquivo.exists():
        arquivo.unlink()

    _log.info("  dividido em %d parte(s): %s", len(partes), [p.name for p in partes])
    return partes
