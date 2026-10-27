#!/usr/bin/env python3
"""
userbot.py — Pipeline Assíncrono Multi-LLM (Groq Primary + DeepSeek Fallback + Gemini Validation)
Monitora canais de ofertas no Telegram via protocolo MTProto, classifica promoções com IA
e sincroniza os resultados em tempo real com o Google Sheets.
"""

import sys
sys.dont_write_bytecode = True
import os
import asyncio
import logging
import json
import hashlib
import re
import datetime
from typing import Optional, List
import pytz
import aiohttp
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import gspread
from gspread.exceptions import WorksheetNotFound

# -------------------------
# Logging / Config
# -------------------------
logging.basicConfig(
    format='[%(levelname) 5s/%(asctime)s] userbot: %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("userbot")

# Timezone
RIO_TZ = pytz.timezone('America/Sao_Paulo')

# Variáveis de Ambiente (Railway / .env)
API_ID = int(os.environ.get('API_ID', '0'))
API_HASH = os.environ.get('API_HASH', '')
SESSION_NAME = os.environ.get('SESSION_NAME', 'promo_userbot')
STRING_SESSION = os.environ.get('STRING_SESSION', '')

TARGET_GROUP_STRING = os.environ.get('TARGET_GROUP', '')
TARGET_GROUP_LIST = [tg.strip() for tg in TARGET_GROUP_STRING.split(',') if tg.strip()] if TARGET_GROUP_STRING else []

NOTIFICATION_CHAT = os.environ.get('NOTIFICATION_CHAT', 'me')

# API Keys dos Provedores de IA
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

# Gemini Quota (para controle diário de custos/requisições)
GEMINI_QUOTA_MAX = int(os.environ.get('GEMINI_QUOTA_MAX', 20))
_gemini_calls_today = 0
_last_quota_reset_date = datetime.date.today()

# -------------------------
# GSpread Globals & Cache
# -------------------------
_spreadsheet: Optional[gspread.Spreadsheet] = None
RECENT_HASHES = set()
RECENT_DURATION = 300  # 5 minutos para deduplicação em memória

HEADERS_PENDING = ["Data/Hora", "Mensagem", "ID_Interno", "processed_timestamp", "processor"]
HEADERS_PROCESSED = ["Data/Hora", "Mensagem", "Classificacao", "Categoria", "processed_timestamp", "processor"]

# -------------------------
# Prompts
# -------------------------
PROMPT_CLASSIFY_SINGLE = """
Você é um bot classificador de ofertas. Sua tarefa é analisar o texto da mensagem e decidir se a oferta é interessante ('SIM') ou não ('NAO') baseado nos seguintes critérios:
1. O preço deve ser bom (desconto real) e o produto deve ser de valor (eletrônicos, eletrodomésticos, produtos esportivos ou de marca premium).
2. Ignore produtos genéricos, alimentos, cartões de presente, serviços ou itens de baixo valor.
3. Se for 'SIM', forneça uma categoria clara e concisa (ex: 'Hardware/Embarcados', 'Smartphone', 'Notebook', 'Periféricos', 'IoT/Automação', 'Outro').
4. Se for 'NAO', use a categoria 'Ignorar'.

Você DEVE retornar APENAS o bloco JSON (sem texto introdutório, explicações ou código markdown).

Exemplo de saída 'SIM':
{"classificacao": "SIM", "categoria": "Smartphone"}

Exemplo de saída 'NAO':
{"classificacao": "NAO", "categoria": "Ignorar"}

Mensagem a analisar: 
"""

# -------------------------
# Instanciação do Cliente Telegram (MTProto)
# -------------------------
if STRING_SESSION:
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
else:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


# -------------------------
# Helpers de GSpread (Síncronos, executados via asyncio.to_thread)
# -------------------------
def setup_gspread_sync() -> bool:
    """Configura a conexão com o Google Sheets."""
    global _spreadsheet
    try:
        creds_file = os.environ.get('GSPREAD_CREDENTIALS', 'gspread_credentials.json')
        if os.path.exists(creds_file):
            gc = gspread.service_account(filename=creds_file)
        else:
            gc = gspread.service_account()

        spreadsheet_ref = os.environ.get('SPREADSHEET_URL', 'Monitoramento de Promocoes')
        if spreadsheet_ref.startswith('http://') or spreadsheet_ref.startswith('https://'):
            _spreadsheet = gc.open_by_url(spreadsheet_ref)
        else:
            _spreadsheet = gc.open(spreadsheet_ref)

        logger.info(f"Conexão GSpread estabelecida com: {_spreadsheet.title}")
        return True
    except Exception as e:
        logger.warning(f"GSpread não configurado ou indisponível: {e}")
        return False


def get_or_create_worksheet_sync(title: str, headers: List[str]) -> Optional[gspread.Worksheet]:
    """Obtém ou cria uma aba da planilha e garante o cabeçalho."""
    if not _spreadsheet:
        return None
    try:
        ws = _spreadsheet.worksheet(title)
    except WorksheetNotFound:
        logger.info(f"Worksheet '{title}' não encontrada, criando...")
        ws = _spreadsheet.add_worksheet(title=title, rows=100, cols=len(headers))

    current_header = ws.row_values(1)
    if current_header != headers:
        ws.update([headers], range_name='A1')
    return ws


# -------------------------
# Helpers Assíncronos (HTTP & JSON)
# -------------------------
async def get_http_session() -> aiohttp.ClientSession:
    """Retorna uma sessão HTTP global reutilizável."""
    if not hasattr(get_http_session, 'session') or get_http_session.session.closed:
        get_http_session.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    return get_http_session.session


def try_extract_json(text: str) -> Optional[dict]:
    """Extrai um objeto JSON válido da resposta textual do modelo."""
    try:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1).strip())

        text = text.strip()
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])

        return None
    except json.JSONDecodeError:
        return None
    except Exception as e:
        logger.warning(f"Erro inesperado na extração de JSON: {e}")
        return None


