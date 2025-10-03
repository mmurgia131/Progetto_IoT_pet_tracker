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

MQTT_BROKER = "172.20.10.4" #0.0.0.0
MQTT_PORT = 1883
MQTT_GPS_TOPIC = "pettracker/gps"
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

# ===== BLE / ANCORE =====
anchors_online = {}  # mac_address: {"anchor_id": ..., "timestamp": ...}
ANCHORS_REFRESH_SEC = 120

# Rolling window RSSI per localizzazione BLE (già usata per /detected_pets)
rssi_windows = {}  # key: (anchor_id, pet_mac) -> [RSSI, RSSI, RSSI]
pet_room_estimate = {}  # pet_mac -> {"room": anchor_id, "avg_rssi": ..., ...}

# Parametri stima presenza in stanza
RSSI_THRESHOLD = float(os.getenv("BLE_RSSI_THRESHOLD", "-65"))   # calibra in casa tua
BLE_EVENT_COOLDOWN_SEC = int(os.getenv("BLE_EVENT_COOLDOWN_SEC", "5"))
last_ble_state = {}  # pet_mac -> {"room": str, "t": float, "avg": float}

# Stato per notifiche stanza non consentita (utile per UI/future)
current_in_restricted_room = False
current_restricted_room = None

@app.route('/change_credentials', methods=['GET', 'POST'])
@login_required
def change_credentials():
    if request.method == 'POST':
        current_username = session['username']
        current_password = request.form['current_password']
        new_username = request.form.get('new_username')
        new_password = request.form.get('new_password')
        result = auth_manager.change_credentials(
            current_username, current_password,
            new_username=new_username if new_username else None,
            new_password=new_password if new_password else None
        )
        flash(result['message'], 'success' if result['success'] else 'danger')
        if result['success']:
            return redirect(url_for('dashboard'))
    return render_template('change_credentials.html')

@app.route('/anchors_online')
@login_required
def anchors_online_view():
    cutoff = time.time() - ANCHORS_REFRESH_SEC
    anchors = [
        {"mac_address": mac, "anchor_id": info["anchor_id"]}
        for mac, info in anchors_online.items()
        if info["timestamp"] > cutoff
    ]
    return jsonify({"anchors": anchors})

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

@app.route('/detected_pets')
@login_required
def detected_pets():
    # Recupera tutti i MAC già associati a un pet
    registered_macs = set(
        p.get("mac_address") for p in db.pets.find() if p.get("mac_address")
    )
    pets = []
    for mac, info in pet_room_estimate.items():
        if mac in registered_macs:
            continue  # Salta quelli già associati a un animale
        pets.append({
            "mac_address": mac,
            "bt_name": info.get("bt_name", ""),
            "rssi": info.get("avg_rssi"),
            "anchor_id": info.get("room"),
            "last_seen": info.get("last_seen")
        })
    pets.sort(key=lambda p: p.get("rssi", -200), reverse=True)
    return jsonify({"pets": pets})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('action') == 'change_credentials':
            # Cambio credenziali
            current_username = request.form['current_username']
            current_password = request.form['current_password']
            new_username = request.form.get('new_username')
            new_password = request.form.get('new_password')
            result = auth_manager.change_credentials(
                current_username, current_password,
                new_username=new_username if new_username else None,
                new_password=new_password if new_password else None
            )
            flash(result['message'], 'success' if result['success'] else 'danger')
            return render_template('login.html')
        else:
            # Login normale
            username = request.form['username']
            password = request.form['password']
            user = auth_manager.verify_password(username, password)
            if user:
                session['username'] = user['username']
                session['is_first_login'] = user.get('is_first_login', False)
                flash('Login effettuato con successo!', 'success')
                return redirect(url_for('dashboard'))
            else:
                flash('Credenziali non valide', 'danger')
                return render_template('login.html')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        result = auth_manager.register_user(username, password)
        if result["success"]:
            flash('Registrazione completata! Ora puoi effettuare il login.', 'success')
            return redirect(url_for('login'))
        else:
            flash(result["message"], "danger")
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    flash('Logout effettuato con successo!', 'info')
    return redirect(url_for('login'))

