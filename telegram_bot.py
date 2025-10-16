import requests
import threading
import time
import json
import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_IDS_FILE = "chat_ids.json"

NOTIFICATION_COOLDOWN_SEC = int(os.getenv("NOTIFICATION_COOLDOWN_SEC", "60")) # per evitare spam
DEBOUNCE_SEC = float(os.getenv("NOTIFY_DEBOUNCE_SEC", "3.0"))

_pending_lock = threading.Lock()
pending_notifications = {}
pending_timers = {}
last_notification_time_by_key = {}


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
    try:
        with open(CHAT_IDS_FILE, "w") as f:
            json.dump(list(chat_ids), f)
        print(f"[TELEGRAM] Chat_id registrato: {chat_id}")
    except Exception as e:
        print(f"[TELEGRAM] Errore salvataggio chat_ids: {e}")


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
            print(f"[BOT] Errore invio messaggio a {chat_id}: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[BOT] Telegram message error: {e}")


def send_to_all_chats(msg):
    chat_ids = load_chat_ids()
    if not chat_ids:
        print("[TELEGRAM] Nessun chat_id registrato, skip invio")
        return
    for chat_id in chat_ids:
        try:
            send_telegram_message(msg, chat_id)
        except Exception as e:
            print(f"[BOT] Errore invio a {chat_id}: {e}")


def _merge_pending(existing, new):
    merged = existing.copy()
    merged['is_outside'] = existing.get('is_outside', False) or new.get('is_outside', False)
    merged['temp_high'] = existing.get('temp_high', False) or new.get('temp_high', False)
    merged['temp_low'] = existing.get('temp_low', False) or new.get('temp_low', False)
    merged['ble_restricted'] = existing.get('ble_restricted', False) or new.get('ble_restricted', False)

    for k in ('room', 'rssi', 'pet_name', 'pet_mac'):
        if new.get(k) is not None:
            merged[k] = new.get(k)

    if new.get('gps'):
        merged['gps'] = new.get('gps')

    if new.get('temp_value') is not None:
        merged['temp_value'] = new.get('temp_value')
    if new.get('temp_min') is not None:
        merged['temp_min'] = new.get('temp_min')
    if new.get('temp_max') is not None:
        merged['temp_max'] = new.get('temp_max')

    # 🔒 Se c'è una stanza BLE, annulla la posizione GPS
    if merged.get('ble_restricted') or (merged.get('room') not in (None, "", False)):
        merged['is_outside'] = False
        merged['gps'] = None

    return merged

