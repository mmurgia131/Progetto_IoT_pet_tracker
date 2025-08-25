import os
import json
import threading
import asyncio
import math
from flask import jsonify, request, render_template, redirect, url_for, session, flash, Flask
import requests
import websockets
from pettracker_db import PetTrackerDB
from auth import AuthManager, login_required, generate_secret_key
from datetime import datetime, timedelta, UTC, timezone
import time
import paho.mqtt.client as mqtt
from zoneinfo import ZoneInfo
from dateutil.parser import isoparse

from telegram_bot import notify_events, save_chat_id

ESP32CAM_IP = os.getenv("ESP32CAM_IP", "http://172.20.10.2")
ESP32_STREAM_PATH = "/stream"
ESP32_CONTROL_PATH = "/control"

MQTT_BROKER = "172.20.10.4"
MQTT_PORT = 1883
MQTT_GPS_TOPIC = "pettracker/gps"
MQTT_BUZZER_CMD_TOPIC = "pettracker/cmd/buzzer"
MQTT_ENV_TOPIC = "pettracker/env"
MQTT_ANCHORS_TOPIC = "tracker/anchors"

db = PetTrackerDB()
auth_manager = AuthManager(db)

app = Flask(__name__)
app.secret_key = generate_secret_key()

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def is_inside_circle(lat_pet, lon_pet, lat_center, lon_center, radius):
    distanza = haversine(lat_pet, lon_pet, lat_center, lon_center)
    return distanza <= radius

latest_gps = {"lat": None, "lon": None}
latest_env = {}
mqtt_client = None

# Stato globale per logica notifiche combinate
current_is_outside = False
current_temp_high = False
current_temp_low = False
current_temp_value = None
current_temp_min = None
current_temp_max = None

# ------------- ANCORE BLE REGISTRAZIONE -------------
anchors_online = {}  # mac_address: {"anchor_id": ..., "timestamp": ...}
ANCHORS_REFRESH_SEC = 120

@app.route('/anchors_online')
@login_required
def anchors_online_view():
    # Mostra solo quelle annunciate negli ultimi ANCHORS_REFRESH_SEC secondi
    cutoff = time.time() - ANCHORS_REFRESH_SEC
    anchors = [
        {"mac_address": mac, "anchor_id": info["anchor_id"]}
        for mac, info in anchors_online.items()
        if info["timestamp"] > cutoff
    ]
    return jsonify({"anchors": anchors})

# ------------- ROLLING WINDOW RSSI PER LOCALIZZAZIONE BLE -------------
rssi_windows = {}  # key: (anchor_id, pet_mac) -> [RSSI, RSSI, RSSI]
pet_room_estimate = {}  # pet_mac -> {"room": anchor_id, "avg_rssi": ...}

def update_rssi_window(anchor_id, pet_mac, rssi, bt_name=None):
    key = (anchor_id, pet_mac)
    arr = rssi_windows.setdefault(key, [])
    arr.append(rssi)
    if len(arr) > 3:
        arr.pop(0)
    if len(arr) == 3:
        avg = sum(arr) / 3
        prev = pet_room_estimate.get(pet_mac)
        if not prev or avg > prev["avg_rssi"]:
            pet_room_estimate[pet_mac] = {
                "room": anchor_id,
                "avg_rssi": avg,
                "bt_name": bt_name,
                "last_seen": time.time()
            }
            print(f"[BLE-LOC] Pet {pet_mac} stimato in stanza ancora {anchor_id} (RSSI medio {avg:.1f})")