# -------------------------
# Chamadas de API (Multi-LLM Pipeline)
# -------------------------
async def call_groq_single(message: str) -> Optional[dict]:
    """Tier 1: API Groq (Llama 3.1 8B Instant)."""
    if not GROQ_API_KEY:
        logger.debug("GROQ_API_KEY não configurada. Pulando Groq.")
        return None

    session = await get_http_session()
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": PROMPT_CLASSIFY_SINGLE + message}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }

    try:
        async with session.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                logger.error(f"Groq API Error {resp.status}: {data.get('error', {}).get('message', 'Erro desconhecido')}")
                return None

            response_text = data['choices'][0]['message']['content']
            return try_extract_json(response_text)
    except aiohttp.ClientError as e:
        logger.warning(f"Erro de conexão Groq: {e}")
        return None
    except Exception as e:
        logger.exception(f"Erro inesperado Groq: {e}")
        return None


async def call_deepseek_single(message: str) -> Optional[dict]:
    """Tier 2 (Fallback): API DeepSeek Chat."""
    if not DEEPSEEK_API_KEY:
        logger.debug("DEEPSEEK_API_KEY não configurada. Pulando DeepSeek.")
        return None

    session = await get_http_session()
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": PROMPT_CLASSIFY_SINGLE + message}],
        "temperature": 0.1,
    }

    try:
        async with session.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                logger.error(f"DeepSeek API Error {resp.status}: {data.get('error', {}).get('message', 'Erro desconhecido')}")
                return None

            response_text = data['choices'][0]['message']['content']
            return try_extract_json(response_text)
    except aiohttp.ClientError as e:
        logger.warning(f"Erro de conexão DeepSeek: {e}")
        return None
    except Exception as e:
        logger.exception(f"Erro inesperado DeepSeek: {e}")
        return None


