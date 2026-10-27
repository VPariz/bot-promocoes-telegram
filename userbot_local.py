import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import os
import asyncio
import datetime
import pytz
from telethon import TelegramClient, events
from google import genai
import gspread

# --- 1. CONFIGURAÇÕES DE VARIÁVEIS ---
# Carregadas de variáveis de ambiente com segurança (sem chaves expostas no código)
API_ID = int(os.environ.get('API_ID', '0'))
API_HASH = os.environ.get('API_HASH', '')

SESSION_NAME = os.environ.get('SESSION_NAME', 'promo_userbot')
TARGET_GROUP_ENV = os.environ.get('TARGET_GROUP', '')
TARGET_GROUP = [g.strip() for g in TARGET_GROUP_ENV.split(',') if g.strip()] if TARGET_GROUP_ENV else []
MY_PRIVATE_CHAT = os.environ.get('NOTIFICATION_CHAT', 'me')

# Chaves de API obtidas de variáveis de ambiente
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GSPREAD_CREDENTIALS = os.environ.get('GSPREAD_CREDENTIALS', 'gspread_credentials.json') 
SPREADSHEET_NAME = os.environ.get('SPREADSHEET_NAME', 'Monitoramento de Promocoes')

if not API_ID or not API_HASH:
    print("⚠️ ATENÇÃO: Defina as variáveis de ambiente API_ID e API_HASH antes de iniciar.")

# --- 2. PROMPT BASE PARA A IA ---
PROMPT_BASE = """
Você é um analista de ofertas e seu objetivo é classificar mensagens de promoção com base em uma lista de interesses.
Analise o texto abaixo e responda APENAS com 'SIM' ou 'NAO' (em maiúsculas).
Não inclua nenhum outro texto, explicação ou pontuação.

Interesses Personalizados: {interesses}
Texto da Promoção para Análise:
---
{mensagem_texto}
---
Classificação (SIM/NAO):
"""

# LISTA DE INTERESSES (Configurável para qualquer nicho de produtos)
INTERESSES_LIST = """
Hardware & Embarcados: Placas de desenvolvimento (ESP32, STM32, Raspberry Pi, Arduino), módulos de comunicação, componentes eletrônicos, kits de robótica e ferramentas de bancada/solda.

Computação & Periféricos: Notebooks de alta performance, monitores IPS/OLED, teclados mecânicos, SSDs NVMe e placas de vídeo (GPUs) com descontos significativos.

Dispositivos Inteligentes & Wearables: Smartwatches com GPS, sensores IoT para automação residencial (Zigbee/Wi-Fi) e periféricos com desconto igual ou superior a 30%.
"""

# Fuso horário para garantir a comparação de datas correta
RIO_TZ = pytz.timezone('America/Sao_Paulo') 

# --- 3. INICIALIZAÇÃO DE CLIENTES ---
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
gemini_client = None
worksheet = None


def setup_gspread():
    global worksheet
    try:
        if not os.path.exists(GSPREAD_CREDENTIALS):
            print(f"Aviso: Arquivo '{GSPREAD_CREDENTIALS}' não encontrado. Sincronização Google Sheets desativada.")
            return False
        gc = gspread.service_account(filename=GSPREAD_CREDENTIALS)
        worksheet = gc.open(SPREADSHEET_NAME).sheet1
        return True
    except Exception as e:
        print(f"Erro ao configurar o gspread: {e}")
        return False


def setup_gemini():
    global gemini_client
    try:
        if not GEMINI_API_KEY:
            print("Aviso: GEMINI_API_KEY não informada no ambiente.")
            return False
        client_obj = genai.Client(api_key=GEMINI_API_KEY)
        gemini_client = client_obj
        print("Cliente Gemini configurado com sucesso!")
        return True
    except Exception as e:
        print(f"Erro ao configurar o cliente Gemini: {e}")
        return False


# --- 4. FUNÇÕES DO USERBOT ---