@app.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    username = session['username']
    # Cancella l'utente dal db
    success = auth_manager.delete_account(username)
    session.pop('username', None)
    if success:
        flash('Account eliminato con successo.', 'success')
    else:
        flash('Errore durante l\'eliminazione dell\'account.', 'danger')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    user = auth_manager.get_user_info(session['username'])
    pets = db.get_pets_for_user(user['_id'])
    is_first_login = session.pop('is_first_login', False)  # la leggiamo una volta sola
    return render_template('dashboard.html', user=user, pets=pets, is_first_login=is_first_login)

@app.route('/dashboard_pet/<pet_id>')
@login_required
def dashboard_pet(pet_id):
    user = auth_manager.get_user_info(session['username'])
    pet = db.get_pet_by_id(pet_id)
    return render_template('dashboard_pet.html', user=user, pet=pet)

@app.route('/get_latest_env/<pet_id>')
@login_required
def get_latest_env(pet_id):
    # Usiamo la chiave "global" per temperatura/umidità unica
    env = latest_env.get("global") or db.get_latest_env("global")
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
            bt_name=bt_name
            # NB: la tua PetTrackerDB.add_pet non accetta temp_min/max; li gestiamo via update_pet se servisse
        )
        # Se vuoi salvarle subito:
        if temp_min or temp_max:
            pet = db.pets.find_one({"owner_id": str(user['_id']), "mac_address": mac_address})
            if pet:
                db.update_pet(
                    str(pet["_id"]),
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
    # Passiamo alla pagina una mappa mac -> nome pet per poter mostrare etichette friendly
    user = auth_manager.get_user_info(session['username'])
    pets = db.get_pets_for_user(user['_id'])
    pets_by_mac = {}
    for p in pets:
        mac = p.get("mac_address")
        if mac:
            pets_by_mac[mac] = p.get("name", mac)

    # Opzionale: mac iniziale del primo pet (se vuoi pre-selezionarlo)
    mac_address = pets[0]['mac_address'] if pets else ''
    return render_template('localizza.html', gps=latest_gps, pets_by_mac=pets_by_mac, mac_address=mac_address)

# --- Aggiungere la seguente route in app.py (posizionala vicino ad altre route GET) ---

@app.route('/get_pet_location/<pet_id>')
@login_required
def get_pet_location(pet_id):
    """
    Restituisce la posizione 'logica' del pet:
      - room: nome della stanza stimata via BLE (se disponibile)
      - gps: ultime coordinate GPS disponibili (se presenti)
    Il risultato è JSON: { "room": <str|null>, "gps": { "lat": <float>, "lon": <float> } | null }
    """
    try:
        pet = db.get_pet_by_id(pet_id)
        if not pet:
            return jsonify({"room": None, "gps": None})

        # Proviamo prima a risolvere stanza via BLE (mappa in memoria pet_room_estimate)
        room_name = None
        mac = pet.get("mac_address")
        if mac:
            info = pet_room_estimate.get(mac)
            if info:
                anchor_id = info.get("room")
                # Prova a risolvere anchor_id in una stanza registrata nel DB
                room_doc = db.rooms.find_one({"name": anchor_id}) or db.rooms.find_one({"mac_address": anchor_id})
                if room_doc and room_doc.get("name"):
                    room_name = room_doc.get("name")
                else:
                    # se non troviamo una doc, usa l'anchor_id grezzo come etichetta
                    room_name = anchor_id

        # Proviamo a fornire coordinate GPS (ultimo record per quel pet oppure fallback a latest_gps globale)
        gps = None
        try:
            last_pos = db.get_last_position(pet_id)
            if last_pos and last_pos.get("lat") is not None and last_pos.get("lon") is not None:
                gps = {"lat": float(last_pos["lat"]), "lon": float(last_pos["lon"])}
        except Exception:
            gps = None

        # fallback: global latest_gps (se non abbiamo posizioni specifiche)
        if gps is None and latest_gps.get("lat") and latest_gps.get("lon"):
            try:
                gps = {"lat": float(latest_gps["lat"]), "lon": float(latest_gps["lon"])}
            except Exception:
                gps = None

        return jsonify({"room": room_name, "gps": gps})
    except Exception as e:
        print("[GET_PET_LOCATION] Errore:", e)
        return jsonify({"room": None, "gps": None})
    

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

def build_timeline_segments(positions, period="day", start=None, end=None, max_gap_seconds=1800):
    # max_gap_seconds = 1800 -> 30 minuti
    from datetime import timedelta

    COLORS = {
        "ble_allowed":  "#49c24b",
        "ble_blocked":  "#e65c5c",
        "gps_allowed":  "#53c7c3",
        "gps_blocked":  "#ffa500",
        "no_data":      "#e9ecef",
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

        day_positions = [p for p in positions
                         if p.get("timestamp_rome") and midnight <= p["timestamp_rome"] < next_midnight]
        day_positions.sort(key=lambda p: p["timestamp_rome"])

        segments = []
        cur_t = midnight
        last_seg = None

        def push_or_extend(state, start_dt, end_dt, label=""):
            nonlocal last_seg
            if last_seg and last_seg["state"] == state and (start_dt - last_seg["end_dt"]).total_seconds() <= 60:
                last_seg["end_dt"] = end_dt
            else:
                last_seg = {"state": state, "start_dt": start_dt, "end_dt": end_dt, "label": label}
                segments.append(last_seg)

        for i, p in enumerate(day_positions):
            start_dt = p["timestamp_rome"]
            end_dt = day_positions[i + 1]["timestamp_rome"] if i + 1 < len(day_positions) else next_midnight
            st = state_of(p)

            # Se c'è un buco tra cur_t e start_dt, metti "nessun dato"
            if start_dt > cur_t:
                push_or_extend("no_data", cur_t, start_dt, "Nessun dato")

            # FINE PATCH: se il gap al prossimo evento (o alla fine della giornata) è troppo grande, tronca lo stato dopo max_gap_seconds e poi "nessun dato"
            gap_to_next = (end_dt - start_dt).total_seconds()
            next_break = min(gap_to_next, max_gap_seconds)
            # Segmento valido solo per max_gap_seconds
            push_or_extend(st, start_dt, start_dt + timedelta(seconds=next_break), p.get("room") or p.get("entry_type") or "")
            if gap_to_next > max_gap_seconds:
                # Da qui in poi "nessun dato" fino al prossimo evento (o fine giornata)
                nd_start = start_dt + timedelta(seconds=max_gap_seconds)
                push_or_extend("no_data", nd_start, end_dt, "Nessun dato")
            cur_t = end_dt

        if cur_t < next_midnight:
            push_or_extend("no_data", cur_t, next_midnight, "Nessun dato")

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

    start_utc = start_local.astimezone(timezone.utc)
    end_utc   = end_local.astimezone(timezone.utc)

    positions = list(db.positions.find({
        "pet_id": str(pet_id),
        "timestamp": {"$gte": start_utc, "$lte": end_utc}
    }).sort("timestamp", 1))

    for p in positions:
        ts = p.get('timestamp')
        if isinstance(ts, str):
            ts = isoparse(ts)
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        p['timestamp'] = ts
        p['timestamp_rome'] = ts.astimezone(rome) if ts else None

    last_mov = "-"
    if positions and positions[-1].get('timestamp_rome'):
        last_mov = positions[-1]['timestamp_rome'].strftime("%H:%M")

    stats_obj = {
        'n_points': len(positions),
        'top_room': "None",
        'last_movement': last_mov,
        'restricted_entries': 0,
    }

    if positions:
        # ----------- Total Movements -----------
        total_movements = 1  # Primo movimento conta sempre
        prev_state = None
        for p in positions:
            # Scegli una tupla che rappresenta la posizione logica (room oppure entry_type)
            key = (p.get("room"), p.get("entry_type"))
            if prev_state is not None and key != prev_state:
                total_movements += 1
            prev_state = key
        stats_obj['n_points'] = total_movements

        # ----------- Top Room -----------
        # Somma il tempo in ogni stanza BLE (solo per eventi "ble_allowed" o "ble_blocked")
        from collections import defaultdict
        room_times = defaultdict(float)
        for i in range(len(positions) - 1):
            p = positions[i]
            next_p = positions[i + 1]
            if p.get("room"):
                dt = (next_p["timestamp"] - p["timestamp"]).total_seconds()
                room_times[p["room"]] += max(dt, 0)
        if room_times:
            top_room = max(room_times.items(), key=lambda x: x[1])[0]
            room_times = defaultdict(float)
            for i in range(len(positions) - 1):
                p = positions[i]
                next_p = positions[i + 1]
                if p.get("room"):
                    dt = (next_p["timestamp"] - p["timestamp"]).total_seconds()
                    room_times[p["room"]] += max(dt, 0)

            # Crea una mappa MAC address → nome stanza leggibile
            room_name_map = {}
            for room in db.get_rooms():
                if "mac_address" in room and "name" in room:
                    room_name_map[room["mac_address"]] = room["name"]

            if room_times:
                # Trova la chiave (MAC address) con più tempo e mostra il nome stanza
                top_room_mac = max(room_times.items(), key=lambda x: x[1])[0]
                stats_obj['top_room'] = room_name_map.get(top_room_mac, top_room_mac)
            else:
                stats_obj['top_room'] = "None"

        # ----------- Restricted Entries -----------
        restricted_entries = 0
        for p in positions:
            et = p.get("entry_type", "")
            if et in ["zona_esterna_non_accessibile", "restricted", "stanza_non_accessibile"]:
                restricted_entries += 1
        stats_obj['restricted_entries'] = restricted_entries

    timeline_by_day, calendar_hours = build_timeline_segments(positions, period, start_local, end_local)
    current_date_iso = start_local.strftime("%Y-%m-%d")

    # Costruisci tutte le 5 righe timeline per ogni giorno (per settimana/mese)
    STATES = [
        ("Zona Interna Consentita", "#49c24b"),
        ("Zona Interna NON Consentita", "#e65c5c"),
        ("Zona Esterna Consentita", "#53c7c3"),
        ("Zona Esterna NON Consentita", "#ffa500"),
        ("Nessun dato", "#e9ecef"),
    ]

    timeline_by_days_and_state = []
    for day, segs in timeline_by_day.items():
        day_rows = []
        for label, colore in STATES:
            riga = []
            for s in segs:
                if s["colore"] == colore:
                    riga.append(s)
                else:
                    riga.append({
                        "colore": "transparent",
                        "start": s["start"],
                        "end": s["end"],
                        "width_pct": s["width_pct"],
                        "label": ""
                    })
            day_rows.append({"label": label, "colore": colore, "segments": riga})
    timeline_by_day, calendar_hours = build_timeline_segments(positions, period, start_local, end_local)
    return render_template(
        'stats.html',
        pet=pet,
        stats=stats_obj,
        current_date_iso=current_date_iso,
        period=period,
        granularity=granularity,
        timeline_by_day=timeline_by_day,  # Questa variabile deve contenere i dati della timeline
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
            reply_text = "✅ Iscritto alle notifiche! Riceverai un avviso se il tuo pet esce dal perimetro oppure entra in una stanza NON consentita o se la temperatura è fuori soglia."
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            if bot_token:
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
                # Supporto frame BINARI (video)
                if isinstance(message, (bytes, bytearray)):
                    for client in connected_clients.copy():
                        if client != websocket and client.close_code is None:
                            await client.send(message)
                    continue

                data = json.loads(message)
                # Comandi di controllo
                if data.get("type") == "control_command":
                    for client in connected_clients.copy():
                        if client != websocket and client.close_code is None:
                            await client.send(json.dumps(data))

                elif data.get("type") == "sensor_data":
                    # --- estrai coordinate GPS se presenti ---
                    lat = None
                    lon = None
                    if "gps" in data and isinstance(data["gps"], dict):
                        lat = data["gps"].get("lat")
                        lon = data["gps"].get("lon")
                    elif "lat" in data and "lon" in data:
                        lat = data.get("lat")
                        lon = data.get("lon")

                    # --- estrai possibili identificativi pet dal payload ---
                    pet_id = data.get("pet_id") or None
                    pet_mac = data.get("pet_mac") or data.get("mac") or None
                    pet_name = data.get("pet_name") or None

                    # Se abbiamo pet_id ma non pet_mac/name, proviamo a risolvere dal DB
                    if pet_id and (not pet_mac or not pet_name):
                        try:
                            pet_doc = db.get_pet_by_id(pet_id)
                            if pet_doc:
                                pet_mac = pet_mac or pet_doc.get("mac_address")
                                pet_name = pet_name or pet_doc.get("name")
                        except Exception:
                            pass

                    # Se abbiamo solo pet_mac proviamo a risolvere pet_id/name
                    if pet_mac and not pet_id:
                        try:
                            resolved_id, pet_doc = resolve_pet_by_mac(pet_mac)
                            if resolved_id:
                                pet_id = resolved_id
                            if pet_doc and not pet_name:
                                pet_name = pet_doc.get("name")
                        except Exception:
                            pass

                    # --- aggiorna latest_gps e inoltra un gps_update arricchito ---
                    if lat is not None and lon is not None:
                        latest_gps["lat"] = lat
                        latest_gps["lon"] = lon

                        gps_update = {
                            "type": "gps_update",
                            "lat": lat,
                            "lon": lon
                        }
                        if pet_id:
                            gps_update["pet_id"] = pet_id
                        if pet_mac:
                            gps_update["pet_mac"] = pet_mac
                        if pet_name:
                            gps_update["pet_name"] = pet_name

                        # inoltra a tutti i client WS
                        for client in connected_clients.copy():
                            if client.close_code is None:
                                await client.send(json.dumps(gps_update))

                    # --- temperatura / umidità (se presenti) ---
                    temp = None
                    hum = None
                    if "environmental" in data and isinstance(data["environmental"], dict):
                        temp = data["environmental"].get("temperature")
                        hum = data["environmental"].get("humidity")
                    if "temp" in data:
                        temp = data.get("temp")
                    if "hum" in data:
                        hum = data.get("hum")
                    timestamp = data.get("timestamp", int(time.time()))
                    if temp is not None or hum is not None:
                        # salva come global oppure per pet se pet_id presente
                        target_key = pet_id if pet_id else "global"
                        latest_env[target_key] = {"temp": temp, "hum": hum, "timestamp": timestamp}
                        db.save_env_data(target_key, temp, hum, datetime.fromtimestamp(timestamp, timezone.utc))
                        print(f"[WS] ENV aggiornata via WS: pet={target_key} temp={temp} hum={hum}")

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

def resolve_pet_by_mac(pet_mac: str):
    """Trova (pet_id_str, pet_doc) dato il MAC del pet; altrimenti (None, None)."""
    pet = db.pets.find_one({"mac_address": pet_mac})
    return (str(pet["_id"]), pet) if pet else (None, None)

def room_allowed_for_anchor(anchor_id: str):
    """
    Determina se la stanza/ancora è consentita:
    - match per name == anchor_id
    - in alternativa, match per mac_address usando anchors_online
    Ritorna (allowed_bool, room_doc or None).
    """
    room = db.rooms.find_one({"name": anchor_id})
    if room:
        return bool(room.get("allowed", True)), room

    anchor_mac = None
    for mac, info in anchors_online.items():
        if info.get("anchor_id") == anchor_id:
            anchor_mac = mac
            break
    if anchor_mac:
        room = db.rooms.find_one({"mac_address": anchor_mac})
        if room:
            return bool(room.get("allowed", True)), room

    return True, None

def on_mqtt_connect(client, userdata, flags, rc):
    print(f"[MQTT] Connessione: {rc}")
    client.subscribe(MQTT_ANCHORS_TOPIC)
    client.subscribe("tracker/+/+")

def on_mqtt_message(client, userdata, msg):
    global main_asyncio_loop, latest_env
    global current_is_outside, current_temp_high, current_temp_low, current_temp_value, current_temp_min, current_temp_max, latest_gps
    global current_in_restricted_room, current_restricted_room, last_ble_state

    try:
        payload = msg.payload.decode()
        print(f"[MQTT] Messaggio su {msg.topic}: {payload}")

        # --- Ancore BLE si annunciano ---
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

        # --- RSSI da ancora: tracker/<anchorID>/<petMAC> ---
        if msg.topic.startswith("tracker/") and len(msg.topic.split("/")) == 3:
            _, anchor_id, pet_mac = msg.topic.split("/")
            try:
                data = json.loads(payload)
                rssi = data.get("rssi")
                bt_name = data.get("bt_name", "")
                if rssi is not None:
                    rssi = float(rssi)
                    update_rssi_window(anchor_id, pet_mac, rssi, bt_name=bt_name)

                    # calcola media solo se finestra piena (3 campioni)
                    win = rssi_windows.get((anchor_id, pet_mac))
                    if win and len(win) == 3:
                        avg = sum(win) / 3.0
                        now_t = time.time()

                        if avg >= RSSI_THRESHOLD:
                            pet_id, pet_doc = resolve_pet_by_mac(pet_mac)
                            if pet_id:
                                last = last_ble_state.get(pet_mac)
                                if (not last) or (last["room"] != anchor_id) or (now_t - last["t"] >= BLE_EVENT_COOLDOWN_SEC):
                                    allowed, room_doc = room_allowed_for_anchor(anchor_id)
                                    entry_type = "stanza_accessibile" if allowed else "stanza_non_accessibile"
                                    try:
                                        db.save_position(
                                            pet_id=pet_id,
                                            entry_type=entry_type,
                                            source="ble",
                                            room=anchor_id,
                                            rssi=avg,
                                            bt_name=bt_name
                                        )
                                    except Exception as e:
                                        print("[BLE SAVE] errore:", e)

                                    # Notifica se stanza NON accessibile
                                    if not allowed:
                                        current_in_restricted_room = True
                                        current_restricted_room = room_doc["name"] if room_doc else anchor_id
                                        pet_name = pet_doc.get("name", "")
                                        gps_str = f"{latest_gps['lat']}, {latest_gps['lon']}" if latest_gps.get("lat") and latest_gps.get("lon") else None

                                        notify_events(
                                            is_outside=current_is_outside,
                                            temp_high=current_temp_high,
                                            temp_low=current_temp_low,
                                            gps=gps_str,
                                            temp_value=current_temp_value,
                                            temp_min=current_temp_min,
                                            temp_max=current_temp_max,
                                            ble_restricted=True,
                                            room=current_restricted_room,
                                            rssi=round(avg, 1),
                                            pet_name=pet_name,
                                            pet_mac=pet_mac
                                        )
                                    else:
                                        current_in_restricted_room = False
                                        current_restricted_room = None

                                    last_ble_state[pet_mac] = {"room": anchor_id, "t": now_t, "avg": avg}
            except Exception as e:
                print("Errore parsing RSSI:", e)
            return

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