# ---- PATCH: Lista dispositivi BLE visti dalle ancore per registrazione pet ----
@app.route('/detected_pets')
@login_required
def detected_pets():
    now = time.time()
    CUTOFF_SEC = 60
    pets = []
    for mac, info in pet_room_estimate.items():
        # Mostra solo quelli visti di recente e che hanno RSSI valido
        if info.get("last_seen") and now - info["last_seen"] < CUTOFF_SEC:
            pets.append({
                "mac_address": mac,
                "bt_name": info.get("bt_name", ""),
                "rssi": info.get("avg_rssi"),
                "anchor_id": info.get("room")
            })
    pets.sort(key=lambda p: p.get("rssi", -200), reverse=True)
    return jsonify({"pets": pets})

# -------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = auth_manager.verify_password(username, password)
        if user:
            session['username'] = user['username']
            flash('Login effettuato con successo!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Credenziali non valide', 'danger')
            return render_template('login.html')
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    user = auth_manager.get_user_info(session['username'])
    pets = db.get_pets_for_user(user['_id'])
    return render_template('dashboard.html', user=user, pets=pets)

@app.route('/dashboard_pet/<pet_id>')
@login_required
def dashboard_pet(pet_id):
    user = auth_manager.get_user_info(session['username'])
    pet = db.get_pet_by_id(pet_id)
    return render_template('dashboard_pet.html', user=user, pet=pet)

@app.route('/get_latest_env/<pet_id>')
@login_required
def get_latest_env(pet_id):
    env = latest_env.get(pet_id) or db.get_latest_env(pet_id)
    if not env:
        return jsonify({"temp": None, "hum": None})
    return jsonify({
        "temp": float(env.get("temp")) if env.get("temp") is not None else None,
        "hum": float(env.get("hum")) if env.get("hum") is not None else None,
        "timestamp": env.get("timestamp")
    })

@app.route('/add_pet', methods=['GET', 'POST'])
@login_required
def add_pet():
    user = auth_manager.get_user_info(session['username'])
    if request.method == 'POST':
        pet_name = request.form['pet_name']
        mac_address = request.form['mac_address']
        bt_name = request.form['bt_name']
        temp_min = request.form.get('temp_min')
        temp_max = request.form.get('temp_max')
        db.add_pet(
            name=pet_name,
            owner_id=user['_id'],
            mac_address=mac_address,
            bt_name=bt_name,
            temp_min=float(temp_min) if temp_min else None,
            temp_max=float(temp_max) if temp_max else None
        )
        flash("Pet aggiunto con successo!", "success")
        return redirect(url_for('dashboard'))
    return render_template('add_pet.html', user=user)

@app.route('/edit_pet/<pet_id>', methods=['GET', 'POST'])
@login_required
def edit_pet(pet_id):
    user = auth_manager.get_user_info(session['username'])
    pet = db.get_pet_by_id(pet_id)
    if request.method == 'POST':
        pet_name = request.form['pet_name']
        mac_address = request.form['mac_address']
        bt_name = request.form['bt_name']
        temp_min = request.form.get('temp_min')
        temp_max = request.form.get('temp_max')
        db.update_pet(
            pet_id,
            name=pet_name,
            mac_address=mac_address,
            bt_name=bt_name,
            temp_min=float(temp_min) if temp_min else None,
            temp_max=float(temp_max) if temp_max else None
        )
        flash("Pet modificato!", "success")
        return redirect(url_for('pets'))
    return render_template('edit_pet.html', pet=pet, user=user)

@app.route('/pets', methods=['GET'])
@login_required
def pets():
    user = auth_manager.get_user_info(session['username'])
    pets = db.get_pets_for_user(user['_id'])
    return render_template('pets.html', pets=pets, user=user)

@app.route('/delete_pet/<pet_id>', methods=['POST'])
@login_required
def delete_pet(pet_id):
    db.delete_pet(pet_id)
    flash("Pet eliminato!", "info")
    return redirect(url_for('pets'))

@app.route('/get_latest_gps')
def get_latest_gps():
    return jsonify(lat=latest_gps['lat'], lon=latest_gps['lon'])

