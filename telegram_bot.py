import requests
import threading
import time
import json
import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8422442152:AAGNoi5GfcNuaObdO5vttkdgQFTDIpU2L9k")
CHAT_IDS_FILE = "chat_ids.json"
PERIMETER_NOTIFICATION_COOLDOWN = 90  # secondi

last_notification_time = 0

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

def notify_events(is_outside, temp_high, temp_low, gps=None, temp_value=None, temp_min=None, temp_max=None):
    global last_notification_time  # aggiungi questa riga!
    messaggio = None
    if is_outside:
        if temp_high:
            messaggio = (
                f"Il pet si trova fuori dall'area consentita "
                f"e la temperatura rilevata ({temp_value}°C) è sopra la soglia massima ({temp_max}°C).\n"
                f"Ultima posizione: {gps or 'Posizione sconosciuta'}"
            )
        elif temp_low:
            messaggio = (
                f"Il pet si trova fuori dall'area consentita "
                f"e la temperatura rilevata ({temp_value}°C) è sotto la soglia minima ({temp_min}°C).\n"
                f"Ultima posizione: {gps or 'Posizione sconosciuta'}"
            )
        else:
            messaggio = (
                f"Il pet si trova fuori dall'area consentita!\n"
                f"Ultima posizione: {gps or 'Posizione sconosciuta'}"
            )
    else:
        if temp_high:
            messaggio = (
                f"Attenzione! Temperatura sopra la soglia: "
                f"{temp_value}°C (soglia max {temp_max}°C)."
            )
        elif temp_low:
            messaggio = (
                f"Attenzione! Temperatura sotto la soglia: "
                f"{temp_value}°C (soglia min {temp_min}°C)."
            )
    if messaggio:
        now = time.time()
        if now - last_notification_time >= PERIMETER_NOTIFICATION_COOLDOWN:
            print("[TELEGRAM] Invio notifica:", messaggio)
            threading.Thread(target=send_to_all_chats, args=(messaggio,), daemon=True).start()
            last_notification_time = now
        else:
            print("[TELEGRAM] Notifica ignorata per cooldown")