async def sheet_cleaner():
    """Tarefa assíncrona que roda em loop para deletar promoções antigas (após 48h)."""
    if not worksheet:
        return
        
    WAIT_TIME_SECONDS = 48 * 3600 
    
    while True:
        await asyncio.sleep(WAIT_TIME_SECONDS)
        
        try:
            data = worksheet.get_all_values()
            if len(data) <= 1:
                print("Planilha vazia ou apenas com cabeçalho. Nada para limpar.")
                continue

            rows_to_check = data[1:]
            TIMESTAMP_COLUMN_INDEX = 3 
            
            now_utc = datetime.datetime.now(pytz.utc)
            rows_to_delete = []

            for i in range(len(rows_to_check) - 1, -1, -1):
                row_index = i + 2
                row = rows_to_check[i]
                
                if len(row) > TIMESTAMP_COLUMN_INDEX and row[TIMESTAMP_COLUMN_INDEX]:
                    try:
                        promo_timestamp = datetime.datetime.fromisoformat(row[TIMESTAMP_COLUMN_INDEX]).astimezone(pytz.utc)
                        time_diff = now_utc - promo_timestamp

                        if time_diff.total_seconds() > (48 * 3600):
                            rows_to_delete.append(row_index)
                            
                    except ValueError:
                        print(f"Erro ao analisar o timestamp na linha {row_index}. Ignorando.")

            if rows_to_delete:
                for index in sorted(rows_to_delete, reverse=True):
                    worksheet.delete_rows(index)
                    
                await client.send_message(MY_PRIVATE_CHAT, f"Limpeza concluída: Foram removidas {len(rows_to_delete)} promoções com mais de 48 horas da planilha.")
            else:
                print("Nenhuma promoção com mais de 48h encontrada para limpeza.")

        except Exception as e:
            await client.send_message(MY_PRIVATE_CHAT, f"Erro na limpeza automática da planilha: {e}")
            print(f"Erro no cleaner: {e}")


@client.on(events.NewMessage(chats=TARGET_GROUP if TARGET_GROUP else None))
async def handle_new_message(event):
    if not event.text or event.is_private or event.out:
        return 

    mensagem_texto = event.text
    print(f"Mensagem recebida: {mensagem_texto[:50]}...")
    
    current_time_utc_str = datetime.datetime.now(pytz.utc).isoformat()
    
    if gemini_client:
        try:
            prompt_final = PROMPT_BASE.format(interesses=INTERESSES_LIST, mensagem_texto=mensagem_texto)
            response = gemini_client.models.generate_content(model='gemini-1.5-flash', contents=prompt_final)
            classificacao = response.text.strip().upper()
            
            print(f"Classificação da IA: {classificacao}")

            if classificacao == 'SIM':
                if worksheet:
                    try:
                        row = [str(event.date), mensagem_texto, classificacao, current_time_utc_str]
                        worksheet.append_row(row)
                        print("Promoção salva no Google Sheets.")
                    except Exception as e:
                        print(f"Erro ao salvar no Google Sheets: {e}")
                        
                alerta_msg = f"PROMOÇÃO ENCONTRADA!\n\nOrigem: {TARGET_GROUP}\n\nMensagem:\n---\n{mensagem_texto}"
                await client.send_message(MY_PRIVATE_CHAT, alerta_msg)
                print("Alerta enviado para o chat privado.")
                
        except Exception as e:
            await client.send_message(MY_PRIVATE_CHAT, f"Erro na análise da IA: {e}")
            print(f"Erro na análise da IA: {e}")
    else:
        print("Cliente Gemini não configurado. Ignorando análise de IA.")


@client.on(events.NewMessage(chats=MY_PRIVATE_CHAT, func=lambda e: e.text and not e.message.out))
async def handle_private_chat(event):
    """Permite conversar com o bot usando a IA (Gemini)."""
    if not gemini_client:
        await event.reply("O serviço Gemini não está configurado. Não posso conversar.")
        return

    try:
        chat_prompt = f"Você é um assistente de compras inteligente focado em promoções. Seu trabalho é responder as perguntas do usuário de forma útil e concisa. Pergunta: {event.text}"
        response = gemini_client.models.generate_content(model='gemini-1.5-flash', contents=chat_prompt)
        await event.reply(response.text)
    except Exception as e:
        await event.reply(f"Ocorreu um erro ao processar sua solicitação com a IA: {e}")


# --- 5. EXECUÇÃO PRINCIPAL ---
async def main():
    if not setup_gemini():
        print("Aviso: Gemini não configurado. O bot funcionará apenas com monitoramento e gspread.")
        
    if not setup_gspread():
        print("Aviso: gspread não configurado. O bot não salvará dados na planilha.")
        
    print("Iniciando Userbot...")
    await client.start()
    print("Userbot iniciado e ouvindo novas mensagens...")
    
    if worksheet:
        new_header = ["Data/Hora Recebimento", "Mensagem da Promoção", "Classificação IA", "Timestamp UTC"]
        current_header = worksheet.row_values(1)
        if not current_header:
            worksheet.append_row(new_header)
        elif current_header != new_header:
            print("Aviso: Planilha encontrada com cabeçalhos diferentes do esperado.")

    asyncio.create_task(sheet_cleaner())
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