@app.route('/config_area_main', methods=['GET'])
@login_required
def config_area_main():
    user = auth_manager.get_user_info(session['username'])
    rooms = db.get_rooms()
    perimeter_center = db.get_perimeter_center() or (45.123456, 9.123456)
    perimeter_radius = db.get_perimeter_radius() or 50
    gps = latest_gps
    pet_position = (float(gps['lat']), float(gps['lon'])) if gps and gps['lat'] and gps['lon'] else None
    return render_template(
        'config_area_main.html',
        user=user,
        rooms=rooms,
        perimeter_center=perimeter_center,
        perimeter_radius=int(perimeter_radius),
        pet_position=pet_position
    )

@app.route('/update_perimeter', methods=['POST'])
@login_required
def update_perimeter():
    lat = float(request.form['center_lat'])
    lng = float(request.form['center_lng'])
    radius = int(request.form['radius'])
    db.save_perimeter(center=(lat, lng), radius=radius)
    flash("Perimetro aggiornato!", "success")
    return redirect(url_for('config_area_main'))

@app.route('/edit_room/<room_id>', methods=['GET', 'POST'])
@login_required
def edit_room(room_id):
    room = db.get_room_by_id(room_id)
    if request.method == 'POST':
        room_name = request.form['room_name']
        mac_address = request.form['mac_address']
        allowed = request.form.get('allowed') == '1'
        db.update_room(room_id, name=room_name, mac_address=mac_address, allowed=allowed)
        flash("Stanza modificata!", "success")
        return redirect(url_for('config_area_main'))
    return render_template('edit_room.html', room=room)

@app.route('/delete_room/<room_id>', methods=['POST'])
@login_required
def delete_room(room_id):
    db.delete_room(room_id)
    flash("Stanza eliminata!", "info")
    return redirect(url_for('config_area_main'))

@app.route('/add_room', methods=['GET', 'POST'])
@login_required
def add_room():
    user = auth_manager.get_user_info(session['username'])
    if request.method == 'POST':
        room_name = request.form['room_name']
        mac_address = request.form['mac_address']
        allowed = request.form.get('allowed') == '1'
        db.add_room(name=room_name, mac_address=mac_address, allowed=allowed)
        flash("Stanza aggiunta!", "success")
        return redirect(url_for('config_area_main'))
    return render_template('add_room.html', user=user)

@app.route('/toggle_room_access/<room_id>', methods=['POST'])
def toggle_room_access(room_id):
    allowed = bool(int(request.form.get('allowed', 0)))
    db.update_room_access(room_id, allowed)
    return redirect(url_for('config_area_main'))

@app.route('/camera')
@login_required
def camera():
    return render_template('camera.html', esp32_ip=ESP32CAM_IP)

@app.route('/camera/control')
@login_required
def camera_control():
    params = request.args.to_dict(flat=True)
    upstream = f"{ESP32CAM_IP}{ESP32_CONTROL_PATH}"
    try:
        r = requests.get(upstream, params=params, timeout=5)
        return (r.text, r.status_code, {"Content-Type": r.headers.get("Content-Type", "text/plain")})
    except requests.RequestException as e:
        return f"Errore inoltro controllo: {e}", 502

@app.route('/localizza')
@login_required
def localizza():
    return render_template('localizza.html', gps=latest_gps)

@app.route('/buzzer', methods=['POST'])
@login_required
def buzzer():
    try:
        data = request.get_json(force=True)
        action = data.get("action", "on").lower()
        if mqtt_client is not None:
            if action == "on":
                mqtt_client.publish(MQTT_BUZZER_CMD_TOPIC, payload="on", qos=1, retain=False)
            elif action == "off":
                mqtt_client.publish(MQTT_BUZZER_CMD_TOPIC, payload="off", qos=1, retain=False)
            else:
                return jsonify(success=False, error="Azione non valida")
            return jsonify(success=True)
        else:
            print("MQTT client non inizializzato")
            return jsonify(success=False)
    except Exception as e:
        print("Errore invio comando buzzer:", e)
        return jsonify(success=False)