def _build_message_from_payload(payload):
    """
    Crea il messaggio Telegram con priorità:
    1. Stanza non consentita (con temperatura)
    2. Fuori perimetro (con temperatura)
    3. Solo temperatura fuori soglia
    """
    pet_name = payload.get('pet_name') or payload.get('pet_mac') or "Pet"
    lines = ["🔔 Pet Tracker"]

    # Caso 1️⃣: BLE — stanza non consentita
    if payload.get('ble_restricted'):
        lines.append(f"🚫 Stanza NON consentita: {payload.get('room', '')} — Pet: {pet_name}")
        if payload.get('temp_high') and payload.get('temp_value') is not None and payload.get('temp_max') is not None:
            lines.append(f"🌡️ Temperatura alta: {float(payload['temp_value']):.1f}°C (limite {float(payload['temp_max']):.1f}°C)")
        if payload.get('temp_low') and payload.get('temp_value') is not None and payload.get('temp_min') is not None:
            lines.append(f"🌡️ Temperatura bassa: {float(payload['temp_value']):.1f}°C (limite {float(payload['temp_min']):.1f}°C)")
        return "\n".join(lines)

    # Caso 2️⃣: GPS — fuori perimetro
    if payload.get('is_outside'):
        lines.append(f"📍 {pet_name} fuori dal perimetro consentito")
        if payload.get('temp_high') and payload.get('temp_value') is not None and payload.get('temp_max') is not None:
            lines.append(f"🌡️ Temperatura alta: {float(payload['temp_value']):.1f}°C (limite {float(payload['temp_max']):.1f}°C)")
        if payload.get('temp_low') and payload.get('temp_value') is not None and payload.get('temp_min') is not None:
            lines.append(f"🌡️ Temperatura bassa: {float(payload['temp_value']):.1f}°C (limite {float(payload['temp_min']):.1f}°C)")
        if payload.get('gps'):
            gps_data = payload['gps']
            if isinstance(gps_data, (list, tuple)) and len(gps_data) == 2:
                lat, lon = gps_data
                gps_str = f"{lat:.8f} , {lon:.8f}"
            elif isinstance(gps_data, str):
                gps_str = gps_data
            else:
                gps_str = None
            if gps_str:
                lines.append(f"Posizione: {gps_str}")
        return "\n".join(lines)

    # Caso 3️⃣: solo temperatura fuori soglia
    if payload.get('temp_high') or payload.get('temp_low'):
        if payload.get('temp_high'):
            lines.append(f"🌡️ {pet_name} temperatura alta: {float(payload['temp_value']):.1f}°C (limite {float(payload['temp_max']):.1f}°C)")
        if payload.get('temp_low'):
            lines.append(f"🌡️ {pet_name} temperatura bassa: {float(payload['temp_value']):.1f}°C (limite {float(payload['temp_min']):.1f}°C)")
        return "\n".join(lines)

    # Nessuna condizione rilevante
    return None


def _send_aggregated_notification(key):
    with _pending_lock:
        payload = pending_notifications.pop(key, None)
        pending_timers.pop(key, None)
    if not payload:
        return

    msg = _build_message_from_payload(payload)
    now = time.time()
    last = last_notification_time_by_key.get(key, 0)
    if not msg:
        print(f"[TELEGRAM DEBUG] Nessuna notifica per {key}")
        return
    if now - last >= NOTIFICATION_COOLDOWN_SEC:
        print(f"[TELEGRAM] Invio notifica aggregata per {key}: {payload}")
        threading.Thread(target=send_to_all_chats, args=(msg,), daemon=True).start()
        last_notification_time_by_key[key] = now
    else:
        print(f"[TELEGRAM] Notifica per {key} ignorata per cooldown")


def notify_events(
    is_outside,
    temp_high,
    temp_low,
    gps=None,
    temp_value=None,
    temp_min=None,
    temp_max=None,
    ble_restricted=False,
    room=None,
    rssi=None,
    pet_name=None,
    pet_mac=None
):
    if ble_restricted or (room not in (None, "", False)):
        gps_to_use = None
    else:
        gps_to_use = gps

    payload = {
        "is_outside": bool(is_outside),
        "temp_high": bool(temp_high),
        "temp_low": bool(temp_low),
        "ble_restricted": bool(ble_restricted),
        "room": room,
        "rssi": rssi,
        "pet_name": pet_name,
        "pet_mac": pet_mac,
        "gps": gps_to_use,
        "temp_value": temp_value,
        "temp_min": temp_min,
        "temp_max": temp_max
    }

    key = pet_mac or pet_name or "global"
    print(f"[TELEGRAM DEBUG] notify_events called with: {payload} -> key={key}")

    with _pending_lock:
        existing = pending_notifications.get(key)
        if existing:
            merged = _merge_pending(existing, payload)
            pending_notifications[key] = merged
        else:
            pending_notifications[key] = payload

        timer = pending_timers.get(key)
        if timer and timer.is_alive():
            try:
                timer.cancel()
            except Exception:
                pass
        new_timer = threading.Timer(DEBOUNCE_SEC, _send_aggregated_notification, args=(key,))
        pending_timers[key] = new_timer
        new_timer.daemon = True
        new_timer.start()


__all__ = ["notify_events", "save_chat_id"]
