"""Leitura de PDFs e extração de números CNJ.

PDFs escaneados (sem text-layer útil) são tratados via fallback OCR
(`pypdfium2` renderiza página → `pytesseract` faz OCR em pt). O fallback é
ativado quando o text-layer extraído pelo `pypdf` é muito curto.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

import pypdf

_log = logging.getLogger("rpa.pdf")

# CNJ pode aparecer no PDF de duas formas:
#   - sem máscara: 20 dígitos consecutivos
#   - com máscara: NNNNNNN-DD.AAAA.J.TR.OOOO  (7-2.4.1.2.4 = 20 dígitos)
_CNJ_SEM_MASCARA = re.compile(r"(?<!\d)(\d{20})(?!\d)")
_CNJ_COM_MASCARA = re.compile(
    r"(?<!\d)(\d{7})[-.](\d{2})[.](\d{4})[.](\d{1})[.](\d{2})[.](\d{4})(?!\d)"
)

# Versões "largas" usadas só no modo `tolerar_erro_material`:
# aceitam 6-8 dígitos no 1º grupo e 19-21 dígitos no formato plano.
# Usado pra encontrar candidatos com 1 char a mais/menos.
_CNJ_SEM_MASCARA_LARGO = re.compile(r"(?<!\d)(\d{19,21})(?!\d)")
_CNJ_COM_MASCARA_LARGO = re.compile(
    r"(?<!\d)(\d{6,8})[-.](\d{2})[.](\d{4})[.](\d{1})[.](\d{2})[.](\d{4})(?!\d)"
)


_LIMIAR_OCR = 50  # se text-layer < N chars, presume escaneado e roda OCR


def _ler_texto_pypdf(arquivo: Path) -> str:
    """Texto extraído do text-layer do PDF (rápido, mas falha em escaneados)."""
    with arquivo.open("rb") as f:
        reader = pypdf.PdfReader(f)
        partes: list[str] = []
        for page in reader.pages:
            try:
                partes.append(page.extract_text() or "")
            except Exception:
                continue
    return "\n".join(partes)


def _ler_texto_ocr(arquivo: Path, lang: str = "por+eng", escala: float = 2.0) -> str:
    """Renderiza cada página em bitmap e roda Tesseract OCR. Custoso — só é
    chamado quando o text-layer falha.
    """
    try:
        import pypdfium2 as pdfium
        import pytesseract
    except ImportError:
        _log.warning("OCR indisponível (instale pypdfium2 + pytesseract); pulando")
        return ""

    _log.info("PDF parece escaneado — fazendo OCR (%s) em '%s'", lang, arquivo.name)
    pdf = pdfium.PdfDocument(str(arquivo))
    try:
        partes: list[str] = []
        for i in range(len(pdf)):
            page = pdf[i]
            bitmap = page.render(scale=escala)
            pil_img = bitmap.to_pil()
            try:
                txt = pytesseract.image_to_string(pil_img, lang=lang)
            except pytesseract.TesseractError:
                # fallback: tenta só eng se por não estiver disponível
                txt = pytesseract.image_to_string(pil_img, lang="eng")
            partes.append(txt or "")
            page.close()
        return "\n".join(partes)
    finally:
        pdf.close()


@lru_cache(maxsize=64)
def _ler_texto_cache(arquivo_str: str) -> str:
    """Wrapper cacheado: text-layer + fallback OCR. A chave é o path string
    (lru_cache não aceita Path)."""
    arquivo = Path(arquivo_str)
    texto = _ler_texto_pypdf(arquivo)
    if len(texto.strip()) < _LIMIAR_OCR:
        ocr = _ler_texto_ocr(arquivo)
        if ocr.strip():
            return ocr
    return texto


def ler_texto(arquivo: Path) -> str:
    """Devolve o texto do PDF, com fallback OCR transparente quando necessário."""
    if not arquivo.exists():
        raise FileNotFoundError(arquivo)
    return _ler_texto_cache(str(arquivo.resolve()))


def extrair_cnjs(arquivo: Path) -> set[str]:
    """Retorna o conjunto de números CNJ (20 dígitos, sem máscara) encontrados no PDF.

    Robusto contra PDFs com espaços invisíveis ou separadores estranhos:
    além de procurar pela máscara padrão, normaliza tudo pra digits-only e
    detecta a sequência por janela deslizante.
    """
    texto = ler_texto(arquivo)
    achados: set[str] = set()
    for m in _CNJ_SEM_MASCARA.finditer(texto):
        achados.add(m.group(1))
    for m in _CNJ_COM_MASCARA.finditer(texto):
        achados.add("".join(m.groups()))
    return achados


def _indel_distance_ate_1(a: str, b: str) -> bool:
    """True se `a == b` ou se podem se tornar iguais com 1 inserção ou 1 remoção.

    Importante: NÃO aceita substituição (1 char trocado por outro). Isso é o que
    diferencia "mero erro material de digitação" (faltou ou sobrou um dígito) de
    "CNJ diverso" (dígito trocado, comarca diferente, etc.).
    """
    if a == b:
        return True
    if abs(len(a) - len(b)) != 1:
        return False
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    # remove 1 char de `longer` em cada posição e confere com `shorter`
    for i in range(len(longer)):
        if longer[:i] + longer[i + 1:] == shorter:
            return True
    return False


def cnj_no_pdf(arquivo: Path, numero: str, *, tolerar_erro_material: bool = False) -> bool:
    """True se o CNJ (20 dígitos) aparece no PDF.

    Tolera formatação intermediária (espaços, hífens, etc.) — busca em digits-only
    do texto extraído.

    Com `tolerar_erro_material=True`, aceita também CNJs no PDF que diferem do
    alvo por exatamente 1 char inserido ou removido (mero erro material —
    digitação a menos ou a mais). NUNCA aceita substituições. Use só em revisão
    explícita, item por item.
    """
    numero_alvo = re.sub(r"\D", "", numero)
    if not numero_alvo:
        return False
    cnjs = extrair_cnjs(arquivo)
    if numero_alvo in cnjs:
        return True
    # Sliding match em digits-only do texto inteiro
    texto = ler_texto(arquivo)
    digits_only = re.sub(r"\D", "", texto)
    if numero_alvo in digits_only:
        return True

    if tolerar_erro_material:
        # Tenta CNJs com 1 char a mais/menos via regexes largas.
        candidatos: set[str] = set()
        for m in _CNJ_SEM_MASCARA_LARGO.finditer(texto):
            candidatos.add(m.group(1))
        for m in _CNJ_COM_MASCARA_LARGO.finditer(texto):
            candidatos.add("".join(m.groups()))
        for c in candidatos:
            if _indel_distance_ate_1(numero_alvo, c):
                _log.warning(
                    "tolerância aplicada: CNJ alvo %s aceito por diferença de 1 char com %s no PDF (mero erro material)",
                    numero_alvo, c,
                )
                return True
    return False


# ----------------------------------------------------------------------------
# Identificação da petição principal entre vários PDFs (multi-doc)
# ----------------------------------------------------------------------------
# Marcadores típicos da peça principal (vs. anexos como cálculos/guias/comprovantes).
_RE_ENDERECAMENTO = re.compile(
    r"\b(?:EXCELENT[IÍ]SSIMO|EX(?:CELENT[IÍ]SSIM[OA])?|EX\.?M[OA]\.?|MM\.?|MERIT[ÍI]SSIM[OA])"
    r"[^\n]{0,120}\b(?:JUIZ|JU[IÍ]ZA|DOUTOR|DR\.?|DRA\.?)\b",
    re.IGNORECASE,
)
_RE_OAB = re.compile(r"\bOAB\s*[/\-]?\s*[A-Z]{2}\s*[\d.\-/]+", re.IGNORECASE)
_RE_DEFERIMENTO = re.compile(
    r"\b(nestes\s+termos|pede\s+deferimento|requer\s+(o\s+)?deferimento|"
    r"termos\s+em\s+que\s+pede|p\.?\s*deferimento)",
    re.IGNORECASE,
)


def _score_peticao(texto: str, numero_cnj_digits: str) -> dict[str, int]:
    """Pontua um texto com marcadores típicos da peça principal. Retorna o
    breakdown (útil pra logar) — a chave 'total' tem a soma.
    """
    score = {"enderecamento": 0, "cnj": 0, "oab": 0, "deferimento": 0, "total": 0}
    if _RE_ENDERECAMENTO.search(texto):
        score["enderecamento"] = 30
    if numero_cnj_digits and numero_cnj_digits in re.sub(r"\D", "", texto):
        score["cnj"] = 25
    if _RE_OAB.search(texto):
        score["oab"] = 20
    if _RE_DEFERIMENTO.search(texto):
        score["deferimento"] = 10
    score["total"] = sum(v for k, v in score.items() if k != "total")
    return score


def identificar_peticao_principal(
    arquivos: list[Path], numero_cnj: str
) -> tuple[Path, dict[Path, dict[str, int]]]:
    """De uma lista de PDFs, devolve qual é a peça principal e o breakdown
    de pontuação de cada um. Critério: maior `total`; empate desempata por
    tamanho do arquivo (peça principal tende a ter mais texto).
    """
    if not arquivos:
        raise ValueError("lista vazia")
    digits = re.sub(r"\D", "", numero_cnj or "")
    breakdown: dict[Path, dict[str, int]] = {}
    for p in arquivos:
        try:
            texto = ler_texto(p)
        except Exception:
            breakdown[p] = {"enderecamento": 0, "cnj": 0, "oab": 0, "deferimento": 0, "total": 0}
            continue
        breakdown[p] = _score_peticao(texto, digits)
    principal = max(arquivos, key=lambda p: (breakdown[p]["total"], p.stat().st_size))
    return principal, breakdown
