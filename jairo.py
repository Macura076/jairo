"""
JAIRO v2.0 (revisao 2)
==============================================================
Assistente virtual por comando de voz integrado ao Windows.

Mudancas arquiteturais em relacao a v1.5:
  - WebDriver isolado em WhatsAppService com worker thread unica.
    Nenhuma outra thread toca o driver diretamente.
  - Agenda com busca exata e validacao de numero na inicializacao.
  - Timeout global compartilhado entre todos os seletores CSS/XPath.
  - Confirmacao de envio via contagem de bolhas .message-out.
  - Auto-responder com fingerprint SHA-256 por mensagem, nao por pessoa.
  - WebElements nunca guardados entre aquisicoes de lock.
  - Backoff exponencial com jitter para retentativas.
  - Logging estruturado com RotatingFileHandler e mascaramento de numero.
  - Threads iniciadas exclusivamente dentro de main().
  - subprocess.Popen sem shell=True.
  - Configuracao via .env com python-dotenv.
  - Injecao JS via argumento do execute_script, sem f-string.

Revisao 2:
  - Palavra de ativacao obrigatoria: todo comando deve comecar com
    "jairo" (aliases em JAIRO_WAKE_ALIASES; desativavel com
    JAIRO_EXIGIR_WAKE=false).
  - Controle de midia e volume (teclas de midia; pycaw opcional para
    volume absoluto).
  - Leitura por voz das conversas nao lidas, sem abrir os chats.
  - "verificar mensagens" agora dispara de fato a varredura do
    auto-responder, via fila do worker (sem tocar o driver fora dele).
  - "cancelar desligamento" executa shutdown /a.
  - TTS: motor recriado a cada fala, deteccao de fala silenciosa,
    fallback automatico para PowerShell/SAPI e modo de diagnostico
    (python jairo.py --testar-voz).
  - O microfone descarta audio capturado enquanto o Jairo fala.

Revisao 3:
  - Leitura de nao lidas reescrita: coleta via JavaScript tolerante a
    mudancas de DOM do WhatsApp Web (fallback para Selenium) e comando
    "diagnosticar whatsapp" que grava wpp_diagnostico.json.
  - Captura de tela (Win+Shift+S) e bloqueio do PC (LockWorkStation).

Revisao 4 (latencia e usabilidade):
  - Acoes instantaneas (print, bloqueio, janelas, midia, volume) executam
    antes de qualquer fala; feedback por bipes.
  - Microfone aberto continuamente (pause_threshold padrao 0.8 s, JAIRO_PAUSE).
  - Envio de WhatsApp sem sleeps fixos (cabecalho confirmado por polling).
  - Novos: timers, janelas, print de tela inteira, data, repetir, ajuda,
    modo silencioso, atalhos para projetos/downloads/configuracoes.

Revisao 5:
  - Microfone: limite minimo de energia 300 -> 120 (JAIRO_ENERGY_MIN), ganho
    automatico para voz baixa (JAIRO_GANHO_MAX), "recalibrar microfone" e
    diagnostico python jairo.py --testar-mic (sugere JAIRO_ENERGY).
  - Calculadora por voz: "quanto e 15 + 14" responde apenas "29".
  - Agenda: apelidos (Nome:numero:apelido), contatos inativos (!Nome:numero),
    nome ambiguo pede o nome completo, "buscar contato X" lista os cadastrados.
==============================================================
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import webbrowser
from array import array
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic

import pyttsx3
import speech_recognition as sr
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    UnexpectedAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    import winsound  # bipes de feedback (somente Windows)
except ImportError:  # pragma: no cover
    winsound = None

try:
    import audioop  # ganho automatico (dependencia do speech_recognition)
except ImportError:  # pragma: no cover
    audioop = None


# ==============================================================
#  LOGGING
# ==============================================================

def _configurar_logging() -> logging.Logger:
    logger = logging.getLogger("jairo")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(threadName)-22s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    arquivo = RotatingFileHandler(
        "jairo.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    arquivo.setLevel(logging.DEBUG)
    arquivo.setFormatter(fmt)

    logger.addHandler(console)
    logger.addHandler(arquivo)
    return logger


logger = _configurar_logging()


def _mascarar_numero(numero: str) -> str:
    if len(numero) <= 4:
        return "*" * len(numero)
    return f"{'*' * (len(numero) - 4)}{numero[-4:]}"


# ==============================================================
#  CONFIGURACAO (.env)
# ==============================================================

load_dotenv()


@dataclass(frozen=True)
class Config:
    # Paths
    caminho_projeto: Path = field(
        default_factory=lambda: Path(os.getenv("JAIRO_DIR", r"C:\09projec"))
    )
    caminho_brave: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "BRAVE_PATH",
                r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            )
        )
    )

    # Comportamento
    modo_debug: bool = field(
        default_factory=lambda: os.getenv("JAIRO_DEBUG", "false").lower() == "true"
    )
    page_load_timeout: int = field(
        default_factory=lambda: int(os.getenv("PAGE_LOAD_TIMEOUT", "30"))
    )
    element_timeout: int = field(
        default_factory=lambda: int(os.getenv("ELEMENT_TIMEOUT", "20"))
    )
    intervalo_auto_responder: int = field(
        default_factory=lambda: int(os.getenv("AUTO_RESPONDER_INTERVAL", "45"))
    )
    intervalo_minimo_envio: float = field(
        default_factory=lambda: float(os.getenv("MIN_SEND_INTERVAL", "3.0"))
    )
    capacidade_fila_wpp: int = field(
        default_factory=lambda: int(os.getenv("WPP_QUEUE_SIZE", "50"))
    )

    # Palavra de ativacao
    exigir_wake_word: bool = field(
        default_factory=lambda: os.getenv("JAIRO_EXIGIR_WAKE", "true").lower() == "true"
    )
    wake_aliases: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            a.strip().lower()
            for a in os.getenv("JAIRO_WAKE_ALIASES", "jairo,jairó,jairu,jayro").split(",")
            if a.strip()
        )
        or ("jairo",)
    )

    # Voz: auto | pyttsx3 | powershell
    tts_backend: str = field(
        default_factory=lambda: os.getenv("JAIRO_TTS", "auto").strip().lower()
    )

    # Leitura de nao lidas: maximo de conversas lidas em voz alta
    max_chats_leitura: int = field(
        default_factory=lambda: int(os.getenv("JAIRO_MAX_CHATS_LEITURA", "5"))
    )

    # Latencia de escuta
    pause_threshold: float = field(
        default_factory=lambda: float(os.getenv("JAIRO_PAUSE", "0.8"))
    )
    phrase_time_limit: int = field(
        default_factory=lambda: int(os.getenv("JAIRO_PHRASE_LIMIT", "6"))
    )
    calibracao_seg: float = field(
        default_factory=lambda: float(os.getenv("JAIRO_CALIBRACAO", "1.5"))
    )

    # Sensibilidade do microfone (RMS int16; use --testar-mic para medir)
    energy_min: int = field(
        default_factory=lambda: int(os.getenv("JAIRO_ENERGY_MIN", "120"))
    )
    energy_max: int = field(
        default_factory=lambda: int(os.getenv("JAIRO_ENERGY_MAX", "900"))
    )
    energy_fixo: int = field(  # > 0: ignora a calibracao e usa este valor
        default_factory=lambda: int(os.getenv("JAIRO_ENERGY", "0"))
    )
    ganho_max: float = field(  # 1.0 desativa o ganho automatico
        default_factory=lambda: float(os.getenv("JAIRO_GANHO_MAX", "4.0"))
    )
    alvo_rms: int = field(
        default_factory=lambda: int(os.getenv("JAIRO_ALVO_RMS", "2500"))
    )

    # Modo silencioso: confirmacoes viram bipes (respostas importantes continuam faladas)
    modo_silencioso: bool = field(
        default_factory=lambda: os.getenv("JAIRO_SILENCIOSO", "false").lower() == "true"
    )

    @property
    def caminho_perfil(self) -> Path:
        return self.caminho_projeto / "perfil_jairo"

    @property
    def caminho_auth(self) -> Path:
        return self.caminho_projeto / "autenticado.txt"

    @property
    def caminho_contatos(self) -> Path:
        return self.caminho_projeto / "contatos.txt"

    @property
    def caminho_permitidos(self) -> Path:
        return self.caminho_projeto / "permitidos.txt"


CFG = Config()


# ==============================================================
#  UTILITARIOS DE TEXTO
# ==============================================================

_PHONE_RE = re.compile(r"^[1-9]\d{9,14}$")


def _normalizar_nome(nome: str) -> str:
    """Normaliza unicode, colapsa espacos, aplica casefold."""
    nome = unicodedata.normalize("NFKC", nome)
    return " ".join(nome.strip().casefold().split())


def _sem_acentos(texto: str) -> str:
    """Remove acentos, colapsa espacos e aplica casefold. Usado so para casar gatilhos."""
    decomposto = unicodedata.normalize("NFKD", texto)
    sem = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem.casefold().split())


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _limpar_para_fala(texto: str, limite: int = 120) -> str:
    """Remove emojis/simbolos e limita o tamanho para leitura por TTS."""
    texto = re.sub(r"\s+", " ", texto).strip()
    texto = re.sub(r"[^\w\s.,!?:;()@%/'\"-]", "", texto)
    return texto[:limite].strip()


# ==============================================================
#  PALAVRA DE ATIVACAO
# ==============================================================

_WAKE_RE = re.compile(
    r"^\s*(?:(?:ei|oi|ol[aá]|ok|opa|fala|e\s+a[ií])[\s,]+)?"
    r"(?:" + "|".join(re.escape(a) for a in CFG.wake_aliases) + r")\b"
    r"[\s,.:;!?-]*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _extrair_comando(texto: str) -> str | None:
    """
    Remove a palavra de ativacao do inicio e retorna o restante do comando.
    Retorna None se a palavra de ativacao for exigida e nao estiver presente.
    Retorna string vazia se o usuario disse apenas "jairo".
    """
    texto = texto.strip()
    achado = _WAKE_RE.match(texto)
    if achado:
        return achado.group(1).strip()
    return None if CFG.exigir_wake_word else texto


# ==============================================================
#  DOMINIO: CONTATOS
# ==============================================================

@dataclass(frozen=True)
class Contato:
    nome: str
    numero: str
    aliases: tuple[str, ...] = ()


class ContatoAmbiguo(LookupError):
    """Mais de um contato ativo corresponde ao nome falado."""

    def __init__(self, nome: str, candidatos: list[Contato]) -> None:
        self.candidatos = candidatos
        super().__init__(
            f"Nome ambiguo {nome!r}: " + ", ".join(c.nome for c in candidatos)
        )


class Agenda:
    """
    Carrega contatos.txt na inicializacao e valida todos os numeros.

    Formato do arquivo:
        # comentario
        Nome Completo:5511999998888
        Nome Completo:5511999998888:apelido1,apelido2
        !Nome Antigo:5511999990000          (prefixo ! desativa o contato)

    Resolucao do nome falado (sem acentos, sem diferenciar maiusculas):
      1. Apelido exato: prioridade maxima, e a escolha explicita do usuario.
      2. Todos os termos falados presentes no nome do contato. Se mais de um
         contato ativo casar, levanta ContatoAmbiguo em vez de adivinhar.
    """

    def __init__(self, caminho: Path) -> None:
        self._contatos: dict[str, Contato] = {}
        self._aliases: dict[str, Contato] = {}
        self._permitidos_cache: frozenset[str] = frozenset()
        self._permitidos_mtime: float = -1.0
        self._carregar(caminho)

    def _carregar(self, caminho: Path) -> None:
        if not caminho.exists():
            caminho.parent.mkdir(parents=True, exist_ok=True)
            caminho.write_text(
                "# Formato: Nome:5511999998888[:apelido1,apelido2]  "
                "(DDI+DDD+numero, sem + ou espacos)\n"
                "# Prefixo ! desativa um contato antigo: !Fulano Antigo:5511999990000\n",
                encoding="utf-8",
            )
            logger.warning("Arquivo de contatos criado vazio: %s", caminho)
            return

        vistos: set[str] = set()
        inativos = 0

        for numero_linha, linha in enumerate(
            caminho.read_text(encoding="utf-8").splitlines(), start=1
        ):
            linha = linha.strip()
            if not linha or linha.startswith("#"):
                continue

            ativo = not linha.startswith("!")
            if not ativo:
                linha = linha[1:].strip()

            partes = [p.strip() for p in linha.split(":")]
            if len(partes) not in (2, 3) or not partes[0]:
                raise ValueError(
                    f"contatos.txt linha {numero_linha} invalida: {linha!r}"
                )
            nome_raw, numero = partes[0], partes[1]
            aliases = (
                tuple(a for a in (x.strip() for x in partes[2].split(",")) if a)
                if len(partes) == 3
                else ()
            )
            chave = _normalizar_nome(nome_raw)

            if not _PHONE_RE.fullmatch(numero):
                raise ValueError(
                    f"contatos.txt linha {numero_linha}: numero invalido {numero!r}"
                )
            if chave in vistos:
                raise ValueError(
                    f"contatos.txt linha {numero_linha}: contato duplicado {nome_raw!r}"
                )
            vistos.add(chave)

            if not ativo:
                inativos += 1
                logger.info("Contato inativo ignorado: %s", nome_raw)
                continue

            contato = Contato(nome=nome_raw, numero=numero, aliases=aliases)
            self._contatos[chave] = contato
            for alias in aliases:
                chave_alias = _sem_acentos(alias)
                existente = self._aliases.get(chave_alias)
                if existente is not None and existente is not contato:
                    raise ValueError(
                        f"contatos.txt linha {numero_linha}: apelido duplicado {alias!r}"
                    )
                self._aliases[chave_alias] = contato

        logger.info(
            "Agenda carregada: %d contato(s) ativo(s), %d inativo(s).",
            len(self._contatos),
            inativos,
        )

    def buscar_todos(self, nome: str) -> list[Contato]:
        """Contatos que casam com o nome falado (apelido tem prioridade)."""
        chave = _sem_acentos(nome)
        if not chave:
            return []
        if chave in self._aliases:
            return [self._aliases[chave]]
        termos = set(chave.split())
        return [
            c for c in self._contatos.values()
            if termos <= set(_sem_acentos(c.nome).split())
        ]

    def buscar(self, nome: str) -> Contato:
        """
        Levanta LookupError se nao encontrado e ContatoAmbiguo (subclasse)
        se mais de um contato ativo corresponder.
        """
        achados = self.buscar_todos(nome)
        if not achados:
            raise LookupError(f"Contato nao encontrado na agenda: {nome!r}")
        if len(achados) > 1:
            raise ContatoAmbiguo(nome, achados)
        return achados[0]

    def ler_permitidos(self) -> frozenset[str]:
        """Retorna chaves normalizadas do arquivo de permitidos (cache por mtime)."""
        caminho = CFG.caminho_permitidos
        if not caminho.exists():
            caminho.parent.mkdir(parents=True, exist_ok=True)
            caminho.write_text(
                "# Um nome por linha (exatamente como aparece no WhatsApp)\n",
                encoding="utf-8",
            )
            self._permitidos_cache = frozenset()
            self._permitidos_mtime = -1.0
            return self._permitidos_cache

        mtime = caminho.stat().st_mtime
        if mtime != self._permitidos_mtime:
            self._permitidos_cache = frozenset(
                _normalizar_nome(l)
                for l in caminho.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.startswith("#")
            )
            self._permitidos_mtime = mtime
        return self._permitidos_cache


# ==============================================================
#  DOMINIO: RESULTADOS
# ==============================================================

class EstadoEnvio(Enum):
    INSERIDO_NA_INTERFACE  = "inserido_na_interface"
    CONFIRMADO_NA_CONVERSA = "confirmado_na_conversa"
    FALHOU                 = "falhou"
    FILA_CHEIA             = "fila_cheia"


@dataclass(frozen=True)
class ResultadoEnvio:
    estado: EstadoEnvio
    destinatario: str
    detalhe: str = ""


@dataclass(frozen=True)
class ChatNaoLido:
    """Snapshot de uma conversa nao lida (somente strings, sem WebElements)."""
    nome: str
    previa: str
    horario: str
    quantidade: int
    fingerprint: str


@dataclass(frozen=True)
class ResultadoVarredura:
    """
    detalhe vazio = varredura executada.
    Valores possiveis quando nao executou: driver_inativo, sem_permitidos,
    erro_varredura.
    """
    respondidos: int
    nao_lidos: int
    detalhe: str = ""


# ==============================================================
#  EXCECOES DE DOMINIO
# ==============================================================

class DestinatarioInvalido(Exception):
    """Numero invalido ou contato nao encontrado. Nao retenta."""


class AutenticacaoNecessaria(Exception):
    """Sessao expirada. Nao retenta automaticamente."""


class MensagemVazia(ValueError):
    """Mensagem sem conteudo. Nao retenta."""


# ==============================================================
#  LIMITADOR DE FREQUENCIA
# ==============================================================

class LimitadorEnvio:
    """
    Garante intervalo minimo global entre envios consecutivos.
    Adiciona jitter aleatorio de ate 0.5s para comportamento menos previsivel.
    """

    def __init__(self, intervalo_global: float = CFG.intervalo_minimo_envio) -> None:
        self._intervalo = intervalo_global
        self._ultimo_envio: float = 0.0
        self._lock = threading.Lock()

    def aguardar(self) -> None:
        with self._lock:
            agora = monotonic()
            restante = self._intervalo - (agora - self._ultimo_envio)
            if restante > 0:
                time.sleep(restante + random.uniform(0.0, 0.5))
            self._ultimo_envio = monotonic()


# ==============================================================
#  SELETORES DO WHATSAPP WEB
# ==============================================================

# Ordenados por estabilidade historica: data-testid > role ARIA > estrutural > generico.
_CSS_COMPOSITOR: list[str] = [
    "div[data-testid='conversation-text-input']",
    "footer div[contenteditable='true'][role='textbox']",
    "footer div[contenteditable='true'][data-tab]",
    "div[contenteditable='true'][role='textbox']",
    "div[contenteditable='true'][data-tab='10']",
]

_XPATH_COMPOSITOR: list[str] = [
    "//div[@contenteditable='true' and @data-testid='conversation-text-input']",
    "//footer//div[@contenteditable='true' and @role='textbox']",
    "//footer//div[@contenteditable='true']",
]

_CSS_PAINEL_LATERAL: list[str] = [
    "div#pane-side",
    "div[data-testid='chatlist-panel']",
    "div[aria-label='Lista de conversas']",
]

_CSS_POPUP_FECHAR: list[str] = [
    "div[data-testid='popup-controls-ok']",
    "button[data-testid='confirm-popup-ok']",
    "div[role='button'][data-testid='btn-ok']",
]

# Indicador de nao lida dentro de um item da lista lateral.
_CSS_BADGE_NAO_LIDA: list[str] = [
    "span[data-testid='icon-unread-count']",
    "span[aria-label*='não lida']",
    "span[aria-label*='nao lida']",
    "span[aria-label*='unread']",
]

_CSS_PREVIA_CHAT = (
    "span[data-testid='last-msg-status'] + span, "
    "div[data-testid='last-message-preview']"
)

_CSS_BOLHA_SAIDA = "div.message-out"

_JS_NAVEGAR_CHAT = """
const numero = arguments[0];
const chatId = numero + '@c.us';
const store = window.Store;
if (!store || !store.Chat || !store.Cmd) { return false; }
const chat = store.Chat.get(chatId);
if (!chat) { return false; }
store.Cmd.openChatBottom(chat);
return true;
"""

# Utilitarios JS compartilhados: localizam os itens da lista lateral de forma
# tolerante a mudancas de DOM (listitem, row ou cell-frame-container).
_JS_ITENS_CHAT = r"""
function itensDaLista() {
  const painel = document.querySelector('#pane-side') || document;
  const itens = Array.from(painel.querySelectorAll(
    '[role="listitem"], [role="row"], [data-testid="cell-frame-container"]'
  ));
  return itens.filter(el => !itens.some(o => o !== el && el.contains(o)));
}
function nomeDoItem(item) {
  const t = item.querySelector('span[title]');
  if (t && t.getAttribute('title')) return t.getAttribute('title').trim();
  const s = item.querySelector('span[dir="auto"]');
  return s ? (s.textContent || '').trim() : '';
}
"""

_JS_COLETAR_NAO_LIDOS = _JS_ITENS_CHAT + r"""
const reRotulo = /n[aã]o\s*lidas?|unread/i;
const reHora = /^\d{1,2}:\d{2}$/;
const itens = itensDaLista();
const chats = [];
for (const item of itens) {
  let quantidade = 0;
  let achou = false;
  const candidatos = item.querySelectorAll(
    '[aria-label], [data-testid="icon-unread-count"]'
  );
  for (const el of candidatos) {
    const rotulo = el.getAttribute('aria-label') || '';
    const ehBadge = el.getAttribute('data-testid') === 'icon-unread-count';
    if (ehBadge || reRotulo.test(rotulo)) {
      achou = true;
      const m = (rotulo + ' ' + (el.textContent || '')).match(/\d+/);
      quantidade = m ? parseInt(m[0], 10) : 1;
      break;
    }
  }
  if (!achou) continue;

  const nome = nomeDoItem(item);
  if (!nome) continue;

  let previa = '';
  const ltr = item.querySelector('span[dir="ltr"]');
  if (ltr) previa = (ltr.textContent || '').trim();
  if (!previa || previa === nome) {
    const titulos = item.querySelectorAll('span[title]');
    previa = titulos.length > 1 ? (titulos[1].getAttribute('title') || '').trim() : '';
  }

  let horario = '';
  for (const el of item.querySelectorAll('div, span')) {
    const txt = (el.textContent || '').trim();
    if (el.children.length === 0 && reHora.test(txt)) { horario = txt; break; }
  }

  chats.push({nome: nome, previa: previa, horario: horario, quantidade: quantidade});
}
return {total: itens.length, chats: chats};
"""

_JS_ABRIR_CHAT_POR_NOME = _JS_ITENS_CHAT + r"""
const alvo = arguments[0];
for (const item of itensDaLista()) {
  if (nomeDoItem(item) !== alvo) continue;
  const el = item.querySelector('[data-testid="cell-frame-container"]') || item;
  ['mousedown', 'mouseup', 'click'].forEach(tipo =>
    el.dispatchEvent(new MouseEvent(tipo, {bubbles: true, cancelable: true, view: window}))
  );
  return true;
}
return false;
"""

_JS_DIAGNOSTICO = _JS_ITENS_CHAT + r"""
const seletores = [
  '#pane-side', '[role="listitem"]', '[role="row"]', '[role="grid"]',
  '[data-testid="cell-frame-container"]', '[data-testid="icon-unread-count"]',
  '[aria-label*="lida"]', '[aria-label*="unread"]', 'span[title]'
];
const contagens = {};
for (const s of seletores) contagens[s] = document.querySelectorAll(s).length;
const itens = itensDaLista();
return {
  url: location.href,
  contagens: contagens,
  total_itens: itens.length,
  amostra_html: itens.slice(0, 4).map(i => i.outerHTML.slice(0, 4000)),
  rotulos_aria: Array.from(document.querySelectorAll('#pane-side [aria-label]'))
    .slice(0, 40).map(e => e.getAttribute('aria-label'))
};
"""


# ==============================================================
#  UTILITARIOS SELENIUM
# ==============================================================

def _encontrar_elemento(
    driver: webdriver.Chrome,
    seletores_css: list[str],
    xpaths: list[str],
    timeout_total: float,
) -> webdriver.remote.webelement.WebElement:
    """
    Varre todos os seletores CSS e XPath dentro de um deadline global unico.
    Levanta TimeoutException se nenhum for encontrado no prazo.
    """
    deadline = monotonic() + timeout_total
    while monotonic() < deadline:
        for seletor in seletores_css:
            try:
                elementos = driver.find_elements(By.CSS_SELECTOR, seletor)
                for el in elementos:
                    try:
                        if el.is_displayed() and el.is_enabled():
                            logger.debug("Elemento localizado via CSS: %s", seletor)
                            return el
                    except StaleElementReferenceException:
                        continue
            except WebDriverException:
                continue

        for xpath in xpaths:
            try:
                elementos = driver.find_elements(By.XPATH, xpath)
                for el in elementos:
                    try:
                        if el.is_displayed() and el.is_enabled():
                            logger.debug("Elemento localizado via XPath: %s", xpath)
                            return el
                    except StaleElementReferenceException:
                        continue
            except WebDriverException:
                continue

        time.sleep(0.1)

    raise TimeoutException(
        f"Nenhum elemento encontrado em {timeout_total:.1f}s "
        f"({len(seletores_css)} CSS + {len(xpaths)} XPath)."
    )


def _aguardar_painel(driver: webdriver.Chrome, timeout: float = 180.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        for seletor in _CSS_PAINEL_LATERAL:
            try:
                elementos = driver.find_elements(By.CSS_SELECTOR, seletor)
                if elementos and elementos[0].is_displayed():
                    logger.info("Painel lateral do WhatsApp confirmado.")
                    return
            except WebDriverException:
                pass
        time.sleep(0.5)
    raise TimeoutException("Painel lateral nao carregou no prazo.")


def _dispensar_popups(driver: webdriver.Chrome) -> None:
    """Fecha alertas nativos e popups do WhatsApp Web que bloqueiam o fluxo."""
    try:
        alert = driver.switch_to.alert
        logger.warning("Alert nativo detectado: %r. Dispensando.", alert.text)
        alert.dismiss()
        time.sleep(0.3)
    except Exception:
        pass

    for seletor in _CSS_POPUP_FECHAR:
        try:
            botoes = driver.find_elements(By.CSS_SELECTOR, seletor)
            if botoes and botoes[0].is_displayed():
                botoes[0].click()
                logger.warning("Popup WA Web dispensado: %s", seletor)
                time.sleep(0.3)
                break
        except (NoSuchElementException, WebDriverException):
            continue


def _backoff(tentativa: int) -> float:
    """Backoff exponencial com jitter: min(30, 2^n) + uniform(0, 1)."""
    return min(30.0, 2.0 ** tentativa) + random.uniform(0.0, 1.0)


# ==============================================================
#  FINGERPRINT PARA AUTO-RESPONDER
# ==============================================================

def _criar_fingerprint(nome_chat: str, texto_previa: str, horario: str) -> str:
    conteudo = f"{nome_chat}|{texto_previa}|{horario}"
    return hashlib.sha256(conteudo.encode("utf-8")).hexdigest()


# ==============================================================
#  WHATSAPP SERVICE
# ==============================================================

@dataclass(frozen=True)
class _PedidoEnvio:
    contato: Contato
    mensagem: str
    future: Future


@dataclass(frozen=True)
class _PedidoVarredura:
    """Pede ao worker um ciclo imediato do auto-responder. Future -> ResultadoVarredura."""
    future: Future


@dataclass(frozen=True)
class _PedidoLeitura:
    """Pede ao worker a lista de conversas nao lidas. Future -> list[ChatNaoLido]."""
    future: Future


@dataclass(frozen=True)
class _PedidoDiagnostico:
    """Pede ao worker um dump do DOM da lista lateral. Future -> Path do arquivo."""
    future: Future


class WhatsAppService:
    """
    Servico de mensageria com WebDriver isolado em worker thread unica.

    Contrato:
      - O driver e criado, usado e encerrado EXCLUSIVAMENTE pela _worker_thread.
      - Todas as outras threads comunicam com o servico via fila
        (enviar(), varrer_agora(), listar_nao_lidos()).
      - O auto-responder e executado pela mesma worker thread em ciclos periodicos,
        garantindo acesso sequencial ao driver.
    """

    def __init__(self, agenda: Agenda, evento_saida: threading.Event) -> None:
        self._agenda       = agenda
        self._evento_saida = evento_saida
        self._driver: webdriver.Chrome | None = None
        self._autenticado  = False
        self._limitador    = LimitadorEnvio()
        self._mensagens_processadas: set[str] = set()

        self._fila: queue.Queue[
            _PedidoEnvio | _PedidoVarredura | _PedidoLeitura
            | _PedidoDiagnostico | None
        ] = queue.Queue(maxsize=CFG.capacidade_fila_wpp)

        self._worker_thread = threading.Thread(
            target=self._worker,
            name="WhatsAppWorker",
            daemon=False,
        )

    def iniciar(self) -> None:
        self._worker_thread.start()

    def encerrar(self) -> None:
        try:
            self._fila.put_nowait(None)
        except queue.Full:
            pass
        self._worker_thread.join(timeout=15)
        self._encerrar_driver()

    # ----------------------------------------------------------
    #  API PUBLICA (chamada de qualquer thread)
    # ----------------------------------------------------------

    def resolver_contato(self, nome: str) -> Contato:
        """Levanta LookupError / ContatoAmbiguo conforme a agenda."""
        return self._agenda.buscar(nome)

    def buscar_contatos(self, nome: str) -> list[Contato]:
        return self._agenda.buscar_todos(nome)

    def enviar(self, nome: str, mensagem: str) -> Future:
        """Resolve o nome na agenda e enfileira o envio. Future -> ResultadoEnvio."""
        try:
            contato = self._agenda.buscar(nome)
        except LookupError as erro:
            future: Future = Future()
            future.set_result(ResultadoEnvio(EstadoEnvio.FALHOU, nome, str(erro)))
            return future
        return self.enviar_para(contato, mensagem)

    def enviar_para(self, contato: Contato, mensagem: str) -> Future:
        """Enfileira um envio para um contato ja resolvido. Future -> ResultadoEnvio."""
        future: Future = Future()
        mensagem = mensagem.strip()

        if not mensagem:
            future.set_result(
                ResultadoEnvio(EstadoEnvio.FALHOU, contato.nome, "Mensagem vazia.")
            )
            return future

        logger.info(
            "Destinatario resolvido: %s (%s).",
            contato.nome,
            _mascarar_numero(contato.numero),
        )
        pedido = _PedidoEnvio(contato=contato, mensagem=mensagem, future=future)

        try:
            self._fila.put_nowait(pedido)
        except queue.Full:
            future.set_result(
                ResultadoEnvio(
                    EstadoEnvio.FILA_CHEIA,
                    contato.nome,
                    "Fila do WhatsApp cheia. Tente novamente.",
                )
            )
        return future

    def varrer_agora(self) -> Future:
        """
        Solicita um ciclo imediato do auto-responder (ignora a janela minima
        de 10 minutos; o fingerprint ainda evita resposta duplicada).
        Future -> ResultadoVarredura.
        """
        return self._enfileirar_interno(_PedidoVarredura(future=Future()))

    def listar_nao_lidos(self) -> Future:
        """
        Solicita a lista de conversas nao lidas sem abrir nenhum chat
        (portanto sem marcar como lidas). Future -> list[ChatNaoLido].
        """
        return self._enfileirar_interno(_PedidoLeitura(future=Future()))

    def diagnosticar_whatsapp(self) -> Future:
        """
        Grava wpp_diagnostico.json (contagem de seletores, HTML de amostra e
        resultado da coleta) na pasta do projeto. Future -> Path.
        """
        return self._enfileirar_interno(_PedidoDiagnostico(future=Future()))

    def _enfileirar_interno(
        self, pedido: _PedidoVarredura | _PedidoLeitura | _PedidoDiagnostico
    ) -> Future:
        try:
            self._fila.put_nowait(pedido)
        except queue.Full:
            self._resolver(pedido.future, erro=queue.Full("Fila do WhatsApp cheia."))
        return pedido.future

    @staticmethod
    def _resolver(
        future: Future, valor: object = None, erro: BaseException | None = None
    ) -> None:
        if future.done():
            return
        try:
            if erro is not None:
                future.set_exception(erro)
            else:
                future.set_result(valor)
        except Exception:
            pass

    # ----------------------------------------------------------
    #  WORKER (unica thread com acesso ao driver)
    # ----------------------------------------------------------

    def _worker(self) -> None:
        try:
            self._conectar()
        except AutenticacaoNecessaria as exc:
            logger.error("%s", exc)

        ultimo_auto_responder = monotonic()

        while True:
            # Processa pedidos com timeout curto para checar saida.
            try:
                pedido = self._fila.get(timeout=1.0)
            except queue.Empty:
                pedido = None

            if pedido is None and self._evento_saida.is_set():
                logger.info("WhatsAppWorker encerrando.")
                break

            if pedido is not None:
                self._despachar(pedido)
                self._fila.task_done()

            # Ciclo periodico do auto-responder.
            agora = monotonic()
            if agora - ultimo_auto_responder >= CFG.intervalo_auto_responder:
                try:
                    self._ciclo_auto_responder()
                except Exception:
                    logger.exception("Falha no ciclo periodico do auto-responder.")
                ultimo_auto_responder = monotonic()

    def _despachar(
        self,
        pedido: _PedidoEnvio | _PedidoVarredura | _PedidoLeitura | _PedidoDiagnostico,
    ) -> None:
        if isinstance(pedido, _PedidoEnvio):
            resultado = self._enviar_com_retry(pedido)
            self._resolver(pedido.future, resultado)

        elif isinstance(pedido, _PedidoVarredura):
            try:
                self._resolver(
                    pedido.future, self._ciclo_auto_responder(forcar=True)
                )
            except Exception as exc:
                logger.exception("Falha na varredura manual.")
                self._resolver(pedido.future, erro=exc)

        elif isinstance(pedido, _PedidoLeitura):
            try:
                self._resolver(pedido.future, self._ler_nao_lidos())
            except Exception as exc:
                logger.warning("Falha ao listar nao lidos: %s", exc)
                self._resolver(pedido.future, erro=exc)

        elif isinstance(pedido, _PedidoDiagnostico):
            try:
                self._resolver(pedido.future, self._diagnosticar())
            except Exception as exc:
                logger.warning("Falha no diagnostico: %s", exc)
                self._resolver(pedido.future, erro=exc)

    # ----------------------------------------------------------
    #  DRIVER
    # ----------------------------------------------------------

    def _criar_driver(self, modo_visivel: bool) -> webdriver.Chrome:
        opcoes = webdriver.ChromeOptions()
        opcoes.binary_location = str(CFG.caminho_brave)
        opcoes.add_argument(f"--user-data-dir={CFG.caminho_perfil}")
        opcoes.add_argument("--window-size=1366,768")
        opcoes.add_argument("--disable-gpu")
        opcoes.add_argument("--disable-extensions")
        opcoes.add_argument("--disable-renderer-backgrounding")
        opcoes.add_argument("--disable-background-timer-throttling")
        opcoes.add_argument("--disable-backgrounding-occluded-windows")
        opcoes.add_argument("--force-device-scale-factor=1")
        opcoes.add_experimental_option(
            "excludeSwitches", ["enable-automation", "enable-logging"]
        )
        opcoes.add_experimental_option("useAutomationExtension", False)

        posicao = "--window-position=100,100" if (modo_visivel or CFG.modo_debug) \
                  else "--window-position=-1200,0"
        opcoes.add_argument(posicao)

        driver = webdriver.Chrome(options=opcoes)
        driver.set_page_load_timeout(CFG.page_load_timeout + 10)
        return driver

    def _conectar(self) -> None:
        """Inicializa o driver e autentica no WhatsApp Web."""
        ja_autenticado = CFG.caminho_auth.exists()

        try:
            self._driver = self._criar_driver(modo_visivel=not ja_autenticado)
            self._driver.get("https://web.whatsapp.com")

            if not ja_autenticado:
                logger.info("Primeiro acesso. Aguardando QR Code (3 minutos).")
                _aguardar_painel(self._driver, timeout=180.0)
                CFG.caminho_projeto.mkdir(parents=True, exist_ok=True)
                CFG.caminho_auth.write_text(
                    f"Autenticado em {datetime.now()}\n", encoding="utf-8"
                )
                logger.info("QR Code autenticado. Sessao salva.")
                if not CFG.modo_debug:
                    self._driver.set_window_position(-1200, 0)
            else:
                try:
                    _aguardar_painel(self._driver, timeout=30.0)
                    logger.info("Sessao restaurada com sucesso.")
                except TimeoutException:
                    logger.warning("Sessao expirada. Removendo marcador de autenticacao.")
                    CFG.caminho_auth.unlink(missing_ok=True)
                    raise AutenticacaoNecessaria("Sessao expirada. Escanear QR Code.")

            self._autenticado = True

        except AutenticacaoNecessaria:
            raise
        except TimeoutException:
            logger.error("Timeout aguardando QR Code.")
            self._encerrar_driver()
        except WebDriverException as exc:
            logger.exception("Falha ao inicializar WebDriver: %s", exc)
            self._encerrar_driver()

    def _encerrar_driver(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
        self._autenticado = False

    def _driver_ativo(self) -> bool:
        if self._driver is None or not self._autenticado:
            return False
        try:
            _ = self._driver.current_url
            return True
        except WebDriverException:
            return False

    # ----------------------------------------------------------
    #  NAVEGACAO
    # ----------------------------------------------------------

    def _navegar_para_contato(self, numero: str) -> bool:
        assert self._driver is not None

        # Estrategia 1: API JS interna (sem reload de pagina).
        try:
            resultado = self._driver.execute_script(_JS_NAVEGAR_CHAT, numero)
            if resultado:
                logger.debug(
                    "Navegacao JS para %s bem-sucedida.", _mascarar_numero(numero)
                )
                time.sleep(0.15)
                return True
        except WebDriverException as exc:
            logger.debug("Navegacao JS falhou (%s). Usando URL.", exc)

        # Estrategia 2: URL de fallback.
        try:
            url = (
                f"https://web.whatsapp.com/send/"
                f"?phone={numero}&type=phone_number&app_absent=0"
            )
            self._driver.get(url)
            WebDriverWait(self._driver, CFG.page_load_timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(1.0)
            _dispensar_popups(self._driver)
            logger.debug(
                "Navegacao URL para %s concluida.", _mascarar_numero(numero)
            )
            return True
        except TimeoutException:
            logger.error(
                "Timeout ao carregar URL para %s.", _mascarar_numero(numero)
            )
            return False
        except WebDriverException as exc:
            logger.error(
                "WebDriverException na URL para %s: %s",
                _mascarar_numero(numero),
                exc,
            )
            return False

    # ----------------------------------------------------------
    #  ENVIO COM CONFIRMACAO
    # ----------------------------------------------------------

    def _titulo_chat_aberto(self) -> str:
        assert self._driver is not None
        for seletor in (
            "div[data-testid='conversation-header'] span[dir='auto']",
            "header span[dir='auto']",
        ):
            elementos = self._driver.find_elements(By.CSS_SELECTOR, seletor)
            if elementos:
                texto = elementos[0].text.strip()
                if texto:
                    return texto
        return ""

    def _confirmar_destinatario(self, contato: Contato) -> None:
        """
        Aguarda (ate 3 s, polling de 100 ms) o cabecalho refletir o contato.
        Segue assim que bater; aborta se o chat aberto for outro; prossegue
        se o cabecalho nao for localizavel.
        """
        esperado = _normalizar_nome(contato.nome)
        limite = monotonic() + 3.0
        titulo = ""
        while monotonic() < limite:
            try:
                titulo = self._titulo_chat_aberto()
            except WebDriverException:
                titulo = ""
            if titulo and _normalizar_nome(titulo) == esperado:
                return
            time.sleep(0.1)

        if titulo:
            logger.warning(
                "Chat aberto e '%s', esperado '%s'. Abortando envio.",
                titulo,
                contato.nome,
            )
            raise DestinatarioInvalido(
                f"Chat incorreto: aberto={titulo!r}, esperado={contato.nome!r}"
            )
        logger.debug("Nao foi possivel confirmar o header do chat. Prosseguindo.")

    def _enviar_uma_vez(self, pedido: _PedidoEnvio) -> ResultadoEnvio:
        """
        Executa uma unica tentativa de envio e aguarda confirmacao visual
        (nova bolha .message-out no DOM).
        """
        assert self._driver is not None
        contato = pedido.contato

        if not self._navegar_para_contato(contato.numero):
            raise TimeoutException("Falha de navegacao para o contato.")

        self._confirmar_destinatario(contato)

        campo = _encontrar_elemento(
            self._driver,
            _CSS_COMPOSITOR,
            _XPATH_COMPOSITOR,
            timeout_total=float(CFG.element_timeout),
        )

        # Conta bolhas antes do envio para usar como baseline de confirmacao.
        bolhas_antes = len(self._driver.find_elements(By.CSS_SELECTOR, _CSS_BOLHA_SAIDA))

        campo.click()
        campo.send_keys(pedido.mensagem)
        time.sleep(0.1)
        campo.send_keys(Keys.ENTER)

        # Aguarda confirmacao visual: pelo menos uma bolha nova de saida.
        try:
            WebDriverWait(self._driver, 10).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, _CSS_BOLHA_SAIDA))
                > bolhas_antes
            )
            logger.info(
                "Mensagem confirmada na conversa com %s.", contato.nome
            )
            return ResultadoEnvio(
                EstadoEnvio.CONFIRMADO_NA_CONVERSA,
                contato.nome,
                "Bolha de saida detectada no DOM.",
            )
        except TimeoutException:
            logger.warning(
                "Mensagem inserida mas bolha nao confirmada para %s.", contato.nome
            )
            return ResultadoEnvio(
                EstadoEnvio.INSERIDO_NA_INTERFACE,
                contato.nome,
                "Keys.ENTER executado. Bolha nao confirmada.",
            )

    def _enviar_com_retry(self, pedido: _PedidoEnvio) -> ResultadoEnvio:
        """Executa ate 3 tentativas com backoff exponencial e jitter."""
        ultimo_erro: Exception | None = None

        for tentativa in range(1, 4):
            logger.info(
                "Tentativa %d/3 para %s.", tentativa, pedido.contato.nome
            )

            if not self._driver_ativo():
                logger.warning("Driver inativo. Reconectando.")
                self._encerrar_driver()
                try:
                    self._conectar()
                except AutenticacaoNecessaria as exc:
                    return ResultadoEnvio(
                        EstadoEnvio.FALHOU,
                        pedido.contato.nome,
                        str(exc),
                    )

            try:
                self._limitador.aguardar()
                return self._enviar_uma_vez(pedido)

            except DestinatarioInvalido as exc:
                # Erro permanente: nao retenta.
                return ResultadoEnvio(
                    EstadoEnvio.FALHOU,
                    pedido.contato.nome,
                    str(exc),
                )

            except AutenticacaoNecessaria as exc:
                return ResultadoEnvio(
                    EstadoEnvio.FALHOU,
                    pedido.contato.nome,
                    str(exc),
                )

            except UnexpectedAlertPresentException:
                logger.warning("Alert inesperado. Dispensando.")
                try:
                    if self._driver:
                        self._driver.switch_to.alert.dismiss()
                except Exception:
                    pass
                ultimo_erro = UnexpectedAlertPresentException("Alert dispensado.")

            except TimeoutException as exc:
                logger.warning("Timeout na tentativa %d: %s", tentativa, exc)
                if self._driver:
                    _dispensar_popups(self._driver)
                ultimo_erro = exc

            except WebDriverException as exc:
                logger.error("WebDriverException na tentativa %d: %s", tentativa, exc)
                self._encerrar_driver()
                ultimo_erro = exc

            if tentativa < 3:
                espera = _backoff(tentativa)
                logger.debug("Aguardando %.1fs antes da proxima tentativa.", espera)
                time.sleep(espera)

        return ResultadoEnvio(
            EstadoEnvio.FALHOU,
            pedido.contato.nome,
            f"Falha apos 3 tentativas: {ultimo_erro}",
        )

    # ----------------------------------------------------------
    #  LISTA LATERAL: COLETA DE NAO LIDOS
    # ----------------------------------------------------------

    @staticmethod
    def _texto_opcional(item, seletor: str) -> str:
        try:
            return item.find_element(By.CSS_SELECTOR, seletor).text.strip()
        except NoSuchElementException:
            return ""

    def _coletar_nao_lidos(self) -> list[ChatNaoLido]:
        """
        Le a lista lateral e devolve apenas strings (nenhum WebElement e
        guardado). Nao abre nenhum chat, portanto nao marca nada como lido.
        Tenta primeiro um script JS tolerante a mudancas de DOM; se ele nao
        achar nada, cai para os seletores Selenium.
        """
        assert self._driver is not None

        try:
            dados = self._driver.execute_script(_JS_COLETAR_NAO_LIDOS) or {}
            chats: list[ChatNaoLido] = []
            for d in dados.get("chats", []):
                nome = str(d.get("nome", "")).strip()
                previa = str(d.get("previa", "")).strip()
                horario = str(d.get("horario", "")).strip()
                quantidade = int(d.get("quantidade") or 1)
                if not nome:
                    continue
                chats.append(
                    ChatNaoLido(
                        nome=nome,
                        previa=previa,
                        horario=horario,
                        quantidade=quantidade,
                        fingerprint=_criar_fingerprint(nome, previa, horario),
                    )
                )
            logger.info(
                "Lista lateral: %d item(ns), %d nao lido(s) (JS).",
                dados.get("total", 0),
                len(chats),
            )
            if chats:
                return chats
        except WebDriverException as exc:
            logger.debug("Coleta via JS falhou: %s", exc)

        chats = self._coletar_nao_lidos_selenium()
        logger.info("Fallback Selenium: %d nao lido(s).", len(chats))
        if not chats:
            logger.warning(
                "Nenhuma conversa nao lida detectada. Se ha mensagens pendentes, "
                "use o comando 'diagnosticar whatsapp'."
            )
        return chats

    def _coletar_nao_lidos_selenium(self) -> list[ChatNaoLido]:
        """Coleta por seletores Selenium (fallback). Apenas strings, sem WebElements."""
        assert self._driver is not None
        resultado: list[ChatNaoLido] = []

        itens = self._driver.find_elements(By.CSS_SELECTOR, "div[role='listitem']")
        for item in itens:
            try:
                badge = None
                for seletor in _CSS_BADGE_NAO_LIDA:
                    encontrados = item.find_elements(By.CSS_SELECTOR, seletor)
                    if encontrados:
                        badge = encontrados[0]
                        break
                if badge is None:
                    continue

                texto_badge = (
                    badge.text or badge.get_attribute("aria-label") or ""
                ).strip()
                achado = re.search(r"\d+", texto_badge)
                quantidade = int(achado.group()) if achado else 1

                nome = (
                    item.find_element(By.CSS_SELECTOR, "span[title]")
                    .get_attribute("title")
                    or ""
                )
                if not nome:
                    continue

                previa = self._texto_opcional(item, _CSS_PREVIA_CHAT)
                horario = self._texto_opcional(item, "span[data-testid='msg-time']")

                resultado.append(
                    ChatNaoLido(
                        nome=nome,
                        previa=previa,
                        horario=horario,
                        quantidade=quantidade,
                        fingerprint=_criar_fingerprint(nome, previa, horario),
                    )
                )
            except (NoSuchElementException, StaleElementReferenceException):
                continue

        return resultado

    def _ler_nao_lidos(self) -> list[ChatNaoLido]:
        if not self._driver_ativo():
            raise ConnectionError("WhatsApp nao esta conectado.")
        return self._coletar_nao_lidos()

    def _diagnosticar(self) -> Path:
        """
        Grava wpp_diagnostico.json na pasta do projeto. O arquivo contem HTML
        da lista lateral (nomes e previas de mensagens): revise antes de compartilhar.
        """
        if not self._driver_ativo():
            raise ConnectionError("WhatsApp nao esta conectado.")
        assert self._driver is not None

        dados = self._driver.execute_script(_JS_DIAGNOSTICO) or {}
        dados["coleta_js"] = self._driver.execute_script(_JS_COLETAR_NAO_LIDOS)

        CFG.caminho_projeto.mkdir(parents=True, exist_ok=True)
        caminho = CFG.caminho_projeto / "wpp_diagnostico.json"
        caminho.write_text(
            json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Diagnostico gravado em %s", caminho)
        return caminho

    # ----------------------------------------------------------
    #  AUTO-RESPONDER
    # ----------------------------------------------------------

    def _ciclo_auto_responder(self, forcar: bool = False) -> ResultadoVarredura:
        """
        Responde chats nao lidos de contatos permitidos.
        forcar=True ignora a janela minima de 10 minutos (usado na varredura
        manual); o fingerprint continua impedindo resposta duplicada.
        """
        if not self._driver_ativo():
            return ResultadoVarredura(0, 0, "driver_inativo")

        permitidos = self._agenda.ler_permitidos()
        if not permitidos:
            return ResultadoVarredura(0, 0, "sem_permitidos")

        # Fase 1: coleta apenas dados (strings) para evitar stale refs.
        try:
            chats = self._coletar_nao_lidos()
        except WebDriverException as exc:
            logger.warning("Erro na varredura do auto-responder: %s", exc)
            return ResultadoVarredura(0, 0, "erro_varredura")

        candidatos = [
            c for c in chats
            if _normalizar_nome(c.nome) in permitidos
            and c.fingerprint not in self._mensagens_processadas
            and (forcar or self._validar_janela_tempo(c.horario))
        ]

        # Fase 2: processa cada candidato re-localizando o elemento pelo nome.
        respondidos = 0
        for chat in candidatos:
            if self._evento_saida.is_set():
                break
            if self._processar_chat_pelo_nome(chat.nome):
                self._mensagens_processadas.add(chat.fingerprint)
                respondidos += 1
            time.sleep(0.8)

        return ResultadoVarredura(respondidos, len(chats))

    def _validar_janela_tempo(self, horario: str) -> bool:
        """Retorna True se o horario da mensagem estiver dentro de 10min a 24h atras."""
        if not (horario and ":" in horario and len(horario) == 5):
            return True  # Formato desconhecido: permite processar.
        try:
            h, m = map(int, horario.split(":"))
            agora = datetime.now()
            t_msg = agora.replace(hour=h, minute=m, second=0, microsecond=0)
            if t_msg > agora:
                t_msg -= timedelta(days=1)
            diff_min = (agora - t_msg).total_seconds() / 60
            return 10 <= diff_min < 1440
        except ValueError:
            return True

    def _abrir_chat_pelo_nome(self, nome: str) -> bool:
        """Abre o chat na lista lateral pelo nome (JS primeiro, Selenium como fallback)."""
        assert self._driver is not None
        try:
            if self._driver.execute_script(_JS_ABRIR_CHAT_POR_NOME, nome):
                return True
        except WebDriverException as exc:
            logger.debug("Abertura de chat via JS falhou: %s", exc)

        for item in self._driver.find_elements(By.CSS_SELECTOR, "div[role='listitem']"):
            try:
                titulo = item.find_element(By.CSS_SELECTOR, "span[title]")
                if titulo.get_attribute("title") == nome:
                    self._driver.execute_script("arguments[0].click();", item)
                    return True
            except (NoSuchElementException, StaleElementReferenceException):
                continue
        return False

    def _processar_chat_pelo_nome(self, nome: str) -> bool:
        """Re-localiza o chat pelo nome e envia a auto-resposta."""
        assert self._driver is not None
        try:
            if not self._abrir_chat_pelo_nome(nome):
                logger.warning("Chat '%s' nao encontrado na lista lateral.", nome)
                return False
            time.sleep(1.2)

            campo = _encontrar_elemento(
                self._driver,
                _CSS_COMPOSITOR,
                _XPATH_COMPOSITOR,
                timeout_total=15.0,
            )
            campo.click()
            campo.send_keys(
                "Jairo: Oi! Sou o assistente pessoal desse contato. "
                "Vou avisa-lo agora. Ele te responde em breve!"
            )
            time.sleep(0.3)
            campo.send_keys(Keys.ENTER)
            logger.info("Auto-resposta enviada para %s.", nome)
            return True
        except WebDriverException as exc:
            logger.warning("Erro no auto-responder para %s: %s", nome, exc)
        return False


# ==============================================================
#  SINAIS SONOROS (feedback instantaneo, sem TTS)
# ==============================================================

class Sinal:
    """Bipes curtos (somente Windows). Sincronos, mas de 30 a 160 ms."""

    @staticmethod
    def _bip(frequencia: int, ms: int) -> None:
        if winsound is None:
            return
        try:
            winsound.Beep(frequencia, ms)
        except Exception:
            pass

    @classmethod
    def ok(cls) -> None:
        cls._bip(1200, 40)

    @classmethod
    def erro(cls) -> None:
        cls._bip(400, 160)

    @classmethod
    def alerta(cls) -> None:
        for frequencia in (900, 1200, 900):
            cls._bip(frequencia, 90)


# ==============================================================
#  ESTADO COMPARTILHADO DE FALA (anti-eco)
# ==============================================================

class EstadoFala:
    """
    Compartilhado entre VozService e AudioPipeline para que o microfone
    descarte audio capturado enquanto (ou logo apos) o Jairo fala.
    """

    def __init__(self) -> None:
        self.falando = threading.Event()
        self.geracao: int = 0
        self.fim: float = -1e9

    def iniciar(self) -> None:
        self.geracao += 1
        self.falando.set()

    def finalizar(self) -> None:
        self.fim = monotonic()
        self.falando.clear()

    def interferiu(self, geracao_antes: int, margem: float = 1.0) -> bool:
        """True se houve fala durante (ou imediatamente antes do fim d)a captura."""
        return (
            self.falando.is_set()
            or self.geracao != geracao_antes
            or (monotonic() - self.fim) < margem
        )


# ==============================================================
#  GANHO AUTOMATICO (voz baixa)
# ==============================================================

def _ganho_automatico(audio: "sr.AudioData") -> "sr.AudioData":
    """
    Amplifica frases captadas em volume baixo ate um RMS-alvo (limitado por
    JAIRO_GANHO_MAX) antes de enviar ao reconhecedor. Melhora a acuracia do
    STT com fala baixa ou microfone distante. Sem audioop, devolve o original.
    """
    if audioop is None or CFG.ganho_max <= 1.0:
        return audio
    try:
        rms = audioop.rms(audio.frame_data, audio.sample_width)
        if rms <= 0:
            return audio
        ganho = min(CFG.ganho_max, CFG.alvo_rms / rms)
        if ganho < 1.15:
            return audio
        dados = audioop.mul(audio.frame_data, audio.sample_width, ganho)
        logger.debug("Ganho automatico x%.2f (rms %d).", ganho, rms)
        return sr.AudioData(dados, audio.sample_rate, audio.sample_width)
    except Exception as exc:
        logger.debug("Ganho automatico falhou: %s", exc)
        return audio


# ==============================================================
#  AUDIO: CAPTURA E TRANSCRICAO
# ==============================================================

class AudioPipeline:
    """
    Gerencia captura de microfone e transcricao em threads dedicadas.
    Todos os comandos transcritos chegam em cmd_queue.
    """

    def __init__(self, evento_saida: threading.Event, estado_fala: EstadoFala) -> None:
        self._evento_saida  = evento_saida
        self._estado_fala   = estado_fala
        self._reconhecedor  = sr.Recognizer()
        self._audio_queue: queue.Queue[sr.AudioData] = queue.Queue(maxsize=3)
        self.cmd_queue:    queue.Queue[str]           = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._recalibrar_apos: float | None = None

    def _aplicar_limite_energia(self) -> None:
        if CFG.energy_fixo > 0:
            self._reconhecedor.energy_threshold = CFG.energy_fixo
        else:
            self._reconhecedor.energy_threshold = max(
                CFG.energy_min, min(self._reconhecedor.energy_threshold, CFG.energy_max)
            )

    def solicitar_recalibracao(self) -> None:
        """
        Pede recalibracao do microfone. Quem executa e a thread de captura,
        que e dona do stream, depois que o Jairo terminar de falar.
        """
        self._recalibrar_apos = monotonic() + 0.5

    def _recalibrar_agora(self, mic) -> None:
        try:
            self._reconhecedor.adjust_for_ambient_noise(
                mic, duration=CFG.calibracao_seg
            )
            self._aplicar_limite_energia()
            logger.info(
                "Microfone recalibrado. Threshold: %.0f",
                self._reconhecedor.energy_threshold,
            )
            Sinal.ok()
        except Exception as exc:
            logger.warning("Falha na recalibracao: %s", exc)

    def configurar(self) -> None:
        # pause_threshold = silencio que encerra a frase (principal fonte de latencia).
        self._reconhecedor.pause_threshold          = CFG.pause_threshold
        self._reconhecedor.non_speaking_duration    = min(0.5, CFG.pause_threshold)
        self._reconhecedor.phrase_threshold         = 0.2
        self._reconhecedor.dynamic_energy_threshold = False
        # Ponto de partida da calibracao: o padrao do speech_recognition (300)
        # puxava o limite para cima e exigia fala mais alta.
        self._reconhecedor.energy_threshold = (
            CFG.energy_fixo if CFG.energy_fixo > 0 else CFG.energy_min
        )

    def calibrar(self) -> None:
        logger.info("Calibrando microfone (%.1f s de silencio)...", CFG.calibracao_seg)
        try:
            with sr.Microphone() as mic:
                self._reconhecedor.adjust_for_ambient_noise(mic, duration=CFG.calibracao_seg)
            self._aplicar_limite_energia()
            logger.info(
                "Microfone calibrado. Threshold: %.0f",
                self._reconhecedor.energy_threshold,
            )
        except OSError as exc:
            logger.error("Microfone nao encontrado: %s", exc)
        except Exception as exc:
            logger.exception("Erro na calibracao: %s", exc)

    def iniciar(self) -> None:
        captura     = threading.Thread(
            target=self._capturar, name="AudioCaptura", daemon=False
        )
        transcricao = threading.Thread(
            target=self._transcrever, name="AudioTranscricao", daemon=False
        )
        self._threads = [captura, transcricao]
        for t in self._threads:
            t.start()

    def encerrar(self) -> None:
        for t in self._threads:
            t.join(timeout=5)

    def _capturar(self) -> None:
        """
        Mantem o microfone ABERTO entre frases (reabrir a cada frase custava
        100-300 ms e podia cortar o inicio da fala). timeout=1 permite checar
        o evento de saida sem ficar preso em listen().
        """
        while not self._evento_saida.is_set():
            try:
                with sr.Microphone() as mic:
                    while not self._evento_saida.is_set():
                        apos = self._recalibrar_apos
                        if (
                            apos is not None
                            and monotonic() >= apos
                            and not self._estado_fala.falando.is_set()
                            and monotonic() - self._estado_fala.fim > 0.8
                        ):
                            self._recalibrar_apos = None
                            self._recalibrar_agora(mic)
                            continue

                        falava_antes = self._estado_fala.falando.is_set()
                        geracao_antes = self._estado_fala.geracao

                        try:
                            audio = self._reconhecedor.listen(
                                mic,
                                timeout=1,
                                phrase_time_limit=CFG.phrase_time_limit,
                            )
                        except sr.WaitTimeoutError:
                            continue

                        if self._evento_saida.is_set():
                            break

                        # Descarta o proprio TTS captado pelo microfone.
                        if falava_antes or self._estado_fala.interferiu(geracao_antes):
                            logger.debug("Audio descartado (captado durante fala do Jairo).")
                            continue

                        try:
                            self._audio_queue.put_nowait(audio)
                        except queue.Full:
                            pass
            except OSError:
                time.sleep(2)
            except Exception as exc:
                logger.warning("AudioCaptura: %s", exc)
                time.sleep(1)

    def _transcrever(self) -> None:
        while not self._evento_saida.is_set():
            try:
                audio = self._audio_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                texto = self._reconhecedor.recognize_google(
                    _ganho_automatico(audio), language="pt-BR"
                )
                if texto.strip():
                    self.cmd_queue.put(texto.lower())
            except sr.UnknownValueError:
                pass
            except sr.RequestError as exc:
                logger.error("API STT Google: %s", exc)
            except Exception as exc:
                logger.warning("AudioTranscricao: %s", exc)


# ==============================================================
#  SERVICO DE VOZ
# ==============================================================

_PS_FALAR = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "try { $s.SelectVoiceByHints("
    "[System.Speech.Synthesis.VoiceGender]::NotSet,"
    "[System.Speech.Synthesis.VoiceAge]::NotSet,0,"
    "[System.Globalization.CultureInfo]::GetCultureInfo('pt-BR')) } catch {};"
    "$s.Volume = 100;"
    "$s.Speak($env:JAIRO_TEXTO)"
)


class VozService:
    """
    Sintetizador de voz seguro para Windows COM.
    A fila e consumida EXCLUSIVAMENTE na thread principal.

    Backends (JAIRO_TTS):
      auto       pyttsx3 com fallback automatico para PowerShell/SAPI
      pyttsx3    somente pyttsx3
      powershell somente System.Speech via PowerShell

    O motor pyttsx3 e recriado a cada fala: reutilizar uma instancia apos
    runAndWait() e uma causa conhecida de fala silenciosa no Windows.
    Uma fala cujo runAndWait() retorna rapido demais para o tamanho do
    texto e tratada como silenciosa e repetida pelo PowerShell.
    """

    _SEGUNDOS_MIN_POR_CHAR = 0.02

    def __init__(self, estado_fala: EstadoFala) -> None:
        self._fila: queue.Queue[str] = queue.Queue()
        self._estado = estado_fala
        self._backend = CFG.tts_backend if CFG.tts_backend in (
            "auto", "pyttsx3", "powershell"
        ) else "auto"
        self._voz_id: str | None = None
        self._falhas_pyttsx3 = 0
        self.silencioso: bool = CFG.modo_silencioso
        self.ultima_fala: str = ""

    # ----------------------------------------------------------
    #  INICIALIZACAO
    # ----------------------------------------------------------

    def iniciar(self) -> None:
        if self._backend == "powershell":
            logger.info("TTS: backend PowerShell/SAPI (configurado).")
            return
        try:
            motor = pyttsx3.init()
            for voz in motor.getProperty("voices"):
                logger.debug("Voz disponivel: %s | %s", voz.name, voz.id)
                identificador = f"{voz.name} {voz.id}".lower()
                if self._voz_id is None and any(
                    k in identificador
                    for k in ("brazil", "portuguese", "pt-br", "pt_br")
                ):
                    self._voz_id = voz.id
                    logger.info("Voz selecionada: %s", voz.name)
            try:
                motor.stop()
            except Exception:
                pass
            logger.info("Motor de voz inicializado (backend: %s).", self._backend)
        except Exception as exc:
            logger.error("pyttsx3 nao iniciou: %s. Usando PowerShell/SAPI.", exc)
            self._backend = "powershell"

    # ----------------------------------------------------------
    #  API PUBLICA
    # ----------------------------------------------------------

    def falar(self, texto: str) -> None:
        """Enfileira uma resposta (sempre falada). Retorna imediatamente."""
        logger.info("[VOZ] %s", texto)
        self.ultima_fala = texto
        self._fila.put(texto)

    def confirmar(self, texto: str) -> None:
        """
        Confirmacao curta de acao. Falada normalmente; no modo silencioso vira
        um bip. Nao sobrescreve ultima_fala (usada por "repetir").
        """
        if self.silencioso:
            logger.info("[ACK] %s", texto)
            Sinal.ok()
            return
        logger.info("[VOZ] %s", texto)
        self._fila.put(texto)

    def consumir_fila(self) -> None:
        """
        Deve ser chamado no loop da thread principal.
        Processa todos os textos pendentes na fila.
        """
        while not self._fila.empty():
            try:
                texto = self._fila.get_nowait()
            except queue.Empty:
                break
            try:
                if texto:
                    self._estado.iniciar()
                    try:
                        self._reproduzir(texto)
                    finally:
                        self._estado.finalizar()
            except Exception as exc:
                logger.warning("Erro ao reproduzir voz: %s", exc)
            finally:
                self._fila.task_done()

    # ----------------------------------------------------------
    #  BACKENDS
    # ----------------------------------------------------------

    def _reproduzir(self, texto: str) -> None:
        if self._backend in ("auto", "pyttsx3"):
            resultado = self._falar_pyttsx3(texto)
            if resultado == "ok":
                self._falhas_pyttsx3 = 0
                return

            self._falhas_pyttsx3 += 1
            logger.warning(
                "pyttsx3 retornou '%s' (ocorrencia %d).",
                resultado,
                self._falhas_pyttsx3,
            )
            if self._backend == "pyttsx3":
                return
            if self._falhas_pyttsx3 >= 2:
                logger.warning("Alternando definitivamente para PowerShell/SAPI.")
                self._backend = "powershell"

        self._falar_powershell(texto)

    def _falar_pyttsx3(self, texto: str) -> str:
        """Retorna 'ok', 'silencioso' ou 'erro'."""
        try:
            motor = pyttsx3.init()
            motor.setProperty("rate", 175)
            motor.setProperty("volume", 1.0)
            if self._voz_id:
                motor.setProperty("voice", self._voz_id)

            inicio = monotonic()
            motor.say(texto)
            motor.runAndWait()
            decorrido = monotonic() - inicio

            try:
                motor.stop()
            except Exception:
                pass
            del motor

            if (
                len(texto) >= 8
                and decorrido < len(texto) * self._SEGUNDOS_MIN_POR_CHAR
            ):
                return "silencioso"
            return "ok"
        except Exception as exc:
            logger.warning("pyttsx3 falhou: %s", exc)
            return "erro"

    def _falar_powershell(self, texto: str) -> bool:
        """
        Fala via System.Speech. O texto vai por variavel de ambiente, nunca
        interpolado no script, sem shell=True.
        """
        try:
            processo = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _PS_FALAR,
                ],
                env={**os.environ, "JAIRO_TEXTO": texto},
                capture_output=True,
                timeout=90,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if processo.returncode != 0:
                logger.error(
                    "PowerShell TTS retornou %d: %s",
                    processo.returncode,
                    processo.stderr.decode("utf-8", errors="replace").strip(),
                )
                return False
            return True
        except FileNotFoundError:
            logger.error("powershell.exe nao encontrado no PATH.")
        except subprocess.TimeoutExpired:
            logger.error("Timeout no TTS via PowerShell.")
        except OSError as exc:
            logger.error("Falha ao executar PowerShell TTS: %s", exc)
        return False

    # ----------------------------------------------------------
    #  DIAGNOSTICO (python jairo.py --testar-voz)
    # ----------------------------------------------------------

    def diagnostico(self) -> None:
        print("Vozes detectadas pelo pyttsx3:")
        try:
            motor = pyttsx3.init()
            for voz in motor.getProperty("voices"):
                print(f"  - {voz.name} | {voz.id}")
            motor.stop()
        except Exception as exc:
            print(f"  falha ao listar vozes: {exc}")

        print("\n[1/2] Teste via pyttsx3...")
        resultado = self._falar_pyttsx3(
            "Teste de voz pelo pyttsx3. Se voce esta ouvindo isso, o audio funciona."
        )
        print(f"  resultado interno: {resultado}")

        print("\n[2/2] Teste via PowerShell/SAPI...")
        ok = self._falar_powershell(
            "Teste de voz pelo PowerShell. Se voce esta ouvindo isso, o audio funciona."
        )
        print(f"  executou sem erro: {ok}")

        print(
            "\nInterpretacao:\n"
            "  - Ouviu apenas o segundo: defina JAIRO_TTS=powershell no .env.\n"
            "  - Ouviu apenas o primeiro: defina JAIRO_TTS=pyttsx3 no .env.\n"
            "  - Nao ouviu nenhum: verifique o dispositivo de saida padrao do Windows,\n"
            "    o mixer de volume (volume do Python/terminal) e se ha vozes pt-BR\n"
            "    instaladas em Configuracoes > Hora e idioma > Fala."
        )


# ==============================================================
#  CONTROLE DE MIDIA E VOLUME
# ==============================================================

_VK_VOLUME_MUTE      = 0xAD
_VK_VOLUME_DOWN      = 0xAE
_VK_VOLUME_UP        = 0xAF
_VK_MEDIA_NEXT       = 0xB0
_VK_MEDIA_PREV       = 0xB1
_VK_MEDIA_PLAY_PAUSE = 0xB3
_KEYEVENTF_KEYUP     = 0x0002


class ControleMidia:
    """
    Controle de midia e volume do Windows.

    - Play/pause, proxima e anterior usam as teclas de midia virtuais
      (funcionam com Spotify, YouTube no navegador, etc.).
    - Volume: usa pycaw se instalado (valor absoluto e leitura do volume
      atual). Sem pycaw, cai para teclas de volume (cada toque = 2%),
      sem leitura do valor atual.

    Chamado somente pela thread principal (COM).
    """

    def __init__(self) -> None:
        self._user32 = None
        if sys.platform == "win32":
            import ctypes
            self._user32 = ctypes.windll.user32
        self._avisou_pycaw = False

    # ----------------------------------------------------------
    #  TECLAS
    # ----------------------------------------------------------

    def _tecla(self, vk: int, vezes: int = 1) -> bool:
        if self._user32 is None:
            return False
        for _ in range(vezes):
            self._user32.keybd_event(vk, 0, 0, 0)
            self._user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
            time.sleep(0.01)
        return True

    def play_pause(self) -> bool:
        return self._tecla(_VK_MEDIA_PLAY_PAUSE)

    def proxima(self) -> bool:
        return self._tecla(_VK_MEDIA_NEXT)

    def anterior(self) -> bool:
        return self._tecla(_VK_MEDIA_PREV)

    # ----------------------------------------------------------
    #  VOLUME (pycaw opcional)
    # ----------------------------------------------------------

    def _endpoint(self):
        try:
            from ctypes import POINTER, cast
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except ImportError:
            if not self._avisou_pycaw:
                logger.info(
                    "pycaw nao instalado: volume por teclas (pip install pycaw "
                    "para volume absoluto)."
                )
                self._avisou_pycaw = True
            return None

        try:
            dispositivo = AudioUtilities.GetSpeakers()
            # Versoes recentes do pycaw expoem EndpointVolume diretamente.
            endpoint = getattr(dispositivo, "EndpointVolume", None)
            if endpoint is not None:
                return endpoint
            interface = dispositivo.Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None
            )
            return cast(interface, POINTER(IAudioEndpointVolume))
        except Exception as exc:
            logger.debug("Falha ao obter endpoint de volume: %s", exc)
            return None

    def obter_volume(self) -> int | None:
        endpoint = self._endpoint()
        if endpoint is None:
            return None
        try:
            return round(endpoint.GetMasterVolumeLevelScalar() * 100)
        except Exception as exc:
            logger.debug("Falha ao ler volume: %s", exc)
            return None

    def definir_volume(self, percentual: int) -> int | None:
        """Retorna o volume resultante se conhecido (pycaw), senao None."""
        percentual = max(0, min(100, percentual))
        endpoint = self._endpoint()
        if endpoint is not None:
            try:
                endpoint.SetMasterVolumeLevelScalar(percentual / 100.0, None)
                return percentual
            except Exception as exc:
                logger.debug("pycaw falhou ao definir volume: %s", exc)

        # Fallback: zera com 50 toques para baixo e sobe (percentual / 2) toques.
        self._tecla(_VK_VOLUME_DOWN, 50)
        if percentual > 0:
            self._tecla(_VK_VOLUME_UP, round(percentual / 2))
        return None

    def ajustar_volume(self, delta: int) -> int | None:
        """Soma delta (positivo ou negativo) ao volume. Retorna valor final se conhecido."""
        atual = self.obter_volume()
        if atual is not None:
            return self.definir_volume(atual + delta)

        vezes = max(1, round(abs(delta) / 2))
        self._tecla(_VK_VOLUME_UP if delta > 0 else _VK_VOLUME_DOWN, vezes)
        return None

    def mutar(self, silenciar: bool) -> bool:
        """
        Retorna True se o estado foi definido de forma explicita (pycaw).
        Sem pycaw, apenas alterna o mudo (retorna False).
        """
        endpoint = self._endpoint()
        if endpoint is not None:
            try:
                endpoint.SetMute(1 if silenciar else 0, None)
                return True
            except Exception as exc:
                logger.debug("pycaw falhou ao alterar mudo: %s", exc)
        self._tecla(_VK_VOLUME_MUTE)
        return False


# ==============================================================
#  CONTROLE DE SISTEMA (print e bloqueio)
# ==============================================================

_VK_LWIN     = 0x5B
_VK_SHIFT    = 0x10
_VK_ALT      = 0x12
_VK_TAB      = 0x09
_VK_F4       = 0x73
_VK_S        = 0x53
_VK_D        = 0x44
_VK_SNAPSHOT = 0x2C
_VK_LEFT     = 0x25
_VK_UP       = 0x26
_VK_RIGHT    = 0x27
_VK_DOWN     = 0x28

_KEYEVENTF_EXTENDEDKEY = 0x0001
_TECLAS_ESTENDIDAS = frozenset([_VK_LEFT, _VK_UP, _VK_RIGHT, _VK_DOWN])


class ControleSistema:
    """
    Captura de tela, bloqueio e gerenciamento de janelas do Windows.

    - Captura (area): Win+Shift+S (abre o recorte; voce seleciona a area).
    - Captura (tela inteira): Win+PrtSc (salva em Imagens\\Capturas de Tela).
    - Bloqueio: LockWorkStation() em vez de simular Win+L, porque o Windows
      nao aceita Win+L gerado por software (keybd_event).
    - Janelas: atalhos Win+D, Alt+Tab, Alt+F4, Win+setas.

    Os combos usam intervalos de 10 ms (antes 50 ms) para responder mais rapido.
    Chamado somente pela thread principal.
    """

    def __init__(self) -> None:
        self._user32 = None
        if sys.platform == "win32":
            import ctypes
            self._user32 = ctypes.windll.user32

    @staticmethod
    def _flags(vk: int) -> int:
        return _KEYEVENTF_EXTENDEDKEY if vk in _TECLAS_ESTENDIDAS else 0

    def _combo(self, *teclas: int, pausa: float = 0.01) -> bool:
        if self._user32 is None:
            return False
        for vk in teclas:
            self._user32.keybd_event(vk, 0, self._flags(vk), 0)
            time.sleep(pausa)
        for vk in reversed(teclas):
            self._user32.keybd_event(vk, 0, self._flags(vk) | _KEYEVENTF_KEYUP, 0)
            time.sleep(pausa)
        return True

    # --- captura e bloqueio ---

    def captura_tela(self) -> bool:
        return self._combo(_VK_LWIN, _VK_SHIFT, _VK_S)

    def captura_tela_cheia(self) -> bool:
        return self._combo(_VK_LWIN, _VK_SNAPSHOT)

    def bloquear(self) -> bool:
        if self._user32 is None:
            return False
        return bool(self._user32.LockWorkStation())

    # --- janelas ---

    def mostrar_area_trabalho(self) -> bool:
        return self._combo(_VK_LWIN, _VK_D)

    def trocar_janela(self) -> bool:
        return self._combo(_VK_ALT, _VK_TAB, pausa=0.03)

    def fechar_janela(self) -> bool:
        return self._combo(_VK_ALT, _VK_F4)

    def maximizar_janela(self) -> bool:
        return self._combo(_VK_LWIN, _VK_UP)

    def minimizar_janela(self) -> bool:
        return self._combo(_VK_LWIN, _VK_DOWN)

    def janela_esquerda(self) -> bool:
        return self._combo(_VK_LWIN, _VK_LEFT)

    def janela_direita(self) -> bool:
        return self._combo(_VK_LWIN, _VK_RIGHT)


# ==============================================================
#  LANCADOR DE APLICATIVOS
# ==============================================================

class AppLauncher:
    """Abre aplicativos locais sem shell=True."""

    _APPS: dict[str, list[str]] = {}

    def __init__(self) -> None:
        local_app = os.environ.get("LOCALAPPDATA", "")
        app_data  = os.environ.get("APPDATA", "")

        self._APPS = {
            "vs code":                ["code"],
            "visual studio":          ["code"],
            "discord":                [
                os.path.join(local_app, "Discord", "Update.exe"),
                "--processStart", "Discord.exe",
            ],
            "spotify":                [os.path.join(app_data, "Spotify", "Spotify.exe")],
            "chrome":                 [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            ],
            "brave":                  [str(CFG.caminho_brave)],
            "jogo":                   [r"C:\Program Files\HoYoPlay\launcher.exe"],
            "explorer":               ["explorer.exe"],
            "bloco de notas":         ["notepad.exe"],
            "calculadora":            ["calc.exe"],
            "terminal":               ["wt.exe"],
            "gerenciador de tarefas": ["taskmgr.exe"],
            "configuracoes":          ["explorer.exe", "ms-settings:"],
            "downloads":              ["explorer.exe", "shell:Downloads"],
            "projetos":               ["explorer.exe", str(CFG.caminho_projeto)],
        }

    def abrir(self, chave: str) -> bool:
        cmd = self._APPS.get(chave)
        if not cmd:
            return False
        try:
            subprocess.Popen(cmd, shell=False)
            return True
        except FileNotFoundError:
            logger.error("Executavel nao encontrado: %s -> %s", chave, cmd)
            return False
        except OSError as exc:
            logger.exception("Erro ao abrir %s: %s", chave, exc)
            return False

    @property
    def chaves(self) -> list[str]:
        return list(self._APPS.keys())


# ==============================================================
#  PROCESSADOR DE COMANDOS
# ==============================================================

# Padroes de midia/volume aplicados sobre o comando sem acentos.
_RE_VOL_SUBIR = re.compile(
    r"\b(?:aument|sub|sob|elev)\w*\s+(?:o\s+)?(?:volume|som)(?:\s+(?:em\s+)?(\d{1,2}))?\b"
    r"|\bvolume\s+mais\s+alto\b"
)
_RE_VOL_DESCER = re.compile(
    r"\b(?:diminu|abaix|baix|reduz)\w*\s+(?:o\s+)?(?:volume|som)(?:\s+(?:em\s+)?(\d{1,2}))?\b"
    r"|\bvolume\s+mais\s+baixo\b"
)
_RE_VOL_ABS = re.compile(
    r"\bvolume\s+(?:para\s+|em\s+|no\s+)?(\d{1,3})\s*(?:%|por\s*cento)?"
)
_RE_VOL_CONSULTA = re.compile(r"\bqual\s+(?:e\s+)?(?:o\s+)?volume\b")
_RE_DESMUTAR = re.compile(
    r"\b(?:desmutar|tirar\s+do\s+mudo|voltar\s+o\s+som|restaurar\s+o\s+som|ativar\s+o\s+som)\b"
)
_RE_MUTAR = re.compile(r"\b(?:mutar|mudo|tirar\s+o\s+som)\b")
_RE_PROXIMA = re.compile(
    r"\b(?:proxima|proximo)\s+(?:musica|faixa)\b"
    r"|\b(?:pular|passar)\s+(?:a\s+)?(?:musica|faixa)\b"
)
_RE_ANTERIOR = re.compile(
    r"\b(?:musica|faixa)\s+anterior\b"
    r"|\bvoltar\s+(?:a\s+)?(?:musica|faixa)\b"
)
_CMDS_PLAY_PAUSE = frozenset([
    "pausar", "pausa", "pause", "play", "continuar", "retomar",
    "pausar musica", "parar musica", "continuar musica", "tocar musica",
    "dar play", "da play",
])
_CMDS_PROXIMA = frozenset(["proxima", "proximo", "pular"])
_CMDS_ANTERIOR = frozenset(["anterior"])

# Captura de tela, bloqueio e janelas.
_RE_PRINT = re.compile(
    r"\b(?:print\w*|screenshot|captur\w+\s+(?:de\s+|da\s+)?tela|recort\w+\s+(?:de\s+|da\s+)?tela)\b"
)
_RE_PRINT_CHEIA = re.compile(r"\b(?:inteira|cheia|salvar|salva|direto|rapido|arquivo)\b")
_RE_BLOQUEAR = re.compile(
    r"\b(?:bloque\w+|trav\w+)\s+(?:o\s+|a\s+)?(?:computador|pc|tela|notebook|maquina)\b"
)
_RE_AREA_TRABALHO = re.compile(
    r"\b(?:mostrar|ver)\s+(?:a\s+)?(?:area\s+de\s+trabalho|desktop)\b|\bminimizar\s+tudo\b"
)
_RE_TROCAR_JANELA = re.compile(
    r"\b(?:trocar|alternar|mudar)\s+(?:de\s+|a\s+)?janela\b|\bproxima\s+janela\b"
)
_RE_FECHAR_JANELA = re.compile(r"\bfechar\s+(?:a\s+)?janela\b")
_RE_MAXIMIZAR = re.compile(r"\bmaximizar\s+(?:a\s+)?janela\b")
_RE_MINIMIZAR = re.compile(r"\bminimizar\s+(?:a\s+)?janela\b")
_RE_JANELA_LADO = re.compile(r"\bjanela\b.*\b(esquerda|direita)\b")

# Timers (em memoria; nao sobrevivem a reinicializacao do Jairo).
_PALAVRAS_NUM = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4,
    "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
    "quinze": 15, "vinte": 20, "trinta": 30,
}
_RE_DURACAO = re.compile(
    r"\b(\d+|" + "|".join(_PALAVRAS_NUM) + r")\s*(segundos?|minutos?|horas?)\b"
)
_RE_TIMER = re.compile(
    r"\b(?:timer|temporizador|me\s+avis\w*|me\s+lembr\w*|avisa\s+em)\b"
)
_RE_CANCELAR_TIMERS = re.compile(
    r"\bcancel\w*\s+(?:o\s+|os\s+)?(?:timers?|temporizadores?|lembretes?)\b"
)
_RE_QUANTO_FALTA = re.compile(
    r"\b(?:quanto\s+(?:tempo\s+)?falta|meus\s+timers|ver\s+timers|listar\s+timers)\b"
)

# Informacoes.
_RE_DATA = re.compile(r"\b(?:que\s+dia|qual\s+(?:e\s+)?a\s+data|data\s+de\s+hoje)\b")
_CMDS_REPETIR = frozenset([
    "repetir", "repete", "repita", "fala de novo", "fale de novo", "como assim",
])
_CMDS_AJUDA = frozenset(["ajuda", "me ajuda", "socorro", "quais comandos", "o que voce faz"])
_DIAS_SEMANA = [
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
]
_MESES = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]

_AJUDA_COMPLETA = """
Comandos (sempre comecando com 'Jairo'):
  Captura:   tirar print | print da tela inteira (salva arquivo)
  Sistema:   bloquear o computador | desligar o computador | cancelar desligamento
  Janelas:   minimizar tudo | trocar de janela | fechar janela | maximizar janela |
             minimizar janela | janela para a esquerda/direita
  Midia:     pausar/play | proxima musica | musica anterior
  Volume:    aumentar/diminuir volume [em N] | volume N | mutar/desmutar | qual o volume
  Timers:    timer de 5 minutos | me lembra em 10 minutos de X | quanto falta |
             cancelar timers
  WhatsApp:  ler mensagens | verificar mensagens | enviar mensagem para X dizendo Y |
             diagnosticar whatsapp
  Apps:      abre [vs code, discord, spotify, chrome, brave, projetos, downloads,
             configuracoes, ...] | preparar ambiente
  Web:       pesquisar X | youtube X
  Contas:    quanto e 15 + 14 | 20 por cento de 150 | raiz quadrada de 144
  Contatos:  buscar contato gustavo (lista os cadastrados na agenda)
  Info:      que horas sao | que dia e hoje | repetir | ajuda
  Microfone: recalibrar microfone
  Modo:      modo silencioso (so bipes) | modo falante | silenciar (standby)
