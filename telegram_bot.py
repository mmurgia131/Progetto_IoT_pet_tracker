import requests
import threading
import time
import json
import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8422442152:AAGNoi5GfcNuaObdO5vttkdgQFTDIpU2L9k")
CHAT_IDS_FILE = "chat_ids.json"


NOTIFICATION_COOLDOWN_SEC = int(os.getenv("NOTIFICATION_COOLDOWN_SEC", "60"))

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

def notify_events(
    is_outside,
    temp_high,
    temp_low,
    gps=None,
    temp_value=None,
    temp_min=None,
    temp_max=None,
    # --- nuovi opzionali per BLE stanza non accessibile ---
    ble_restricted=False,
    room=None,
    rssi=None,
    pet_name=None,
    pet_mac=None
):
    """
    Invia un messaggio combinato quando c'è qualcosa da segnalare:
      - Perimetro: is_outside True
      - Temperatura: temp_high / temp_low
      - BLE stanza NON consentita: ble_restricted True
    """
    global last_notification_time

    lines = []

    # --- BLE stanza NON consentita ---
    if ble_restricted:
        line = "<b>Stanza NON consentita</b>"
        if room:
            line += f": <b>{room}</b>"
        if pet_name:
            line += f" — Pet: <b>{pet_name}</b>"
        lines.append(line)

    # --- Perimetro ---
    if is_outside:
        if pet_name:
            lines.append(f"📍 <b>{pet_name} Fuori dal perimetro consentito</b>")
        else:
            lines.append("📍 <b>Fuori dal perimetro consentito</b>")

    # --- Temperatura ---
    if temp_high and temp_value is not None and temp_max is not None:
        lines.append(f"🌡️ <b>Temperatura alta</b>: {float(temp_value):.1f}°C (limite {float(temp_max):.1f}°C)")
    if temp_low and temp_value is not None and temp_min is not None:
        lines.append(f"🌡️ <b>Temperatura bassa</b>: {float(temp_value):.1f}°C (limite {float(temp_min):.1f}°C)")

    if not lines:
        return

    if gps:
        lines.append(f"\nPosizione: <code>{gps}</code>")

    msg = "🔔 <b>Pet Tracker</b>\n" + "\n".join(f"• {l}" for l in lines)

    now = time.time()
    if now - last_notification_time >= NOTIFICATION_COOLDOWN_SEC:
        print("[TELEGRAM] Invio notifica:", msg)
        threading.Thread(target=send_to_all_chats, args=(msg,), daemon=True).start()
        last_notification_time = now
    else:
        print("[TELEGRAM] Notifica ignorata per cooldown")