async def call_gemini_single(message: str) -> Optional[dict]:
    """Tier 3 (Fallback com Controle de Cota): Google Gemini 1.5 Flash."""
    global _gemini_calls_today, _last_quota_reset_date

    if _last_quota_reset_date < datetime.date.today():
        _gemini_calls_today = 0
        _last_quota_reset_date = datetime.date.today()

    if _gemini_calls_today >= GEMINI_QUOTA_MAX:
        logger.warning(f"Cota diária do Gemini ({GEMINI_QUOTA_MAX}) atingida. Pulando.")
        return None

    if not GEMINI_API_KEY:
        logger.debug("GEMINI_API_KEY não configurada. Pulando Gemini.")
        return None

    session = await get_http_session()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": PROMPT_CLASSIFY_SINGLE + message}]}
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "object",
                "properties": {
                    "classificacao": {"type": "string", "enum": ["SIM", "NAO"]},
                    "categoria": {"type": "string"}
                },
                "required": ["classificacao", "categoria"]
            }
        }
    }

    try:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                logger.error(f"Gemini API Error {resp.status}: {data.get('error', {}).get('message', 'Erro desconhecido')}")
                return None

            _gemini_calls_today += 1
            response_text = data['candidates'][0]['content']['parts'][0]['text']
            return try_extract_json(response_text)
    except aiohttp.ClientError as e:
        logger.warning(f"Erro de conexão Gemini: {e}")
        return None
    except Exception as e:
        logger.exception(f"Erro inesperado Gemini: {e}")
        return None


# -------------------------
# Lógica de Processamento e Workers
# -------------------------
async def process_message_single(original_text: str, internal_id: str, received_iso: str) -> bool:
    """Classifica a mensagem usando a cadeia de fallback de APIs."""
    logger.info(f"Iniciando classificação para ID: {internal_id}")
    processed_time = datetime.datetime.now(RIO_TZ).isoformat()
    classificacao = "NAO"
    categoria = "Erro na Classificacao"
    source = "Fallback"

    # 1. Groq (Tier 1: Velocidade)
    result = await call_groq_single(original_text)
    if result:
        source = "Groq"

    # 2. DeepSeek (Tier 2: Custo-Benefício)
    if not result:
        result = await call_deepseek_single(original_text)
        if result:
            source = "DeepSeek"

    # 3. Gemini (Tier 3: Precisão com Cota Limitada)
    if not result:
        result = await call_gemini_single(original_text)
        if result:
            source = "Gemini"

    if result:
        classificacao = str(result.get('classificacao', 'NAO')).upper()
        categoria = str(result.get('categoria', 'Outro'))

        if classificacao not in ["SIM", "NAO"]:
            classificacao = "NAO"
            categoria = "Resultado Inválido"
            source += " (JSON Inválido)"

    logger.info(f"ID {internal_id} classificado como: {classificacao} ({categoria}) via {source}")

    # Persistência no Google Sheets
    try:
        if _spreadsheet:
            ws_proc = await asyncio.to_thread(get_or_create_worksheet_sync, "PROCESSAMENTO_COMPLETO", HEADERS_PROCESSED)
            if ws_proc:
                row = [received_iso, original_text, classificacao, categoria, processed_time, source]
                await asyncio.to_thread(ws_proc.append_row, row)

            ws_pending = await asyncio.to_thread(get_or_create_worksheet_sync, "PENDING_CLASSIFICATION", HEADERS_PENDING)
            if ws_pending:
                all_vals = await asyncio.to_thread(ws_pending.get_all_values)
                for idx, r in enumerate(all_vals[1:], start=2):
                    if len(r) >= 3 and r[2] == internal_id:
                        await asyncio.to_thread(ws_pending.update, [[processed_time, source]], f"D{idx}:E{idx}")
                        break
    except Exception as e:
        logger.warning(f"Erro ao atualizar planilha no Google Sheets: {e}")

    # Envio de Notificação (Apenas se for 'SIM')
    if classificacao == "SIM" and NOTIFICATION_CHAT:
        notification_text = f"🔥 **OFERTA DETECTADA ({source})** 🔥\n\n**Categoria:** {categoria}\n\n**Texto:**\n{original_text}"
        try:
            await client.send_message(NOTIFICATION_CHAT, notification_text, link_preview=False, parse_mode='markdown')
            logger.info(f"Notificação enviada para ID: {internal_id}")
        except Exception as e:
            logger.error(f"Falha ao enviar notificação no Telegram: {e}")

    return classificacao == "SIM"