"""

_AJUDA_FALADA = (
    "Posso tirar print, bloquear o computador, controlar janelas, música e volume, "
    "fazer contas, criar timers, ler suas mensagens do WhatsApp, abrir aplicativos, pesquisar, "
    "e dizer a hora e a data. Diga modo silencioso para eu responder só com bipes."
)


class Retorno(Enum):
    OK                = "ok"           # acao executada: bip curto de confirmacao
    SEM_SINAL         = "sem_sinal"    # ja houve feedback (fala/bip) ou nao deve haver bip
    DESCONHECIDO      = "desconhecido" # comando nao reconhecido: bip de erro


def _parse_duracao(cn: str) -> tuple[int, int] | None:
    """Retorna (segundos, indice_do_fim_do_trecho) ou None."""
    if "meia hora" in cn:
        return 1800, cn.index("meia hora") + len("meia hora")
    achado = _RE_DURACAO.search(cn)
    if not achado:
        return None
    valor = achado.group(1)
    quantidade = int(valor) if valor.isdigit() else _PALAVRAS_NUM[valor]
    unidade = achado.group(2)
    if unidade.startswith("seg"):
        mult = 1
    elif unidade.startswith("min"):
        mult = 60
    else:
        mult = 3600
    return quantidade * mult, achado.end()


def _formatar_duracao(segundos: int) -> str:
    horas, resto = divmod(segundos, 3600)
    minutos, segs = divmod(resto, 60)
    partes: list[str] = []
    if horas:
        partes.append(_plural(horas, "hora", "horas"))
    if minutos:
        partes.append(_plural(minutos, "minuto", "minutos"))
    if segs and not horas:
        partes.append(_plural(segs, "segundo", "segundos"))
    return " e ".join(partes) or "0 segundos"


# Calculadora por voz.
_NUMEROS_EXTENSO = {
    "zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4,
    "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
    "onze": 11, "doze": 12, "treze": 13, "catorze": 14, "quatorze": 14,
    "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18,
    "dezenove": 19, "vinte": 20, "trinta": 30, "quarenta": 40,
    "cinquenta": 50, "sessenta": 60, "setenta": 70, "oitenta": 80,
    "noventa": 90, "cem": 100,  # "cento" fica de fora: conflita com "por cento"
}
_RE_NUM_EXTENSO = re.compile(
    r"\b(" + "|".join(sorted(_NUMEROS_EXTENSO, key=len, reverse=True)) + r")\b"
)
_RE_DEZENA_UNIDADE = re.compile(
    r"\b(vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa)\s+e\s+"
    r"(um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove)\b"
)
_RE_PREFIXO_CALCULO = re.compile(
    r"^(?:(?:me\s+)?(?:diga|fale|fala)\s+)?"
    r"(?:quanto\s+(?:e|da|fica|vale|sera)|calcul\w+|conta|"
    r"qual\s+(?:e\s+)?(?:o\s+)?resultado\s+d[ea]|resultado\s+d[ea])\b\s*(?:de\s+)?"
)
_RE_SO_MATEMATICA = re.compile(r"^[\d\s.+\-*/()]+$")
_NUM = r"(\d+(?:\.\d+)?)"


def _normalizar_expressao(texto: str) -> str:
    """Converte uma conta falada em portugues para uma expressao aritmetica."""
    t = texto.strip().rstrip("?!.").strip()
    t = t.replace("×", " * ").replace("÷", " / ").replace("−", " - ")

    t = _RE_DEZENA_UNIDADE.sub(
        lambda m: str(_NUMEROS_EXTENSO[m.group(1)] + _NUMEROS_EXTENSO[m.group(2)]), t
    )
    t = _RE_NUM_EXTENSO.sub(lambda m: str(_NUMEROS_EXTENSO[m.group(1)]), t)

    # Formato brasileiro: 1.500,25 -> 1500.25
    t = re.sub(
        r"\b([1-9]\d{0,2})((?:\.\d{3})+)\b",
        lambda m: m.group(1) + m.group(2).replace(".", ""),
        t,
    )
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)

    t = re.sub(_NUM + r"\s*(?:%|por\s*cento)\s*de\s*" + _NUM, r"(\1 * \2 / 100)", t)
    t = re.sub(r"raiz\s+quadrada\s+d[eo]\s+" + _NUM, r"(\1 ** 0.5)", t)
    t = re.sub(r"raiz\s+cubica\s+d[eo]\s+" + _NUM, r"(\1 ** (1 / 3))", t)
    t = re.sub(_NUM + r"\s+ao\s+quadrado\b", r"(\1 ** 2)", t)
    t = re.sub(_NUM + r"\s+ao\s+cubo\b", r"(\1 ** 3)", t)
    t = re.sub(r"\belevado\s+(?:a|na|ao)\s+(?:potencia\s+)?", " ** ", t)

    t = re.sub(r"\bmais\b", " + ", t)
    t = re.sub(r"\bmenos\b", " - ", t)
    t = re.sub(r"\b(?:vezes|multiplicado\s+por|multiplicar\s+por)\b", " * ", t)
    t = re.sub(r"(?<=[\d)])\s*\bx\b\s*(?=[\d(])", " * ", t)
    t = re.sub(r"\b(?:dividido\s+por|dividido\s+pelo|dividir\s+por)\b", " / ", t)

    t = re.sub(r"\b0+(\d)", r"\1", t)  # 007 -> 7 (literais com zero a esquerda nao compilam)
    return " ".join(t.split())


def _avaliar(no: ast.AST) -> int | float:
    """Avalia a arvore aritmetica sem usar eval (apenas + - * / ** e sinais)."""
    if isinstance(no, ast.Expression):
        return _avaliar(no.body)
    if isinstance(no, ast.Constant) and type(no.value) in (int, float):
        return no.value
    if isinstance(no, ast.UnaryOp) and isinstance(no.op, (ast.UAdd, ast.USub)):
        valor = _avaliar(no.operand)
        return +valor if isinstance(no.op, ast.UAdd) else -valor
    if isinstance(no, ast.BinOp):
        a = _avaliar(no.left)
        b = _avaliar(no.right)
        if isinstance(no.op, ast.Add):
            return a + b
        if isinstance(no.op, ast.Sub):
            return a - b
        if isinstance(no.op, ast.Mult):
            return a * b
        if isinstance(no.op, ast.Div):
            return a / b  # ZeroDivisionError sobe para quem chamou
        if isinstance(no.op, ast.Pow):
            if abs(b) > 1000 or abs(a) > 1e9:
                raise ValueError("potencia grande demais")
            resultado = a ** b
            if isinstance(resultado, complex):
                raise ValueError("resultado complexo")
            return resultado
    raise ValueError("expressao nao permitida")


def _formatar_resultado(valor: int | float) -> str:
    """Formata o numero para fala: so o numero, virgula decimal, 'menos' para negativos."""
    if isinstance(valor, float):
        if math.isnan(valor) or math.isinf(valor):
            raise ValueError("resultado invalido")
        if abs(valor - round(valor)) < 1e-9:
            valor = int(round(valor))
    if abs(valor) >= 10 ** 15:
        return "Resultado muito grande."
    if isinstance(valor, int):
        texto = str(abs(valor))
    else:
        texto = f"{abs(valor):.4f}".rstrip("0").rstrip(".").replace(".", ",")
    if texto in ("", "0"):
        return "0"
    return f"menos {texto}" if valor < 0 else texto


def _calcular(cn: str) -> str | None:
    """
    Retorna o resultado pronto para fala, ou None se o comando nao for uma conta.
    Aceita "quanto e 15 + 14", "calcule 15 mais 14", "20 por cento de 150",
    "raiz quadrada de 144" e contas soltas como "15 mais 14".
    Levanta ZeroDivisionError em divisoes por zero.
    """
    prefixo = _RE_PREFIXO_CALCULO.match(cn)
    corpo = cn[prefixo.end():] if prefixo else cn
    expr = _normalizar_expressao(corpo)

    if not _RE_SO_MATEMATICA.fullmatch(expr) or not re.search(r"\d", expr):
        return None
    # Sem prefixo, exige um operador: numero solto nao e conta.
    if not prefixo and not re.search(r"[+\-*/]", expr):
        return None

    try:
        valor = _avaliar(ast.parse(expr, mode="eval"))
        return _formatar_resultado(valor)
    except (SyntaxError, ValueError, OverflowError, RecursionError):
        return None


# Contatos e microfone.
_RE_BUSCAR_CONTATO = re.compile(
    r"\b(?:buscar|procurar|achar|listar|quais|ver)\s+(?:o\s+|os\s+|meus\s+)?contatos?"
    r"\s+(?:de\s+|do\s+|da\s+|chamados?\s+)?(.+)$"
)
_RE_RECALIBRAR = re.compile(
    r"\b(?:recalibr\w+|calibrar)\s+(?:o\s+)?(?:microfone|mic|audio)\b"
)


class ProcessadorComandos:
    """
    Interpreta comandos de texto e despacha para os servicos corretos.
    Nao possui estado proprio alem dos timers: os servicos sao injetados.

    Recebe o comando SEM a palavra de ativacao (removida em main()).

    Regras de latencia:
      - Acoes instantaneas (print, bloqueio, janelas, midia, volume) NUNCA
        esperam fala: executam primeiro; o feedback e um bip curto.
      - Fala so quando ha informacao a transmitir (confirmar() vira bip no
        modo silencioso).
    """

    _GATILHOS_ABRIR = frozenset(["abrir", "abre", "abra", "iniciar", "liga"])
    _GATILHOS_GOOGLE = frozenset(["pesquisar", "pesquisa", "buscar"])
    _GATILHOS_AMBIENTE = frozenset(["preparar ambiente", "modo trabalho", "vou trabalhar"])
    _GATILHOS_AUTO_RESP = frozenset([
        "verificar mensagens", "varrer whatsapp",
        "responda todo mundo", "forcar responder",
    ])
    _GATILHOS_LER_MENSAGENS = frozenset([
        "ler mensagens", "leia as mensagens", "leia mensagens",
        "mensagens nao lidas", "quais mensagens",
        "tenho mensagem", "tenho mensagens",
    ])
    _GATILHOS_DESLIGAR_PC = frozenset([
        "sono eterno", "desligar computador", "desligar o computador",
    ])
    _GATILHOS_CANCELAR_DESLIGAMENTO = frozenset([
        "cancelar desligamento", "cancelar o desligamento",
        "cancela desligamento", "cancela o desligamento",
        "abortar desligamento", "cancelar shutdown",
    ])
    _GATILHOS_DESLIGAR_JAIRO = frozenset([
        "desligar jairo", "desligar", "fechar sistema",
        "encerrar sistema", "desligar sistema",
    ])
    _GATILHOS_DIAG_WPP = frozenset([
        "diagnosticar whatsapp", "diagnostico do whatsapp", "diagnostico whatsapp",
    ])

    def __init__(
        self,
        wpp:      WhatsAppService,
        voz:      VozService,
        launcher: AppLauncher,
        midia:    ControleMidia,
        sistema:  ControleSistema,
        audio:    AudioPipeline,
        evento_saida: threading.Event,
    ) -> None:
        self._wpp          = wpp
        self._voz          = voz
        self._launcher     = launcher
        self._midia        = midia
        self._sistema      = sistema
        self._audio        = audio
        self._evento_saida = evento_saida

        self._lock_timers = threading.Lock()
        self._timers: dict[int, tuple[float, str, threading.Timer]] = {}
        self._seq_timer = 0

    def encerrar(self) -> None:
        """Cancela timers pendentes (chamado no encerramento)."""
        with self._lock_timers:
            for _, _, timer in self._timers.values():
                timer.cancel()
            self._timers.clear()

    # ----------------------------------------------------------
    #  ROTEAMENTO
    # ----------------------------------------------------------

    def processar(self, comando: str) -> Retorno:
        cmd = comando.strip()           # preserva acentos (nomes, termos de busca)
        cn  = _sem_acentos(cmd)         # usado apenas para casar gatilhos

        # --- SO A PALAVRA DE ATIVACAO ---
        if not cn:
            self._voz.confirmar("Estou aqui!")
            return Retorno.SEM_SINAL

        # --- DESLIGAMENTO DO PC / DO JAIRO ---
        if any(g in cn for g in self._GATILHOS_CANCELAR_DESLIGAMENTO):
            self._cancelar_desligamento()
            return Retorno.SEM_SINAL

        if cn in self._GATILHOS_DESLIGAR_PC:
            self._agendar_desligamento()
            return Retorno.SEM_SINAL

        if cn in self._GATILHOS_DESLIGAR_JAIRO:
            self._voz.falar("Desligando os sistemas. Ate mais!")
            self._evento_saida.set()
            raise SystemExit

        # --- ACOES INSTANTANEAS (sem fala) ---
        if _RE_PRINT.search(cn):
            return self._cmd_print(cn)

        if _RE_BLOQUEAR.search(cn) or cn in {"bloquear", "travar"}:
            if not self._sistema.bloquear():
                self._voz.falar("Nao consegui bloquear o computador.")
            return Retorno.SEM_SINAL  # sem bip: a sessao esta bloqueando

        if self._tentar_janelas(cn) or self._tentar_midia(cn):
            return Retorno.OK

        # --- CALCULADORA ---
        if self._tentar_calculo(cn):
            return Retorno.SEM_SINAL

        # --- TIMERS ---
        if self._tentar_timers(cn):
            return Retorno.SEM_SINAL

        # --- INFORMACOES E MODOS ---
        if self._tentar_info(cn):
            return Retorno.SEM_SINAL

        # --- WHATSAPP ---
        achado = _RE_BUSCAR_CONTATO.search(cn)
        if achado:
            self._cmd_buscar_contato(achado.group(1).strip())
            return Retorno.SEM_SINAL

        if any(g in cn for g in self._GATILHOS_DIAG_WPP):
            self._cmd_diagnosticar_wpp()
            return Retorno.SEM_SINAL

        if any(g in cn for g in self._GATILHOS_LER_MENSAGENS):
            self._cmd_ler_mensagens()
            return Retorno.SEM_SINAL

        if any(g in cn for g in self._GATILHOS_AUTO_RESP):
            self._cmd_varrer_mensagens()
            return Retorno.SEM_SINAL

        if "enviar mensagem para" in cn or "mande uma mensagem para" in cn:
            self._cmd_enviar_wpp(cmd)
            return Retorno.SEM_SINAL

        # --- ABRIR APLICATIVOS ---
        if any(g in cn for g in self._GATILHOS_ABRIR):
            for chave in self._launcher.chaves:
                if chave in cn:
                    self._voz.confirmar(f"Abrindo {chave}.")
                    if not self._launcher.abrir(chave):
                        self._voz.falar(
                            f"Nao encontrei o {chave}. Verifique o caminho."
                        )
                    return Retorno.SEM_SINAL

        # --- PREPARAR AMBIENTE ---
        if any(g in cn for g in self._GATILHOS_AMBIENTE):
            self._voz.confirmar("Preparando seu ambiente.")
            self._launcher.abrir("vs code")
            self._launcher.abrir("discord")
            return Retorno.SEM_SINAL

        # --- PESQUISA GOOGLE ---
        if any(g in cn for g in self._GATILHOS_GOOGLE):
            termo = self._extrair_termo(
                cmd, "jairo", "pesquisar", "pesquisa", "buscar", "no google"
            )
            if termo:
                self._voz.confirmar(f"Pesquisando {termo} no Google.")
                webbrowser.open(f"https://www.google.com/search?q={termo.replace(' ', '+')}")
            else:
                self._voz.falar("O que voce quer pesquisar?")
            return Retorno.SEM_SINAL

        # --- YOUTUBE ---
        if "youtube" in cn:
            termo = self._extrair_termo(
                cmd, "jairo", "youtube", "no youtube", "colocar", "tocar", "abrir"
            )
            if termo:
                self._voz.confirmar(f"Abrindo {termo} no YouTube.")
                webbrowser.open(
                    f"https://www.youtube.com/results?search_query={termo.replace(' ', '+')}"
                )
            else:
                self._voz.falar("O que voce quer no YouTube?")
            return Retorno.SEM_SINAL

        logger.debug("Comando nao mapeado: %r", cmd)
        return Retorno.DESCONHECIDO

    # ----------------------------------------------------------
    #  DESLIGAMENTO DO PC
    # ----------------------------------------------------------

    def _agendar_desligamento(self) -> None:
        self._voz.falar(
            "Desligando o computador em 1 minuto. "
            "Diga: Jairo, cancelar desligamento, para abortar."
        )
        try:
            subprocess.run(["shutdown", "/s", "/t", "60"], check=False, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Falha ao agendar desligamento: %s", exc)
            self._voz.falar("Nao consegui agendar o desligamento.")

    def _cancelar_desligamento(self) -> None:
        try:
            resultado = subprocess.run(
                ["shutdown", "/a"], capture_output=True, check=False, timeout=10
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Falha ao cancelar desligamento: %s", exc)
            self._voz.falar("Nao consegui cancelar o desligamento.")
            return

        if resultado.returncode == 0:
            logger.info("Desligamento cancelado.")
            self._voz.falar("Desligamento cancelado.")
        else:
            # 1116: nao havia desligamento em andamento.
            logger.info("shutdown /a retornou %d.", resultado.returncode)
            self._voz.falar("Nao ha nenhum desligamento agendado.")

    # ----------------------------------------------------------
    #  CAPTURA DE TELA E JANELAS
    # ----------------------------------------------------------

    def _cmd_print(self, cn: str) -> Retorno:
        """Executa a captura imediatamente; sem fala (so o bip final em main)."""
        if _RE_PRINT_CHEIA.search(cn):
            executou = self._sistema.captura_tela_cheia()   # Win+PrtSc: salva arquivo
        else:
            executou = self._sistema.captura_tela()         # Win+Shift+S: recorte
        if not executou:
            self._voz.falar("Nao consegui acionar a captura de tela.")
            return Retorno.SEM_SINAL
        return Retorno.OK

    def _tentar_janelas(self, cn: str) -> bool:
        """Retorna True se o comando era de gerenciamento de janelas."""
        if _RE_AREA_TRABALHO.search(cn):
            return self._sistema.mostrar_area_trabalho()
        if _RE_TROCAR_JANELA.search(cn):
            return self._sistema.trocar_janela()
        if _RE_FECHAR_JANELA.search(cn):
            return self._sistema.fechar_janela()
        if _RE_MAXIMIZAR.search(cn):
            return self._sistema.maximizar_janela()
        if _RE_MINIMIZAR.search(cn):
            return self._sistema.minimizar_janela()
        lado = _RE_JANELA_LADO.search(cn)
        if lado:
            if lado.group(1) == "esquerda":
                return self._sistema.janela_esquerda()
            return self._sistema.janela_direita()
        return False

    # ----------------------------------------------------------
    #  MIDIA E VOLUME (sem fala: execucao imediata + bip)
    # ----------------------------------------------------------

    def _tentar_midia(self, cn: str) -> bool:
        """Retorna True se o comando era de midia/volume (e foi tratado)."""
        m = _RE_VOL_SUBIR.search(cn)
        if m:
            self._midia.ajustar_volume(int(m.group(1)) if m.group(1) else 10)
            return True

        m = _RE_VOL_DESCER.search(cn)
        if m:
            self._midia.ajustar_volume(-(int(m.group(1)) if m.group(1) else 10))
            return True

        if _RE_VOL_CONSULTA.search(cn):
            atual = self._midia.obter_volume()
            if atual is None:
                self._voz.falar(
                    "Nao consigo ler o volume sem a biblioteca pycaw instalada."
                )
            else:
                self._voz.falar(f"O volume esta em {atual} por cento.")
            return True

        m = _RE_VOL_ABS.search(cn)
        if m:
            self._midia.definir_volume(int(m.group(1)))
            return True

        if _RE_DESMUTAR.search(cn):
            self._midia.mutar(False)
            return True

        if _RE_MUTAR.search(cn):
            self._midia.mutar(True)
            return True

        if _RE_PROXIMA.search(cn) or cn in _CMDS_PROXIMA:
            self._midia.proxima()
            return True

        if _RE_ANTERIOR.search(cn) or cn in _CMDS_ANTERIOR:
            self._midia.anterior()
            return True

        if cn in _CMDS_PLAY_PAUSE:
            self._midia.play_pause()
            return True

        return False

    # ----------------------------------------------------------
    #  TIMERS
    # ----------------------------------------------------------

    def _tentar_timers(self, cn: str) -> bool:
        if _RE_CANCELAR_TIMERS.search(cn):
            quantidade = self._cancelar_timers()
            if quantidade:
                self._voz.falar(
                    f"{_plural(quantidade, 'timer cancelado', 'timers cancelados')}."
                )
            else:
                self._voz.falar("Nao ha timers ativos.")
            return True

        if _RE_QUANTO_FALTA.search(cn):
            self._listar_timers()
            return True

        if _RE_TIMER.search(cn):
            duracao = _parse_duracao(cn)
            if duracao is None:
                self._voz.falar("Diga o tempo. Por exemplo: timer de 5 minutos.")
                return True
            segundos, fim = duracao
            if not 1 <= segundos <= 86400:
                self._voz.falar("Tempo invalido. Use entre 1 segundo e 24 horas.")
                return True
            rotulo = re.sub(r"^(?:de|do|da|para|pra|que)\s+", "", cn[fim:].strip())
            self._criar_timer(segundos, rotulo)
            self._voz.confirmar(f"Timer de {_formatar_duracao(segundos)} iniciado.")
            return True

        return False

    def _criar_timer(self, segundos: int, rotulo: str) -> None:
        with self._lock_timers:
            self._seq_timer += 1
            tid = self._seq_timer
            timer = threading.Timer(segundos, self._disparar_timer, args=(tid,))
            timer.daemon = True
            timer.name = f"Timer-{tid}"
            self._timers[tid] = (monotonic() + segundos, rotulo, timer)
        timer.start()
        logger.info("Timer %d criado: %ds (%s).", tid, segundos, rotulo or "sem rotulo")

    def _disparar_timer(self, tid: int) -> None:
        with self._lock_timers:
            item = self._timers.pop(tid, None)
        if item is None:
            return
        _, rotulo, _ = item
        Sinal.alerta()
        self._voz.falar(f"Tempo esgotado. {rotulo}".strip())

    def _cancelar_timers(self) -> int:
        with self._lock_timers:
            quantidade = len(self._timers)
            for _, _, timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
        return quantidade

    def _listar_timers(self) -> None:
        with self._lock_timers:
            itens = sorted(self._timers.values(), key=lambda i: i[0])
        if not itens:
            self._voz.falar("Nao ha timers ativos.")
            return
        partes = []
        for fim, rotulo, _ in itens:
            restante = max(1, round(fim - monotonic()))
            texto = f"Faltam {_formatar_duracao(restante)}"
            if rotulo:
                texto += f" para {rotulo}"
            partes.append(texto + ".")
        self._voz.falar(" ".join(partes))

    # ----------------------------------------------------------
    #  INFORMACOES E MODOS
    # ----------------------------------------------------------

    def _tentar_calculo(self, cn: str) -> bool:
        """Responde so o resultado ("29"). False se o comando nao for uma conta."""
        try:
            resultado = _calcular(cn)
        except ZeroDivisionError:
            self._voz.falar("Não é possível dividir por zero.")
            return True
        if resultado is None:
            return False
        logger.info("Calculo: %r -> %s", cn, resultado)
        self._voz.falar(resultado)
        return True

    def _tentar_info(self, cn: str) -> bool:
        if _RE_RECALIBRAR.search(cn):
            self._voz.confirmar("Calibrando o microfone. Fique em silencio.")
            self._audio.solicitar_recalibracao()
            return True

        if any(g in cn for g in ("que horas", "horas sao")):
            self._voz.falar(f"São {time.strftime('%H horas e %M minutos')}.")
            return True

        if _RE_DATA.search(cn):
            agora = datetime.now()
            self._voz.falar(
                f"Hoje é {_DIAS_SEMANA[agora.weekday()]}, {agora.day} de "
                f"{_MESES[agora.month - 1]} de {agora.year}."
            )
            return True

        if cn in _CMDS_REPETIR:
            self._voz.falar(self._voz.ultima_fala or "Ainda não falei nada.")
            return True

        if cn in _CMDS_AJUDA or "quais comandos" in cn or "o que voce faz" in cn:
            logger.info(_AJUDA_COMPLETA)
            self._voz.falar(_AJUDA_FALADA)
            return True

        if "modo silencioso" in cn or cn in {"so bip", "so bipe", "so bipes"}:
            self._voz.falar("Modo silencioso ativado. Vou responder com bipes.")
            self._voz.silencioso = True
            return True

        if "modo falante" in cn or cn in {"pode falar", "voltar a falar"}:
            self._voz.silencioso = False
            self._voz.falar("Modo falante ativado.")
            return True

        return False

    # ----------------------------------------------------------
    #  DIAGNOSTICO DO WHATSAPP
    # ----------------------------------------------------------

    def _cmd_diagnosticar_wpp(self) -> None:
        self._voz.confirmar("Gerando diagnostico do WhatsApp.")
        future = self._wpp.diagnosticar_whatsapp()
        future.add_done_callback(self._ao_diagnosticar)

    def _ao_diagnosticar(self, f: Future) -> None:
        erro = f.exception()
        if erro is not None:
            logger.error("Diagnostico falhou: %s", erro)
            self._voz.falar("Nao consegui gerar o diagnostico do WhatsApp.")
            return
        logger.info("Diagnostico salvo em %s", f.result())
        self._voz.falar("Diagnostico salvo na pasta do projeto.")

    # ----------------------------------------------------------
    #  WHATSAPP
    # ----------------------------------------------------------

    def _cmd_ler_mensagens(self) -> None:
        self._voz.confirmar("Verificando suas conversas.")
        future = self._wpp.listar_nao_lidos()
        future.add_done_callback(self._ao_listar_nao_lidos)

    def _ao_listar_nao_lidos(self, f: Future) -> None:
        # Executa na thread do worker: so enfileira fala, nunca fala direto.
        erro = f.exception()
        if erro is not None:
            logger.error("Leitura de nao lidas falhou: %s", erro)
            self._voz.falar("Nao consegui acessar o WhatsApp agora.")
            return

        chats: list[ChatNaoLido] = f.result()
        if not chats:
            self._voz.falar("Voce nao tem mensagens nao lidas.")
            return

        total = sum(c.quantidade for c in chats)
        partes = [
            f"Voce tem {_plural(total, 'mensagem nao lida', 'mensagens nao lidas')} "
            f"em {_plural(len(chats), 'conversa', 'conversas')}."
        ]

        limite = max(1, CFG.max_chats_leitura)
        for chat in chats[:limite]:
            nome = _limpar_para_fala(chat.nome, 40)
            previa = _limpar_para_fala(chat.previa)
            if previa:
                partes.append(f"{nome}: {previa}.")
            else:
                partes.append(
                    f"{nome}, {_plural(chat.quantidade, 'mensagem', 'mensagens')}."
                )

        restantes = len(chats) - limite
        if restantes > 0:
            partes.append(f"E mais {_plural(restantes, 'conversa', 'conversas')}.")

        self._voz.falar(" ".join(partes))

    def _cmd_varrer_mensagens(self) -> None:
        self._voz.confirmar("Verificando mensagens nao lidas agora.")
        logger.info("Varredura manual do auto-responder solicitada.")
        future = self._wpp.varrer_agora()
        future.add_done_callback(self._ao_concluir_varredura)

    def _ao_concluir_varredura(self, f: Future) -> None:
        erro = f.exception()
        if erro is not None:
            logger.error("Varredura manual falhou: %s", erro)
            self._voz.falar("Nao consegui varrer o WhatsApp agora.")
            return

        r: ResultadoVarredura = f.result()
        avisos = {
            "driver_inativo": "O WhatsApp nao esta conectado.",
            "sem_permitidos": "Sua lista de contatos permitidos esta vazia.",
            "erro_varredura": "Houve um erro ao ler a lista de conversas.",
        }
        if r.detalhe in avisos:
            self._voz.falar(avisos[r.detalhe])
        elif r.respondidos > 0:
            self._voz.falar(
                f"Varredura concluida. Respondi {_plural(r.respondidos, 'conversa', 'conversas')}."
            )
        elif r.nao_lidos > 0:
            self._voz.falar(
                "Ha conversas nao lidas, mas nenhuma pendente de contato permitido."
            )
        else:
            self._voz.falar("Nenhuma conversa nao lida.")

    def _cmd_buscar_contato(self, nome: str) -> None:
        """Lista os contatos ativos que casam com o nome (util para achar entradas antigas)."""
        achados = self._wpp.buscar_contatos(nome)
        if not achados:
            self._voz.falar(f"Não encontrei contato com o nome {nome}.")
            return
        partes = [f"{c.nome}, final {' '.join(c.numero[-4:])}" for c in achados]
        self._voz.falar(
            f"Encontrei {_plural(len(achados), 'contato', 'contatos')}: "
            + "; ".join(partes) + "."
        )

    def _cmd_enviar_wpp(self, cmd: str) -> None:
        partes = cmd.split("dizendo", 1)
        if len(partes) < 2:
            self._voz.falar("Diga: enviar mensagem para [nome] dizendo [texto].")
            return

        nome = self._extrair_termo(
            partes[0], "jairo", "enviar mensagem para", "mande uma mensagem para"
        )
        mensagem = partes[1].strip()

        if not nome or not mensagem:
            self._voz.falar("Nao consegui identificar o destinatario ou a mensagem.")
            return

        # Resolve o contato ANTES de enviar: se houver mais de um, pede o nome completo.
        try:
            contato = self._wpp.resolver_contato(nome)
        except ContatoAmbiguo as exc:
            nomes = " ou ".join(c.nome for c in exc.candidatos)
            logger.warning("Contato ambiguo para %r: %s", nome, nomes)
            self._voz.falar(
                f"Encontrei {_plural(len(exc.candidatos), 'contato', 'contatos')} "
                f"para {nome}: {nomes}. Diga o nome completo."
            )
            return
        except LookupError:
            self._voz.falar(f"Não encontrei {nome} na agenda.")
            return

        self._voz.confirmar(f"Enviando mensagem para {contato.nome}.")
        future = self._wpp.enviar_para(contato, mensagem)

        def _callback(f: Future) -> None:
            resultado: ResultadoEnvio = f.result()
            if resultado.estado == EstadoEnvio.CONFIRMADO_NA_CONVERSA:
                self._voz.confirmar(f"Mensagem confirmada para {contato.nome}.")
            elif resultado.estado == EstadoEnvio.INSERIDO_NA_INTERFACE:
                self._voz.confirmar(
                    f"Mensagem enviada para {contato.nome}, aguardando confirmacao."
                )
            else:
                self._voz.falar(
                    f"Nao consegui enviar para {contato.nome}. {resultado.detalhe}"
                )
                logger.error(
                    "Envio falhou para %s: %s", contato.nome, resultado.detalhe
                )

        future.add_done_callback(_callback)

    @staticmethod
    def _extrair_termo(texto: str, *remover: str) -> str:
        for p in remover:
            texto = texto.replace(p, "")
        return texto.strip()


# ==============================================================
#  DIAGNOSTICO DE VOZ
# ==============================================================

def _testar_voz() -> None:
    """python jairo.py --testar-voz : testa os dois backends de TTS isoladamente."""
    estado = EstadoFala()
    voz = VozService(estado)
    voz.iniciar()
    voz.diagnostico()


def _testar_mic() -> None:
    """
    python jairo.py --testar-mic : mede o nivel do ambiente e da sua fala e
    sugere um valor de JAIRO_ENERGY. Usa RMS int16, a mesma escala do
    energy_threshold do speech_recognition.
    """
    def medir(mic, segundos: float) -> list[float]:
        niveis: list[float] = []
        fim = monotonic() + segundos
        while monotonic() < fim:
            bloco = array("h")
            bloco.frombytes(mic.stream.read(mic.CHUNK))
            if bloco:
                niveis.append(math.sqrt(sum(a * a for a in bloco) / len(bloco)))
        return niveis

    def percentil(valores: list[float], p: float) -> float:
        ordenados = sorted(valores)
        return ordenados[min(len(ordenados) - 1, int(len(ordenados) * p))]

    try:
        with sr.Microphone() as mic:
            if mic.SAMPLE_WIDTH != 2:
                print("Formato de audio inesperado; teste indisponivel.")
                return
            print("Fique em SILENCIO por 3 segundos...")
            silencio = medir(mic, 3.0)
            print("Agora diga, no volume e na distancia normais: "
                  "'Jairo, que horas sao' (4 segundos)...")
            fala = medir(mic, 4.0)
    except OSError as exc:
        print(f"Microfone nao encontrado: {exc}")
        return

    ruido = percentil(silencio, 0.95)
    voz_media = percentil(fala, 0.75)
    voz_pico = max(fala)
    sugestao = int(max(ruido * 1.3, math.sqrt(max(ruido, 1.0) * max(voz_media, 1.0))))

    print(f"\nRuido de fundo (p95): {ruido:.0f}")
    print(f"Sua fala (p75):       {voz_media:.0f}   pico: {voz_pico:.0f}")
    print(f"\nSugestao: JAIRO_ENERGY={sugestao}  (coloque no .env)")
    if voz_media < ruido * 2:
        print("Sinal fraco em relacao ao ruido. Aumente o ganho de entrada em "
              "Configuracoes > Sistema > Som > Entrada, desative 'Melhorias de audio' "
              "do microfone ou aproxime-o da boca.")
    elif voz_pico < 800:
        print("Sua fala chega baixa ao PC. O ganho automatico do Jairo ajuda; "
              "aumentar o volume de entrada no Windows ajuda mais.")


# ==============================================================
#  MAIN
# ==============================================================

_COMANDOS_STANDBY = frozenset([
    "silenciar", "modo espera", "descansar", "dormir", "dormi",
])
_COMANDOS_ACORDAR = frozenset([
    "", "ativar", "acordar", "voltar", "volta",
])


def main() -> None:
    if "--testar-voz" in sys.argv[1:]:
        _testar_voz()
        return
    if "--testar-mic" in sys.argv[1:]:
        _testar_mic()
        return

    logger.info("=" * 52)
    logger.info("         J A I R O  v 2 . 0")
    logger.info("=" * 52)
    if CFG.modo_debug:
        logger.warning("MODO_DEBUG ativo. Brave sera aberto visivelmente.")
    if CFG.exigir_wake_word:
        logger.info("Palavra de ativacao obrigatoria: %s", ", ".join(CFG.wake_aliases))
    else:
        logger.warning("Palavra de ativacao DESATIVADA: todo audio vira comando.")

    evento_saida = threading.Event()
    estado_fala  = EstadoFala()

    # Inicializa servicos.
    voz      = VozService(estado_fala)
    voz.iniciar()

    try:
        agenda = Agenda(CFG.caminho_contatos)
    except ValueError as exc:
        logger.critical("Erro na agenda de contatos: %s. Encerrando.", exc)
        sys.exit(1)

    wpp      = WhatsAppService(agenda, evento_saida)
    launcher = AppLauncher()
    midia    = ControleMidia()
    sistema  = ControleSistema()
    audio    = AudioPipeline(evento_saida, estado_fala)
    proc     = ProcessadorComandos(wpp, voz, launcher, midia, sistema, audio, evento_saida)

    audio.configurar()

    # O Brave/WhatsApp sobe em paralelo com a calibracao do microfone.
    wpp.iniciar()
    audio.calibrar()
    audio.iniciar()

    voz.confirmar("Jairo pronto. Pode chamar.")
    logger.info("Diga 'Jairo, ajuda' para ouvir o que posso fazer.")

    jairo_ativo   = True
    ultimo_status: str | None = None

    try:
        while not evento_saida.is_set():
            # --- Consume fila de voz na main thread (Windows COM safe) ---
            voz.consumir_fila()

            if jairo_ativo:
                if ultimo_status != "ouvindo":
                    print(
                        f"\r[{time.strftime('%H:%M:%S')}] >> Ouvindo...                    ",
                        end="",
                        flush=True,
                    )
                    ultimo_status = "ouvindo"
                try:
                    comando = audio.cmd_queue.get(timeout=0.2)
                    print("\r" + " " * 60 + "\r", end="", flush=True)
                    ultimo_status = None

                    alvo = _extrair_comando(comando)
                    if alvo is None:
                        logger.debug("Ignorado (sem palavra de ativacao): %r", comando)
                        continue

                    logger.info("Comando recebido: %r", alvo or comando)

                    if _sem_acentos(alvo) in _COMANDOS_STANDBY:
                        voz.confirmar("Entrando em modo de espera.")
                        jairo_ativo = False
                        logger.info("STANDBY ativo.")
                        continue

                    retorno = proc.processar(alvo)
                    if retorno is Retorno.OK:
                        Sinal.ok()
                    elif retorno is Retorno.DESCONHECIDO:
                        logger.info("Comando nao reconhecido: %r", alvo)
                        Sinal.erro()

                except queue.Empty:
                    pass

            else:
                if ultimo_status != "standby":
                    print(
                        f"\r[{time.strftime('%H:%M:%S')}] >> Standby...                    ",
                        end="",
                        flush=True,
                    )
                    ultimo_status = "standby"
                try:
                    fala = audio.cmd_queue.get(timeout=0.5)
                    alvo = _extrair_comando(fala)
                    if alvo is not None and _sem_acentos(alvo) in _COMANDOS_ACORDAR:
                        jairo_ativo = True
                        ultimo_status = None
                        voz.confirmar("Estou de volta!")
                        logger.info("Modo ATIVO retomado.")
                except queue.Empty:
                    pass

    except SystemExit:
        logger.info("SystemExit recebido. Encerrando com seguranca.")
    except KeyboardInterrupt:
        print()
        logger.info("Interrompido pelo usuario.")
        evento_saida.set()
    except Exception:
        logger.exception("Excecao nao tratada no loop principal.")
    finally:
        evento_saida.set()
        proc.encerrar()
        voz.consumir_fila()  # fala a despedida pendente antes de encerrar
        logger.info("Aguardando encerramento dos servicos...")
        wpp.encerrar()
        audio.encerrar()
        logger.info("Jairo encerrado.")


if __name__ == "__main__":
    main()