@app.route('/update_temp_thresholds/<pet_id>', methods=['POST'])
@login_required
def update_temp_thresholds(pet_id):
    temp_min = request.form.get('temp_min')
    temp_max = request.form.get('temp_max')
    try:
        temp_min_f = float(temp_min) if temp_min not in (None, "") else None
        temp_max_f = float(temp_max) if temp_max not in (None, "") else None
    except Exception:
        flash("Errore nei valori inseriti (usa solo numeri)", "danger")
        return redirect(url_for('dashboard_pet', pet_id=pet_id))
    db.update_pet(pet_id, temp_min=temp_min_f, temp_max=temp_max_f)
    flash("Soglie temperatura aggiornate!", "success")
    return redirect(url_for('dashboard_pet', pet_id=pet_id))

def build_timeline_segments(positions, period="day", start=None, end=None):
    from datetime import timedelta

    # Mappa stati → colore (come da tua richiesta)
    COLORS = {
        "ble_allowed":  "#49c24b",  # Zona Interna Consentita
        "ble_blocked":  "#e65c5c",  # Zona Interna NON Consentita
        "gps_allowed":  "#53c7c3",  # Zona Esterna Consentita
        "gps_blocked":  "#ffa500",  # Zona Esterna NON Consentita
        "no_data":      "#e9ecef",  # Nessun dato
    }

    def state_of(p):
        src = p.get("source")
        et  = p.get("entry_type")
        if src == "ble":
            if et in ("stanza_accessibile", "normal"):
                return "ble_allowed"
            if et in ("stanza_non_accessibile", "restricted"):
                return "ble_blocked"
        if src == "gps":
            if et == "zona_esterna_accessibile":
                return "gps_allowed"
            if et == "zona_esterna_non_accessibile":
                return "gps_blocked"
        return "no_data"

    # (le posizioni sono già convertite a timestamp_rome nella route /stats)
    # Giorni da visualizzare
    if period == "day":
        days = [start]
    elif period in ("week", "month"):
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    else:
        days = [start]

    timeline_by_day = {}
    for day in days:
        midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
        next_midnight = midnight + timedelta(days=1)

        # Eventi del giorno, ordinati
        day_positions = [p for p in positions
                         if p.get("timestamp_rome") and midnight <= p["timestamp_rome"] < next_midnight]
        day_positions.sort(key=lambda p: p["timestamp_rome"])

        segments = []
        cur_t = midnight
        last_seg = None  # dict con chiavi: state, start_dt, end_dt, label

        def push_or_extend(state, start_dt, end_dt, label=""):
            nonlocal last_seg
            # unisco se stesso stato ed è contiguo (o buco ≤ 60s)
            if last_seg and last_seg["state"] == state and (start_dt - last_seg["end_dt"]).total_seconds() <= 60:
                last_seg["end_dt"] = end_dt
            else:
                last_seg = {"state": state, "start_dt": start_dt, "end_dt": end_dt, "label": label}
                segments.append(last_seg)

        for i, p in enumerate(day_positions):
            start_dt = p["timestamp_rome"]
            end_dt = day_positions[i + 1]["timestamp_rome"] if i + 1 < len(day_positions) else next_midnight
            st = state_of(p)

            # eventuale buco prima dell’evento
            if start_dt > cur_t:
                push_or_extend("no_data", cur_t, start_dt, "Nessun dato")

            # segmento evento
            push_or_extend(st, start_dt, end_dt, p.get("room") or p.get("entry_type") or "")

            cur_t = end_dt

        # coda fino a mezzanotte
        if cur_t < next_midnight:
            push_or_extend("no_data", cur_t, next_midnight, "Nessun dato")

        # serializzo per il template
        out = []
        for s in segments:
            dur_s = (s["end_dt"] - s["start_dt"]).total_seconds()
            out.append({
                "colore": COLORS[s["state"]],
                "start": s["start_dt"].strftime("%H:%M"),
                "end": s["end_dt"].strftime("%H:%M"),
                "width_pct": (dur_s / 86400) * 100.0,
                "label": s["label"],
            })
        timeline_by_day[day.strftime("%d/%m/%Y")] = out

    # tacche orarie 0..24
    return timeline_by_day, [f"{h:02d}" for h in range(25)]