async def pending_reprocessor():
    """Worker de segurança: reprocessa mensagens pendentes na planilha."""
    while True:
        await asyncio.sleep(30)
        if not _spreadsheet:
            continue

        try:
            ws_pending = await asyncio.to_thread(get_or_create_worksheet_sync, "PENDING_CLASSIFICATION", HEADERS_PENDING)
            if not ws_pending:
                continue

            all_vals = await asyncio.to_thread(ws_pending.get_all_values)
            unprocessed_rows = [row for row in all_vals[1:] if len(row) < 4 or not row[3]]

            if unprocessed_rows:
                logger.info(f"Reprocessor: {len(unprocessed_rows)} mensagens pendentes encontradas.")
                for row in unprocessed_rows:
                    if len(row) >= 3:
                        asyncio.create_task(process_message_single(row[1], row[2], row[0]))
        except Exception as e:
            logger.warning(f"Erro no loop do pending_reprocessor: {e}")


# -------------------------
# Deduplicação por Hash SHA-256
# -------------------------
def generate_internal_id(text: str) -> str:
    hasher = hashlib.sha256()
    hasher.update(text.encode('utf-8'))
    return hasher.hexdigest()[:16]


def is_recent(internal_id: str) -> bool:
    return internal_id in RECENT_HASHES


def add_to_recent(internal_id: str):
    RECENT_HASHES.add(internal_id)
    asyncio.get_running_loop().call_later(RECENT_DURATION, lambda: RECENT_HASHES.discard(internal_id))


# -------------------------
# Handler de Eventos do Telegram
# -------------------------
@client.on(events.NewMessage(chats=TARGET_GROUP_LIST if TARGET_GROUP_LIST else None))
async def handler(event):
    if event.is_reply or event.is_private or not event.text:
        return

    text = event.text.strip()
    internal_id = generate_internal_id(text)
    now_iso = datetime.datetime.now(RIO_TZ).isoformat()

    if is_recent(internal_id):
        logger.debug(f"Mensagem duplicada ignorada. ID: {internal_id}")
        return

    add_to_recent(internal_id)

    try:
        if _spreadsheet:
            ws_pending = await asyncio.to_thread(get_or_create_worksheet_sync, "PENDING_CLASSIFICATION", HEADERS_PENDING)
            if ws_pending:
                await asyncio.to_thread(ws_pending.append_row, [now_iso, text, internal_id, "", ""])

        asyncio.create_task(process_message_single(text, internal_id, now_iso))
    except Exception as e:
        logger.exception(f"Erro no handler de mensagem: {e}")


# -------------------------
# Inicialização Principal
# -------------------------
async def main():
    if not API_ID or not API_HASH:
        logger.error("API_ID e API_HASH não configurados! Configure o arquivo .env ou as variáveis de ambiente.")
        return

    ok = await asyncio.to_thread(setup_gspread_sync)
    if ok:
        try:
            await asyncio.to_thread(get_or_create_worksheet_sync, "PENDING_CLASSIFICATION", HEADERS_PENDING)
            await asyncio.to_thread(get_or_create_worksheet_sync, "PROCESSAMENTO_COMPLETO", HEADERS_PROCESSED)
        except Exception:
            pass

    asyncio.create_task(pending_reprocessor())

    await get_http_session()
    await client.start()
    if not await client.is_user_authorized():
        logger.error("Sessão do Telegram não autorizada. Execute logar.py primeiro.")
        return

    logger.info("Userbot autorizado e monitorando canais em tempo real.")
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Userbot encerrado pelo usuário.")
    except Exception as e:
        logger.error(f"Erro fatal na execução: {e}")
