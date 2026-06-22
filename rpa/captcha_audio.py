"""Resolve o áudio-captcha do eproc (InfraCaptcha) via speech-to-text.

O eproc-MG passou a exigir captcha no login (a partir de jun/2026). A imagem é
ruidosa demais pra OCR confiável, mas o eproc oferece um áudio que narra o código
caractere a caractere. Este módulo baixa esse WAV e transcreve com faster-whisper.

Detalhe-chave: o áudio narra as letras pelos NOMES em português ("ême"=M,
"ípsilon"=Y, "ésse"=S...), então a transcrição bruta do whisper precisa passar
por um mapa nome-da-letra → caractere. Dígitos também vêm por extenso.

A acurácia por tentativa não é 100% (o whisper embola alguns áudios), mas o
chamador roda num retry-loop: cada submit errado regenera o captcha, então 2-3
tentativas resolvem na prática.

Uso:
    from rpa.captcha_audio import transcrever_codigo
    codigo = transcrever_codigo(wav_bytes)   # -> 'ZEEZ' ou '' se não decodificou
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from pathlib import Path

_log = logging.getLogger("rpa.captcha_audio")

# Nome da letra em PT (+ manglings típicos do whisper) → caractere.
_NOMES: dict[str, str] = {
    "a": "A", "á": "A",
    "be": "B", "bê": "B",
    "ce": "C", "cê": "C", "sê": "C",
    "de": "D", "dê": "D",
    "e": "E", "é": "E",
    "efe": "F", "éfe": "F", "efi": "F",
    "ge": "G", "gê": "G", "jê": "G",
    "aga": "H", "agá": "H",
    "i": "I",
    "jota": "J",
    "ka": "K", "cá": "K", "ká": "K", "kappa": "K",
    "ele": "L", "éle": "L", "eli": "L",
    "eme": "M", "ême": "M", "emi": "M",
    "ene": "N", "êne": "N", "eni": "N",
    "o": "O", "ó": "O",
    "pe": "P", "pê": "P",
    "que": "Q", "quê": "Q",
    "erre": "R", "érre": "R", "erri": "R",
    "esse": "S", "ésse": "S", "essi": "S",
    "te": "T", "tê": "T",
    "u": "U",
    "ve": "V", "vê": "V",
    "dablio": "W", "dáblio": "W",
    "xis": "X", "chis": "X",
    "ipsilon": "Y", "ípsilon": "Y", "ipsu": "Y", "ipisilon": "Y",
    "ze": "Z", "zê": "Z", "zi": "Z",
    "zero": "0", "um": "1", "dois": "2", "tres": "3", "três": "3",
    "quatro": "4", "cinco": "5", "seis": "6", "sete": "7", "oito": "8", "nove": "9",
}

_MODEL = None
_MODEL_LOCK = threading.Lock()


def _modelo():
    """Carrega o faster-whisper UMA vez (lazy singleton, thread-safe).
    Tamanho configurável via RPA_WHISPER_MODEL (default 'small')."""
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                from faster_whisper import WhisperModel
                nome = os.getenv("RPA_WHISPER_MODEL", "small")
                _log.info("carregando faster-whisper (%s)…", nome)
                _MODEL = WhisperModel(nome, device="cpu", compute_type="int8")
    return _MODEL


def normalizar(transcricao: str) -> str:
    """Converte a transcrição bruta do whisper no código (letras/dígitos)."""
    out: list[str] = []
    for tok in re.split(r"[\s,.\-]+", transcricao.lower()):
        tok = tok.strip(" .,")
        if not tok:
            continue
        if tok in _NOMES:
            out.append(_NOMES[tok])
        elif len(tok) == 1 and tok.isalnum():
            out.append(tok.upper())
    return "".join(out)


def transcrever_codigo(wav_bytes: bytes) -> str:
    """Recebe o WAV do áudio-captcha e devolve o código normalizado.

    Retorna '' se o STT não estiver disponível ou não decodificar nada.
    """
    try:
        modelo = _modelo()
    except Exception as e:  # faster-whisper ausente, etc.
        _log.warning("STT indisponível (instale faster-whisper): %s", e)
        return ""

    tmp = Path(tempfile.gettempdir()) / f"captcha_{threading.get_ident()}.wav"
    tmp.write_bytes(wav_bytes)
    try:
        segs, _ = modelo.transcribe(
            str(tmp), language="pt",
            initial_prompt="código: letras e números narrados um a um",
        )
        bruto = "".join(s.text for s in segs)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    cod = normalizar(bruto)
    _log.info("captcha áudio: bruto=%r → código=%r", bruto.strip(), cod)
    return cod
