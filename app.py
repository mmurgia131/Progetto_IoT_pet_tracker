import os
import json
import threading
import asyncio
import math
from flask import jsonify, request
import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, Response, stream_with_context
)
import websockets
from pettracker_db import PetTrackerDB
from auth import AuthManager, login_required, generate_secret_key

# MQTT BRIDGE
import paho.mqtt.client as mqtt

from telegram_bot import notify_pet_outside_area, save_chat_id

ESP32CAM_IP = os.getenv("ESP32CAM_IP", "http://172.20.10.2")
ESP32_STREAM_PATH = "/stream"
ESP32_CONTROL_PATH = "/control"

MQTT_BROKER = "172.20.10.4"
MQTT_PORT = 1883
MQTT_TOPIC = "pettracker/ble_scan"
MQTT_GPS_TOPIC = "pettracker/gps"

PERIMETER_CENTER = (45.123456, 9.123456)  # Da rendere dinamico se vuoi!
DIAMETER_METERS = 50
RADIUS_METERS = DIAMETER_METERS / 2

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
    a = math.sin(delta_phi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(delta_lambda/2)**2
    c = 2*math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def is_inside_circle(lat_pet, lon_pet, lat_center, lon_center, radius):
    distanza = haversine(lat_pet, lon_pet, lat_center, lon_center)
    return distanza <= radius

from datetime import datetime, timedelta

# === Auth / Pagine base =======================================================
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

@app.route('/add_pet', methods=['GET','POST'])
@login_required
def add_pet():
    user = auth_manager.get_user_info(session['username'])
    if request.method == 'POST':
        pet_name = request.form['pet_name']
        mac_address = request.form['mac_address']
        bt_name = request.form['bt_name']
        db.add_pet(name=pet_name, owner_id=user['_id'], mac_address=mac_address, bt_name=bt_name)
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
        db.update_pet(pet_id, name=pet_name, mac_address=mac_address, bt_name=bt_name)
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

@app.route('/config_area_main', methods=['GET', 'POST'])
@login_required
def config_area_main():
    user = auth_manager.get_user_info(session['username'])
    # Qui puoi gestire la configurazione generale di area e stanze
    return render_template('config_area_main.html', user=user)

@app.route('/stats/<pet_id>')
@login_required
def stats(pet_id):
    user = auth_manager.get_user_info(session['username'])
    pet = db.get_pet_by_id(pet_id)
    period = request.args.get('period', 'day')
    now = datetime.utcnow()
    if period == 'day':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'week':
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'month':
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=1)
    positions = list(db.positions.find({
        "pet_id": str(pet_id),
        "timestamp": {"$gte": start, "$lte": now}
    }).sort("timestamp", 1))
    timestamps = []
    lats = []
    lons = []
    distances = []
    def haversine(lat1, lon1, lat2, lon2):
        import math
        R = 6371000
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        a = math.sin(delta_phi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(delta_lambda/2)**2
        c = 2*math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c
    total_distance = 0
    for i, p in enumerate(positions):
        timestamps.append(p['timestamp'].strftime("%Y-%m-%d %H:%M"))
        lats.append(p['lat'])
        lons.append(p['lon'])
        if i > 0:
            d = haversine(
                positions[i-1]['lat'], positions[i-1]['lon'],
                p['lat'], p['lon']
            )
            total_distance += d
            distances.append(round(d, 2))
        else:
            distances.append(0)
    stats = {
        'n_points': len(positions),
        'total_distance': round(total_distance/1000, 2),
        'period': period,
    }
    return render_template(
        'stats.html',
        pet=pet,
        stats=stats,
        timestamps=timestamps,
        distances=distances,
        lats=lats,
        lons=lons
    )

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


# === WebSocket Server =========================================================
latest_gps = {"lat": None, "lon": None}
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
                    print(f"🟧 Comando CONTROL_COMMAND ricevuto dal browser: {data.get('command')}, inoltro all'ESP32")
                    for client in connected_clients.copy():
                        if client != websocket and client.close_code is None:
                            await client.send(json.dumps(data))
                elif data.get("type") == "sensor_data" and "gps" in data:
                    print(f"📍 GPS ricevuto (vecchio canale WS): {data['gps']}")
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
                    print("💓 Heartbeat ricevuto")
                elif data.get("type") == "frame":
                    for client in connected_clients.copy():
                        if client != websocket and client.close_code is None:
                            await client.send(message)
                elif data.get("type") == "scan_ble":
                    print("🟦 Comando SCAN BLE ricevuto da browser, inoltro all'ESP32")
                    for client in connected_clients.copy():
                        if client != websocket and client.close_code is None:
                            await client.send(json.dumps({"type": "scan_ble"}))
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

# === MQTT Bridge: inoltra i risultati BLE e GPS via WebSocket ai browser ======
def on_mqtt_connect(client, userdata, flags, rc):
    print(f"[MQTT] Connessione: {rc}")
    client.subscribe(MQTT_TOPIC)
    client.subscribe(MQTT_GPS_TOPIC)

def on_mqtt_message(client, userdata, msg):
    global main_asyncio_loop
    try:
        payload = msg.payload.decode()
        print(f"[MQTT] Messaggio su {msg.topic}: {payload}")

        data = json.loads(payload)
        if msg.topic == MQTT_TOPIC:
            data['type'] = 'ble_scan_result'
            if main_asyncio_loop:
                for ws in connected_clients.copy():
                    if ws.close_code is None:
                        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(data)), main_asyncio_loop)
            else:
                print("[MQTT] Errore: main_asyncio_loop non inizializzato")
        elif msg.topic == MQTT_GPS_TOPIC:
            data['type'] = 'gps_update'
            latest_gps['lat'] = data.get('lat')
            latest_gps['lon'] = data.get('lon')
            if main_asyncio_loop:
                for ws in connected_clients.copy():
                    if ws.close_code is None:
                        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(data)), main_asyncio_loop)
            else:
                print("[MQTT] Errore: main_asyncio_loop non inizializzato")
            # --- LOGICA NOTIFICA TELEGRAM SE FUORI ZONA ----
            try:
                lat_pet = float(data.get('lat'))
                lon_pet = float(data.get('lon'))
                lat_centro, lon_centro = PERIMETER_CENTER
                if not is_inside_circle(lat_pet, lon_pet, lat_centro, lon_centro, RADIUS_METERS):
                    notify_pet_outside_area(gps=f"{lat_pet}, {lon_pet}")
            except Exception as e:
                print(f"[GPS/NOTIFICA] Errore controllo zona consentita: {e}")
    except Exception as e:
        print(f"[MQTT] Errore on_message: {e}")

def mqtt_thread():
    client = mqtt.Client()
    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_forever()

def start_mqtt_bridge():
    t = threading.Thread(target=mqtt_thread, daemon=True)
    t.start()

@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    print("Ricevuto da Telegram:", data)
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"].strip().lower()
        if text == "/start":
            save_chat_id(chat_id)
            reply_text = "✅ Iscritto alle notifiche! Riceverai un avviso se il tuo pet esce dal perimetro."
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "8422442152:AAGNoi5GfcNuaObdO5vttkdgQFTDIpU2L9k")
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            requests.post(url, data={"chat_id": chat_id, "text": reply_text})
    return "ok"

if __name__ == '__main__':
    ws_thread = threading.Thread(target=start_websocket_server, daemon=True)
    ws_thread.start()
    start_mqtt_bridge()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)