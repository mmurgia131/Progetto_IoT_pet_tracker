import requests
import threading
import time
import json
import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8422442152:AAGNoi5GfcNuaObdO5vttkdgQFTDIpU2L9k")
CHAT_IDS_FILE = "chat_ids.json"
PERIMETER_NOTIFICATION_COOLDOWN = 120  # secondi

last_perimeter_notification = 0

def load_chat_ids():
    try:
        with open(CHAT_IDS_FILE, "r") as f:
            ids = set(json.load(f))
            return ids
    except Exception:
        return set()

def save_chat_id(chat_id):
    chat_ids = load_chat_ids()
    chat_ids.add(str(chat_id))
    with open(CHAT_IDS_FILE, "w") as f:
        json.dump(list(chat_ids), f)
    print(f"Chat_id registrato: {chat_id}")

def send_telegram_message(message, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": str(chat_id),
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, data=data, timeout=10)
        if r.status_code != 200:
            print(f"[BOT] Errore invio messaggio a {chat_id}: {r.text}")
    except Exception as e:
        print(f"[BOT] Telegram message error: {e}")

def send_to_all_chats(msg):
    chat_ids = load_chat_ids()
    for chat_id in chat_ids:
        try:
            send_telegram_message(msg, chat_id)
        except Exception as e:
            print(f"[BOT] Errore invio a {chat_id}: {e}")

def notify_pet_outside_area(gps=None, cooldown=PERIMETER_NOTIFICATION_COOLDOWN):
    global last_perimeter_notification
    now = time.time()
    if now - last_perimeter_notification < cooldown:
        print(f"[BOT] Cooldown attivo per notifica fuori perimetro ({now - last_perimeter_notification:.1f}s fa)")
        return
    last_perimeter_notification = now
    gps_info = f"\nPosizione: {gps}" if gps else ""
    msg = (
        "🚨 <b>Il tuo animale è uscito dall'area consentita!</b>"
        f"{gps_info}\n"
        "Controlla la posizione e richiama il pet nell'area stabilita."
    )
    threading.Thread(target=send_to_all_chats, args=(msg,), daemon=True).start()