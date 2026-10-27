import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

# Carregado via variáveis de ambiente para segurança
API_ID = int(os.environ.get('API_ID', '0'))
API_HASH = os.environ.get('API_HASH', '')

if not API_ID or not API_HASH:
    print("--- Autenticação Telegram ---")
    print("Dica: Você também pode definir as variáveis de ambiente API_ID e API_HASH.")
    try:
        API_ID = int(input("Digite seu API_ID do Telegram: ").strip())
        API_HASH = input("Digite seu API_HASH do Telegram: ").strip()
    except Exception as e:
        print(f"Erro na entrada de dados: {e}")
        exit(1)

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\n--- COPIE A STRING ABAIXO PARA O SEU .ENV OU RAILWAY ---")
    print(client.session.save())
    print("--- FIM DA STRING ---")