@app.route('/stats/<pet_id>')
@login_required
def stats(pet_id):
    user = auth_manager.get_user_info(session['username'])
    pet = db.get_pet_by_id(pet_id)

    period = request.args.get('period', 'day')
    date_str = request.args.get('date')
    granularity = request.args.get('gran', 'hour')

    rome = ZoneInfo("Europe/Rome")

    # 1) Giorno di riferimento in ORA LOCALE (Europe/Rome)
    if date_str:
        try:
            if "/" in date_str:
                base_local = datetime.strptime(date_str, "%d/%m/%Y").replace(tzinfo=rome)
            else:
                base_local = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=rome)
        except Exception as e:
            print("Errore conversione data:", date_str, e)
            base_local = datetime.now(rome)
    else:
        base_local = datetime.now(rome)

    # 2) Intervallo di visualizzazione in LOCALE
    if period == 'day':
        start_local = base_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local   = start_local + timedelta(days=1) - timedelta(microseconds=1)
    elif period == 'week':
        start_local = (base_local - timedelta(days=base_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end_local   = start_local + timedelta(days=7) - timedelta(microseconds=1)
    elif period == 'month':
        start_local = base_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start_local.month < 12:
            next_month = start_local.replace(month=start_local.month + 1, day=1)
        else:
            next_month = start_local.replace(year=start_local.year + 1, month=1, day=1)
        end_local   = next_month - timedelta(microseconds=1)
    else:
        start_local = base_local - timedelta(days=1)
        end_local   = base_local

    # 3) Query in UTC (il DB salva timestamp in UTC)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc   = end_local.astimezone(timezone.utc)

    positions = list(db.positions.find({
        "pet_id": str(pet_id),
        "timestamp": {"$gte": start_utc, "$lte": end_utc}
    }).sort("timestamp", 1))

    # 4) Normalizza timestamp e aggiungi 'timestamp_rome'
    for p in positions:
        ts = p.get('timestamp')
        if isinstance(ts, str):
            ts = isoparse(ts)
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        p['timestamp'] = ts
        p['timestamp_rome'] = ts.astimezone(rome) if ts else None

    # 5) Statistiche
    last_mov = "-"
    if positions and positions[-1].get('timestamp_rome'):
        last_mov = positions[-1]['timestamp_rome'].strftime("%H:%M")

    stats_obj = {
        'n_points': len(positions),
        'top_room': "None",
        'last_movement': last_mov,
        'restricted_entries': sum(
            1 for p in positions
            if p.get('entry_type') in ['zona_esterna_non_accessibile', 'restricted', 'stanza_non_accessibile']
        ),
    }

    # 6) Timeline: usa i limiti **locali** (coerenti con 'timestamp_rome')
    timeline_by_day, calendar_hours = build_timeline_segments(positions, period, start_local, end_local)

    # 7) Valore ISO per l’<input type="date">
    current_date_iso = start_local.strftime("%Y-%m-%d")

    return render_template(
        'stats.html',
        pet=pet,
        stats=stats_obj,
        current_date_iso=current_date_iso,   # <— usa questo nel template
        period=period,
        granularity=granularity,
        timeline_by_day=timeline_by_day,
        calendar_hours=calendar_hours
    )

def utc_to_rome(dt):
    if dt is None:
        return ""
    return dt.astimezone(ZoneInfo("Europe/Rome"))

app.jinja_env.filters['utc_to_rome'] = utc_to_rome

@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"].strip().lower()
        if text == "/start":
            save_chat_id(chat_id)
            reply_text = "✅ Iscritto alle notifiche! Riceverai un avviso se il tuo pet esce dal perimetro oppure se la temperatura è troppo alta o troppo bassa."
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "8422442152:AAGNoi5GfcNuaObdO5vttkdgQFTDIpU2L9k")
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            requests.post(url, data={"chat_id": chat_id, "text": reply_text})
    return "ok"

connected_clients = set()
main_asyncio_loop = None

async def websocket_handler(websocket):
    print(f"📡 Nuova connessione WebSocket da {websocket.remote_address}")
    connected_clients.add(websocket)
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get("type") == "control_command":
                    for client in connected_clients.copy():
                        if client != websocket and client.close_code is None:
                            await client.send(json.dumps(data))
                elif data.get("type") == "sensor_data" and "gps" in data:
                    latest_gps.update(data['gps'])
                    gps_update = {
                        "type": "gps_update",
                        "lat": latest_gps["lat"],
                        "lon": latest_gps["lon"]
                    }
                    for client in connected_clients.copy():
                        if client != websocket and client.close_code is None:
                            await client.send(json.dumps(gps_update))
                elif data.get("type") == "heartbeat":
                    pass
                elif data.get("type") == "frame":
                    for client in connected_clients.copy():
                        if client != websocket and client.close_code is None:
                            await client.send(message)
            except json.JSONDecodeError:
                print("❌ JSON non valido")
    except Exception as e:
        print(f"❌ Errore WebSocket: {e}")
    finally:
        connected_clients.discard(websocket)

async def run_ws_server():
    global main_asyncio_loop
    main_asyncio_loop = asyncio.get_event_loop()
    async with websockets.serve(websocket_handler, "0.0.0.0", 8765):
        print("✅ WebSocket Server in ascolto sulla porta 8765")
        await asyncio.Future()

def start_websocket_server():
    asyncio.run(run_ws_server())

def on_mqtt_connect(client, userdata, flags, rc):
    print(f"[MQTT] Connessione: {rc}")
    client.subscribe(MQTT_GPS_TOPIC)
    client.subscribe(MQTT_BUZZER_CMD_TOPIC)
    client.subscribe(MQTT_ENV_TOPIC)
    client.subscribe(MQTT_ANCHORS_TOPIC)
    client.subscribe("tracker/+/+")

def on_mqtt_message(client, userdata, msg):
    global main_asyncio_loop, latest_env
    global current_is_outside, current_temp_high, current_temp_low, current_temp_value, current_temp_min, current_temp_max, latest_gps
    try:
        payload = msg.payload.decode()
        print(f"[MQTT] Messaggio su {msg.topic}: {payload}")

        # --- Gestione ancore BLE (annuncio su tracker/anchors) ---
        if msg.topic == MQTT_ANCHORS_TOPIC:
            try:
                data = json.loads(payload)
                mac = data.get("mac_address")
                anchor_id = data.get("anchor_id")
                if mac and anchor_id:
                    anchors_online[mac] = {
                        "anchor_id": anchor_id,
                        "timestamp": time.time()
                    }
                    print(f"[ANCHOR REG] Ricevuta ancora {anchor_id} ({mac})")
            except Exception as e:
                print("Errore parsing ancora:", e)
            return

        # --- Gestione rolling window RSSI BLE (topic: tracker/<anchorID>/<petMACaddress>)
        if msg.topic.startswith("tracker/") and len(msg.topic.split("/")) == 3:
            _, anchor_id, pet_mac = msg.topic.split("/")
            try:
                data = json.loads(payload)
                rssi = data.get("rssi")
                bt_name = data.get("bt_name", "")
                if rssi is not None:
                    update_rssi_window(anchor_id, pet_mac, float(rssi), bt_name=bt_name)
            except Exception as e:
                print("Errore parsing RSSI:", e)
            return

        data = json.loads(payload) if msg.topic not in [MQTT_BUZZER_CMD_TOPIC] else payload

        if msg.topic == MQTT_GPS_TOPIC:
            data = json.loads(payload)
            data['type'] = 'gps_update'
            latest_gps['lat'] = data.get('lat')
            latest_gps['lon'] = data.get('lon')

            try:
                lat_pet = float(data.get('lat'))
                lon_pet = float(data.get('lon'))
                perimeter_center = db.get_perimeter_center() or (45.123456, 9.123456)
                perimeter_radius = db.get_perimeter_radius() or 50
                lat_centro, lon_centro = perimeter_center
                inside = is_inside_circle(lat_pet, lon_pet, lat_centro, lon_centro, perimeter_radius)
                pet_id = data.get("pet_id", "68a0945af163860973073d68")
                if inside:
                    entry_type = "zona_esterna_accessibile"
                else:
                    entry_type = "zona_esterna_non_accessibile"
                db.save_position(
                    pet_id=pet_id,
                    entry_type=entry_type,
                    lat=lat_pet,
                    lon=lon_pet,
                    source="gps"
                )
                current_is_outside = not inside
                gps_str = f"{lat_pet}, {lon_pet}"
                notify_events(
                    is_outside=current_is_outside,
                    temp_high=current_temp_high,
                    temp_low=current_temp_low,
                    gps=gps_str,
                    temp_value=current_temp_value,
                    temp_min=current_temp_min,
                    temp_max=current_temp_max
                )
            except Exception as e:
                print(f"[GPS/NOTIFICA] Errore controllo zona consentita: {e}")

            if main_asyncio_loop:
                for ws in connected_clients.copy():
                    if ws.close_code is None:
                        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(data)), main_asyncio_loop)

        elif msg.topic == MQTT_BUZZER_CMD_TOPIC:
            print("[MQTT] Comando buzzer ricevuto:", payload)

        elif msg.topic == MQTT_ENV_TOPIC:
            data = json.loads(payload)
            pet_id = data.get("pet_id", "68a0945af163860973073d68")
            temp = data.get("temp")
            hum = data.get("hum")
            timestamp = data.get("timestamp", int(time.time()))
            latest_env[pet_id] = {"temp": temp, "hum": hum, "timestamp": timestamp}
            db.save_env_data(pet_id, temp, hum, datetime.fromtimestamp(timestamp, UTC))

            try:
                pet = db.get_pet_by_id(pet_id)
                temp_min = float(pet.get("temp_min", 0))
                temp_max = float(pet.get("temp_max", 30))
                temp_high = temp is not None and float(temp) > temp_max
                temp_low = temp is not None and float(temp) < temp_min
                current_temp_high = temp_high
                current_temp_low = temp_low
                current_temp_value = temp
                current_temp_min = temp_min
                current_temp_max = temp_max
                gps_str = f"{latest_gps['lat']}, {latest_gps['lon']}" if latest_gps['lat'] and latest_gps['lon'] else None

                notify_events(
                    is_outside=current_is_outside,
                    temp_high=current_temp_high,
                    temp_low=current_temp_low,
                    gps=gps_str,
                    temp_value=current_temp_value,
                    temp_min=current_temp_min,
                    temp_max=current_temp_max
                )
            except Exception as e:
                print(f"[ENV/NOTIFICA] Errore controllo temperatura custom: {e}")

    except Exception as e:
        print(f"[MQTT] Errore on_message: {e}")

def mqtt_thread():
    global mqtt_client
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_message = on_mqtt_message
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_forever()

def start_mqtt_bridge():
    t = threading.Thread(target=mqtt_thread, daemon=True)
    t.start()

if __name__ == '__main__':
    ws_thread = threading.Thread(target=start_websocket_server, daemon=True)
    ws_thread.start()
    start_mqtt_bridge()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)