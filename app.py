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

from dotenv import load_dotenv
load_dotenv()

import os

ESP32CAM_IP = os.getenv("ESP32CAM_IP")
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGODB_CONN_STRING = os.getenv("MONGODB_CONN_STRING")

ESP32_STREAM_PATH = "/stream"
ESP32_CONTROL_PATH = "/control"


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
seen_devices = {}

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

# Rolling window RSSI per localizzazione BLE 
rssi_windows = {}  # key: (anchor_id, pet_mac) -> [RSSI, RSSI, RSSI]
pet_room_estimate = {}  # pet_mac -> {"room": anchor_id, "avg_rssi": ..., ...}

# Parametri stima presenza in stanza
RSSI_THRESHOLD = float(os.getenv("BLE_RSSI_THRESHOLD", "-100"))   # calibra in casa tua
BLE_EVENT_COOLDOWN_SEC = int(os.getenv("BLE_EVENT_COOLDOWN_SEC", "5"))
last_ble_state = {}  # pet_mac -> {"room": str, "t": float, "avg": float}

# Stato per notifiche stanza non consentita 
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

def update_rssi_window(anchor_id, pet_mac_norm, rssi, bt_name=None):
    """
    Mantiene la rolling window (3 campioni) per coppia (anchor_id, pet_mac_norm)
    e aggiorna pet_room_estimate SOLO per pet_mac_norm registrati.
    """
    key = (anchor_id, pet_mac_norm)
    arr = rssi_windows.setdefault(key, [])
    arr.append(rssi)
    if len(arr) > 3:
        arr.pop(0)
    # debug
    print(f"[RSSI-WIN] key={key} window={arr}")
    if len(arr) == 3:
        avg = sum(arr) / 3.0
        prev = pet_room_estimate.get(pet_mac_norm)
        if not prev or avg > prev["avg_rssi"]:
            pet_room_estimate[pet_mac_norm] = {
                "room": anchor_id,
                "avg_rssi": avg,
                "bt_name": bt_name,
                "last_seen": time.time()
            }
            print(f"[BLE-LOC] Pet {pet_mac_norm} stimato in stanza ancora {anchor_id} (RSSI medio {avg:.1f})")

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
            # Login 
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


@app.route('/add_pet', methods=['GET', 'POST'])
@login_required
def add_pet():
    user = auth_manager.get_user_info(session['username'])

    def normalize_mac(mac: str):
        if not mac:
            return None
        m = str(mac).strip().upper()
        # rimuovi separatori non standard
        m = m.replace("-", "").replace(" ", "").replace(":", "")
        if len(m) != 12:
            return m  # fallback: ritorna upper-case così com'è
        return ':'.join(m[i:i+2] for i in range(0, 12, 2))

    if request.method == 'POST':
        pet_name = request.form['pet_name']
        mac_address = request.form.get('mac_address')
        bt_name = request.form.get('bt_name')
        temp_min = request.form.get('temp_min')
        temp_max = request.form.get('temp_max')

        mac_norm = normalize_mac(mac_address) if mac_address else None

        db.add_pet(
            name=pet_name,
            owner_id=user['_id'],
            mac_address=mac_norm,
            bt_name=bt_name
        )

        if temp_min or temp_max:
            # cerchiamo il pet creato (per owner e MAC normalizzato)
            pet = db.pets.find_one({"owner_id": str(user['_id']), "mac_address": mac_norm})
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

    def normalize_mac(mac: str):
        if not mac:
            return None
        m = str(mac).strip().upper()
        m = m.replace("-", "").replace(" ", "").replace(":", "")
        if len(m) != 12:
            return m
        return ':'.join(m[i:i+2] for i in range(0, 12, 2))

    if request.method == 'POST':
        pet_name = request.form['pet_name']
        mac_address = request.form.get('mac_address')
        bt_name = request.form.get('bt_name')
        temp_min = request.form.get('temp_min')
        temp_max = request.form.get('temp_max')

        mac_norm = normalize_mac(mac_address) if mac_address else None

        db.update_pet(
            pet_id,
            name=pet_name,
            mac_address=mac_norm,
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


@app.route('/config_area_main', methods=['GET'])
@login_required
def config_area_main():
    user = auth_manager.get_user_info(session['username'])
    rooms = db.get_rooms()
    perimeter_center = db.get_perimeter_center() or (45.123456, 9.123456)
    perimeter_radius = db.get_perimeter_radius() or 50
    gps = latest_gps
    pet_position = (float(gps['lat']), float(gps['lon'])) if gps and gps['lat'] and gps['lon'] else None

    # --- tabella di selezione ---
    try:
        pets = db.get_pets_for_user(user['_id'])
    except Exception:
        pets = []

    return render_template(
        'config_area_main.html',
        user=user,
        rooms=rooms,
        perimeter_center=perimeter_center,
        perimeter_radius=int(perimeter_radius),
        pet_position=pet_position,
        pets=pets
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


# stanza accessibile / non accessibile
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
    

#  risolve anchor_id -> nome stanza leggibile 
def resolve_room_name(anchor_id: str):
    if not anchor_id:
        return None
    try:
        # 1) prova a trovare una stanza il cui campo 'name' corrisponda ad anchor_id
        room = db.rooms.find_one({"name": anchor_id})
        if room and room.get("name"):
            return room.get("name")
        # 2) altrimenti prova a trovare la mac corrispondente in anchors_online
        anchor_mac = None
        for mac, info in anchors_online.items():
            if info.get("anchor_id") == anchor_id:
                anchor_mac = mac
                break
        if anchor_mac:
            room2 = db.rooms.find_one({"mac_address": anchor_mac})
            if room2 and room2.get("name"):
                return room2.get("name")
    except Exception:
        pass
    
    return anchor_id


# helper per normalizzare MAC 
def normalize_mac(mac: str):
    if not mac:
        return None
    m = str(mac).strip().upper()
    # rimuovi separatori non standard
    m = m.replace("-", "").replace(" ", "").replace(":", "")
    if len(m) != 12:
        return m  # fallback: ritorna upper-case così com'è
    return ':'.join(m[i:i+2] for i in range(0, 12, 2))


@app.route('/get_pet_location/<pet_id>')
@login_required
def get_pet_location(pet_id):
    pet = db.get_pet_by_id(pet_id)
    if not pet:
        return jsonify({"error": "Pet non trovato"}), 404

    mac = pet.get("mac_address")
    if not mac:
        return jsonify({"error": "Nessun MAC associato"}), 404

    now = time.time()
    cutoff = now - 5 * 60  # 5 minuti

    # --- CERCA BLE (per pet_id) ---
    last_ble = db.positions.find_one(
        {"pet_id": str(pet_id), "source": "ble"},
        sort=[("timestamp", -1)]
    )
    if last_ble:
        ts = last_ble.get("timestamp")
        if isinstance(ts, str):
            ts = isoparse(ts)
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts and ts.timestamp() >= cutoff:
            room_name = resolve_room_name(last_ble.get("room"))
            return jsonify({
                "room": room_name or last_ble.get("room"),
                "room_last_seen": int(ts.timestamp())
            })

    # --- CERCA GPS COME FALLBACK ---
    last_gps = db.positions.find_one(
        {"pet_mac": normalize_mac(mac), "source": "gps"},
        sort=[("timestamp", -1)]
    )
    if last_gps and last_gps.get("lat") and last_gps.get("lon"):
        ts = last_gps.get("timestamp")
        if isinstance(ts, str):
            ts = isoparse(ts)
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts and ts.timestamp() >= cutoff:
            gps_data = {"lat": float(last_gps["lat"]), "lon": float(last_gps["lon"])}
            return jsonify({
                "gps": gps_data,
                "gps_last_seen": int(ts.timestamp())
            })

    # --- SE NON TROVA NULLA ---
    return jsonify({
        "none": True,
        "message": "Nessuna posizione disponibile"
    })


    
@app.route('/update_temp_thresholds/<pet_id>', methods=['POST'])
@login_required
def update_temp_thresholds(pet_id):
    temp_min = request.form.get('temp_min')
    temp_max = request.form.get('temp_max')
    print(f"[DEBUG] update_temp_thresholds called for pet_id={pet_id} raw_min={temp_min} raw_max={temp_max}")
    try:
        temp_min_f = float(temp_min) if temp_min not in (None, "") else None
        temp_max_f = float(temp_max) if temp_max not in (None, "") else None
    except Exception as e:
        print("[DEBUG] parsing error:", e)
        flash("Errore nei valori inseriti (usa solo numeri)", "danger")
        return redirect(url_for('dashboard_pet', pet_id=pet_id))

    pet_before = None
    try:
        pet_before = db.get_pet_by_id(pet_id)
    except Exception as e:
        print("[DEBUG] Errore lettura pet prima update:", e)

    mac_before = None
    if pet_before:
        mac_before = pet_before.get("mac_address")
        mac_before = normalize_mac(mac_before) if mac_before else None
    print(f"[DEBUG] pet before update: id={pet_id} mac={mac_before} doc={pet_before}")

    # salva le soglie nel documento pet (associato all'_id)
    db.update_pet(pet_id, temp_min=temp_min_f, temp_max=temp_max_f)
    print(f"[DEBUG] saved thresholds temp_min={temp_min_f} temp_max={temp_max_f} for pet_id={pet_id}")

    # Verifica post-salvataggio
    pet_after = None
    try:
        pet_after = db.get_pet_by_id(pet_id)
    except Exception as e:
        print("[DEBUG] Errore lettura pet dopo update:", e)
    print(f"[DEBUG] pet after update: {pet_after}")

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

    # Normalizza timestamp e risolvi eventuali nomi stanza leggibili (se p["room"] è un anchor_id)
    for p in positions:
        ts = p.get('timestamp')
        if isinstance(ts, str):
            ts = isoparse(ts)
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        p['timestamp'] = ts
        p['timestamp_rome'] = ts.astimezone(rome) if ts else None

        # Risolvi room -> nome leggibile se presente
        if p.get("room"):
            try:
                p["room"] = resolve_room_name(p.get("room"))
            except Exception:
                pass

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
            # Trova la stanza (nome leggibile) con più tempo totale
            top_room = max(room_times.items(), key=lambda x: x[1])[0]
            stats_obj['top_room'] = top_room
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

# ---------- HELPERS ----------
@staticmethod
def normalize_mac(mac: str):
    """Normalizza un MAC in formato AA:BB:CC:DD:EE:FF (MAIUSCOLO)."""
    if not mac:
        return None
    m = str(mac).strip().upper()
    # rimuove separatori non standard
    m = m.replace("-", "").replace(" ", "").replace(":", "")
    if len(m) != 12:
        return m  # fallback, ritorna upper-case così com'è
    return ':'.join(m[i:i+2] for i in range(0, 12, 2))


def resolve_pet_by_mac(pet_identifier: str):
    """
    Prova a risolvere pet a partire da MAC (normalizzato) o da bt_name.
    Restituisce (pet_id_str, pet_doc) o (None, None)
    """
    if not pet_identifier:
        return (None, None)
    # prima come MAC normalizzato
    mac_cand = db._normalize_mac(pet_identifier) if hasattr(db, "_normalize_mac") else normalize_mac(pet_identifier)
    if mac_cand:
        pet = db.pets.find_one({"mac_address": mac_cand})
        if pet:
            return (str(pet["_id"]), pet)
    # fallback bt_name case-insensitive
    try:
        pet = db.pets.find_one({"bt_name": pet_identifier})
        if pet:
            return (str(pet["_id"]), pet)
        pet = db.pets.find_one({"bt_name": {"$regex": f'^{pet_identifier}$', "$options": "i"}})
        if pet:
            return (str(pet["_id"]), pet)
    except Exception:
        pass
    return (None, None)

@app.route('/detected_pets')
@login_required
def detected_pets():
    # Recupera tutti i MAC già associati a un pet (normalizzati)
    registered_macs = set()
    try:
        for p in db.pets.find():
            mac = p.get("mac_address")
            if mac:
                registered_macs.add(mac)
    except Exception as e:
        print("[DETECTED_PETS] Errore lettura pets dal DB:", e)

    # Costruiamo lista mostrata in UI partendo da seen_devices per includere i non-registrati
    pets = []
    cutoff = time.time() - 300  # mostra solo dispositivi visti negli ultimi 5 minuti
    for mac, info in seen_devices.items():
        if mac in registered_macs:
            continue  # escludi già registrati (non devono comparire nella lista "da registrare")
        if info.get("last_seen", 0) < cutoff:
            continue
        pets.append({
            "mac_address": mac,
            "bt_name": info.get("bt_name", ""),
            "rssi": info.get("last_rssi"),
            "anchor_id": info.get("last_anchor"),
            "last_seen": info.get("last_seen")
        })
    pets.sort(key=lambda p: p.get("rssi", -200), reverse=True)
    return jsonify({"pets": pets})


@app.route('/localizza')
@login_required
def localizza():
    user = auth_manager.get_user_info(session['username'])
    pets = db.get_pets_for_user(user['_id'])

    simple_pets = []
    for p in pets:
        mac = p.get("mac_address")
        gps_data = None
        gps_available = False

        if mac:
            last_pos = db.positions.find_one({"pet_mac": mac}, sort=[("timestamp", -1)])
            if last_pos and last_pos.get("lat") and last_pos.get("lon"):
                gps_data = {"lat": float(last_pos["lat"]), "lon": float(last_pos["lon"])}
                gps_available = True

        simple_pets.append({
            "_id": str(p.get("_id")),
            "name": p.get("name", "Senza nome"),
            "mac_address": mac,
            "gps_available": gps_available,
            "gps": gps_data
        })

    return render_template('localizza.html', pets=simple_pets)

@app.route('/get_latest_env/<pet_id>')
@login_required
def get_latest_env(pet_id):
    """
    Restituisce l'ultima lettura ambientale cercando, nell'ordine:
      1) MAC associato al pet (formato normalizzato con :)
      2) pet_id (l'_id del documento pet)
      3) fallback "global"
    """
    try:
        # 1) prova per MAC associato al pet 
        env = None
        try:
            pet = db.get_pet_by_id(pet_id)
        except Exception:
            pet = None

        if pet and pet.get("mac_address"):
            mac_norm = normalize_mac(pet.get("mac_address"))
            if mac_norm:
                env = latest_env.get(mac_norm) or db.get_latest_env(mac_norm)
            # prova anche senza i due punti (AABBCC..), nel caso in cui in DB sia salvato così
            if not env and mac_norm and ':' in mac_norm:
                mac_nocolon = mac_norm.replace(":", "")
                env = latest_env.get(mac_nocolon) or db.get_latest_env(mac_nocolon)

        # 2) se non trovato tramite MAC, prova per pet_id (chiave dell'app)
        if not env:
            key_pet = str(pet_id)
            env = latest_env.get(key_pet) or db.get_latest_env(key_pet)

        # 3) fallback globale
        if not env:
            env = latest_env.get("global") or db.get_latest_env("global")

        if not env:
            print(f"[GET_LATEST_ENV] Nessun env trovato (pet_id={pet_id})")
            return jsonify({"temp": None, "hum": None})

        # assicurati di ritornare numeri o null
        try:
            temp_v = float(env.get("temp")) if env.get("temp") is not None else None
        except Exception:
            temp_v = None
        try:
            hum_v = float(env.get("hum")) if env.get("hum") is not None else None
        except Exception:
            hum_v = None

        return jsonify({
            "temp": temp_v,
            "hum": hum_v,
            "timestamp": env.get("timestamp")
        })
    except Exception as e:
        print("[GET_LATEST_ENV] Errore:", e)
        return jsonify({"temp": None, "hum": None})



async def websocket_handler(websocket):
    """
    WebSocket handler :
      - inoltra i frame binari (streaming) a tutti i client
      - gestisce control_command e frame testuali come prima
      - normalizza il MAC se possibile
      - aggiorna latest_gps per diagnostica ma salva in DB SOLO se risolto pet_id
      - inoltra gps_update ai client SOLO se il messaggio contiene pet_id o pet_mac (evita gps anonimi che spostano marker)
    """
    global latest_gps
    global current_is_outside, current_temp_high, current_temp_low, current_temp_value, current_temp_min, current_temp_max
    print(f"📡 Nuova connessione WebSocket da {websocket.remote_address}")
    connected_clients.add(websocket)
    try:
        async for message in websocket:
            try:
                # FRAME BINARI (video) -> inoltra a tutti gli altri client
                if isinstance(message, (bytes, bytearray)):
                    for client in connected_clients.copy():
                        if client != websocket and getattr(client, "close_code", None) is None:
                            try:
                                await client.send(message)
                            except Exception as e:
                                print("[WS BIN FORWARD] Errore invio frame binario a client:", e)
                    continue

                # JSON testuale
                try:
                    data = json.loads(message)
                except Exception:
                    print("❌ JSON non valido ricevuto via WS (testuale)")
                    continue

                # control_command -> inoltra a tutti gli altri client
                if data.get("type") == "control_command":
                    for client in connected_clients.copy():
                        if client != websocket and getattr(client, "close_code", None) is None:
                            try:
                                await client.send(json.dumps(data))
                            except Exception as e:
                                print("[WS CMD] Errore invio control_command:", e)
                    continue

                # sensor_data -> gestione gps / env / ecc.
                if data.get("type") == "sensor_data":
                    # estrai GPS
                    lat = None
                    lon = None
                    if "gps" in data and isinstance(data["gps"], dict):
                        lat = data["gps"].get("lat")
                        lon = data["gps"].get("lon")
                    elif "lat" in data and "lon" in data:
                        lat = data.get("lat")
                        lon = data.get("lon")

                    # estrai identificatori (supporta pet_mac_ble / pet_mac / mac)
                    pet_id = data.get("pet_id") or None
                    pet_mac_raw = data.get("pet_mac_ble") or data.get("pet_mac") or data.get("mac") or None
                    # normalizza MAC se esiste helper normalize_mac, altrimenti minimo fallback
                    if pet_mac_raw and callable(globals().get("normalize_mac")):
                        pet_mac = normalize_mac(pet_mac_raw)
                    elif pet_mac_raw:
                        s = str(pet_mac_raw).strip().upper().replace("-", "").replace(".", "").replace(":", "").replace(" ", "")
                        pet_mac = ':'.join([s[i:i+2] for i in range(0, 12, 2)]) if len(s) == 12 else s
                    else:
                        pet_mac = None

                    reported_bt_name = data.get("pet_name") or data.get("bt_name") or None
                    pet_doc = None

                    # se abbiamo pet_id cerco doc sul DB e aggiorno pet_mac se disponibile
                    if pet_id:
                        try:
                            pet_doc = db.get_pet_by_id(pet_id)
                            if pet_doc and pet_doc.get("mac_address"):
                                if callable(globals().get("normalize_mac")):
                                    pet_mac = normalize_mac(pet_doc.get("mac_address"))
                                else:
                                    rawm = str(pet_doc.get("mac_address")).strip().upper()
                                    rawm = rawm.replace("-", "").replace(".", "").replace(":", "").replace(" ", "")
                                    pet_mac = ':'.join([rawm[i:i+2] for i in range(0, 12, 2)]) if len(rawm) == 12 else rawm
                        except Exception:
                            pet_doc = None

                    # se abbiamo solo mac, prova a risolvere pet_id/doc
                    if pet_mac and not pet_doc:
                        try:
                            resolved_id, resolved_doc = resolve_pet_by_mac(pet_mac)
                            if resolved_id:
                                pet_id = pet_id or resolved_id
                                pet_doc = resolved_doc
                        except Exception:
                            pass

                    #  nome DB
                    pet_name = pet_doc.get("name") if pet_doc and pet_doc.get("name") else reported_bt_name

                    # --- GPS handling: aggiorna latest_gps, salva SOLO se pet_id risolto, inoltra SOLO se identificato ---
                    if lat is not None and lon is not None:
                        lat_f = None
                        lon_f = None
                        try:
                            lat_f = float(lat)
                            lon_f = float(lon)
                        except Exception:
                            print("[WS-GPS] Coordinate non numeriche ricevute:", lat, lon)

                        if lat_f is not None and lon_f is not None:
                            # aggiorna latest_gps per diagnostica/server (non è assegnazione automatica a pet)
                            latest_gps["lat"] = lat_f
                            latest_gps["lon"] = lon_f
                            latest_gps["ts"] = int(time.time())

                            gps_update = {
                                "type": "gps_update",
                                "lat": lat_f,
                                "lon": lon_f
                            }
                            if pet_id:
                                gps_update["pet_id"] = pet_id
                            if pet_mac:
                                gps_update["pet_mac"] = pet_mac
                            if pet_name:
                                gps_update["pet_name"] = pet_name

                            # perimetro: calcola inside/outside solo per notifiche (non per associare posizione)
                            try:
                                perimeter_center = db.get_perimeter_center() or (45.123456, 9.123456)
                                perimeter_radius = db.get_perimeter_radius() or 50
                                lat_centro, lon_centro = perimeter_center
                                inside = is_inside_circle(lat_f, lon_f, lat_centro, lon_centro, perimeter_radius)
                            except Exception as e:
                                print("[WS-GPS] Errore lettura perimetro:", e)
                                inside = True

                            entry_type = "zona_esterna_accessibile" if inside else "zona_esterna_non_accessibile"

                            # SALVATAGGIO: salva la posizione GPS nel DB SOLO se abbiamo pet_id risolto
                            if pet_id:
                                save_kwargs = {"lat": lat_f, "lon": lon_f, "source": "gps"}
                                if pet_mac:
                                    save_kwargs["pet_mac"] = pet_mac
                                try:
                                    db.save_position(pet_id=pet_id, entry_type=entry_type, **save_kwargs)
                                except Exception as e:
                                    print("[WS-GPS] Errore salvataggio posizione:", e)
                            else:
                                # diagnostico: fix GPS anonimo -> non salvo
                                print("[WS-GPS] Fix GPS ricevuto ma nessuna associazione pet (no pet_id): non salvo in DB.")

                            # Inoltro ai client: SOLO se abbiamo pet_id o pet_mac (evita gps anonimi che spostano marker)
                            if pet_id or pet_mac:
                                for client in connected_clients.copy():
                                    if getattr(client, "close_code", None) is None:
                                        try:
                                            await client.send(json.dumps(gps_update))
                                        except Exception as e:
                                            print("[WS SEND] Errore invio gps_update:", e)
                            else:
                                # non inoltrare gps anonimo ai client
                                print("[WS-GPS] GPS anonimo ricevuto: non inoltrato ai client per evitare sovrascritture globali.")

                            # NOTIFICA SOLO SU TRANSIZIONE per evitare spam ripetuto 
                            try:
                                if pet_id:
                                    prev_outside = current_is_outside
                                    # aggiorna stato globale (utile ma notifiche per pet vanno legate a pet_id)
                                    current_is_outside = not inside
                                    gps_str = f"{lat_f:.6f}, {lon_f:.6f}"

                                    # aggiorna soglie temperatura pet-specifiche se abbiamo il documento
                                    if pet_doc:
                                        current_temp_min = pet_doc.get("temp_min")
                                        current_temp_max = pet_doc.get("temp_max")

                                    if current_is_outside != prev_outside:
                                        print(f"[WS-GPS] Transizione perimetro (prev={prev_outside} now={current_is_outside}), invio notify_events")
                                        notify_events(
                                            is_outside=current_is_outside,
                                            temp_high=current_temp_high,
                                            temp_low=current_temp_low,
                                            gps=gps_str,
                                            temp_value=current_temp_value,
                                            temp_min=current_temp_min,
                                            temp_max=current_temp_max,
                                            pet_mac=pet_mac,
                                            pet_name=pet_name
                                        )
                            except Exception as e:
                                print("[WS-GPS] Errore notify_events:", e)

                    # --- environmental (temp/hum) ---
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
                        # converti i valori numerici
                        try:
                            temp_f = float(temp) if temp is not None else None
                        except Exception:
                            temp_f = None
                        try:
                            hum_f = float(hum) if hum is not None else None
                        except Exception:
                            hum_f = None

                        # preferiamo mac normalizzato come key, altrimenti pet_id, altrimenti global
                        raw_mac = data.get("pet_mac_ble") or data.get("pet_mac") or data.get("mac") or None
                        if raw_mac and callable(globals().get("normalize_mac")):
                            mac_norm = normalize_mac(raw_mac)
                        elif raw_mac:
                            s = str(raw_mac).strip().upper().replace("-", "").replace(".", "").replace(":", "").replace(" ", "")
                            mac_norm = ':'.join([s[i:i+2] for i in range(0, 12, 2)]) if len(s) == 12 else s
                        else:
                            mac_norm = None

                        # prova a risolvere pet_doc a partire dal MAC normalizzato o pet_id
                        pet_doc_env = None
                        pet_id_env = None
                        if mac_norm:
                            pet_doc_env = db.pets.find_one({"mac_address": mac_norm})
                            if pet_doc_env:
                                pet_id_env = str(pet_doc_env["_id"])
                        if not pet_doc_env and data.get("pet_id"):
                            try:
                                pet_doc_env = db.get_pet_by_id(data.get("pet_id"))
                                if pet_doc_env:
                                    pet_id_env = str(pet_doc_env["_id"])
                                    if callable(globals().get("normalize_mac")) and pet_doc_env.get("mac_address"):
                                        mac_norm = normalize_mac(pet_doc_env.get("mac_address"))
                            except Exception:
                                pet_doc_env = None

                        if mac_norm:
                            target_key = mac_norm
                        elif pet_id_env:
                            target_key = pet_id_env
                        else:
                            target_key = "global"

                        latest_env[target_key] = {"temp": temp_f, "hum": hum_f, "timestamp": timestamp}
                        try:
                            db.save_env_data(target_key, temp_f, hum_f, datetime.fromtimestamp(timestamp, timezone.utc))
                        except Exception as e:
                            print("[WS-ENV] Errore salvataggio env:", e)

                        print(f"[WS-ENV] Received env: target_key={target_key} temp={temp_f} hum={hum_f} mac_norm={mac_norm}")
                        # 🔥 Inoltra aggiornamento ENV in tempo reale ai client dashboard
                        try:
                            env_update = {
                                "type": "env_update",
                                "pet_mac": mac_norm,
                                "temp": temp_f,
                                "hum": hum_f
                            }
                            import json
                            for client in connected_clients.copy():
                                if getattr(client, "close_code", None) is None:
                                    try:
                                        await client.send(json.dumps(env_update))
                                    except Exception as e:
                                        print("[WS-ENV] Errore invio env_update:", e)
                        except Exception as e:
                            print("[WS-ENV] Errore broadcast env_update:", e)

                        # Valuta soglie se pet_doc_env presente
                        try:
                            if pet_doc_env:
                                p_name = pet_doc_env.get("name")
                                p_min = pet_doc_env.get("temp_min")
                                p_max = pet_doc_env.get("temp_max")
                                if temp_f is not None and (p_min is not None or p_max is not None):
                                    over_high = (p_max is not None and temp_f > float(p_max))
                                    under_low = (p_min is not None and temp_f < float(p_min))
                                    if over_high or under_low:
                                        print(f"[WS-ENV] Soglia superata per pet {p_name or mac_norm}: temp={temp_f} - invio notify")
                                        gps_str = None
                                        if latest_gps.get("lat") and latest_gps.get("lon"):
                                            try:
                                                gps_str = f"{float(latest_gps['lat']):.6f}, {float(latest_gps['lon']):.6f}"
                                            except Exception:
                                                gps_str = f"{latest_gps.get('lat')}, {latest_gps.get('lon')}"
                                        notify_events(
                                            is_outside=current_is_outside,
                                            temp_high=over_high,
                                            temp_low=under_low,
                                            gps=gps_str,
                                            temp_value=temp_f,
                                            temp_min=p_min,
                                            temp_max=p_max,
                                            pet_mac=mac_norm,
                                            pet_name=p_name
                                        )
                                    else:
                                        print(f"[WS-ENV] Temperatura per {p_name or mac_norm} OK: {temp_f}°C (min={p_min} max={p_max})")
                            else:
                                print("[WS-ENV] Nessun pet trovato per il MAC ricevuto; nessuna soglia valutata.")
                        except Exception as e:
                            print("[WS-ENV] Errore controllo soglie:", e)

                elif data.get("type") == "frame":
                    for client in connected_clients.copy():
                        if client != websocket and getattr(client, "close_code", None) is None:
                            try:
                                await client.send(message)
                            except Exception as e:
                                print("[WS FRAME] Errore inoltro frame:", e)

            except json.JSONDecodeError:
                print("❌ JSON non valido")
            except Exception as e:
                print("❌ Errore interno handler WS:", e)
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
        #print(f"[MQTT] Messaggio su {msg.topic}: {payload}")

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
            _, anchor_id, pet_mac_raw = msg.topic.split("/")
            try:
                data = json.loads(payload)
                rssi = data.get("rssi")
                bt_name = data.get("bt_name", "") or ""
                if rssi is not None:
                    rssi = float(rssi)

                    # normalizza MAC ricevuto
                    pet_mac = normalize_mac(pet_mac_raw)

                    # Aggiorna la mappa di tutti i dispositivi visti (per la UI / registrazione)
                    seen_devices[pet_mac] = {
                        "bt_name": bt_name,
                        "last_seen": time.time(),
                        "last_anchor": anchor_id,
                        "last_rssi": rssi
                    }

                    # Verifica se il MAC è registrato nel DB: SOLO IN TAL CASO costruiamo/aggiorniamo la finestra a 3 campioni
                    pet_doc = db.pets.find_one({"mac_address": pet_mac})
                    if not pet_doc:
                        # Non registrato: non creare la rolling window, skip della logica di localizzazione/storico
                        #print(f"[MQTT] dispositivo non registrato: {pet_mac} (skip window).")
                        return
                    # Se è registrato: aggiorna window e procedi con la logica
                    update_rssi_window(anchor_id, pet_mac, rssi, bt_name=bt_name)

                    # leggi la finestra per questa coppia (anchor,mac)
                    win = rssi_windows.get((anchor_id, pet_mac))
                    if win and len(win) == 3:
                        avg = sum(win) / 3.0
                        now_t = time.time()
                        #print(f"[MQTT] AVG calcolata per {pet_mac} su ancora {anchor_id}: {avg:.1f} dBm (threshold {RSSI_THRESHOLD})")
                        if avg >= RSSI_THRESHOLD:
                            # Risolvi pet_id dal documento (userà ObjectId string)
                            pet_id = str(pet_doc["_id"])
                            last = last_ble_state.get(pet_mac)
                            if (not last) or (last["room"] != anchor_id) or (now_t - last["t"] >= BLE_EVENT_COOLDOWN_SEC):
                                allowed, room_doc = room_allowed_for_anchor(anchor_id)
                                entry_type = "stanza_accessibile" if allowed else "stanza_non_accessibile"
                                # --- risolvi il nome della stanza dall'anchor MAC ---
                                room_name = anchor_id  # valore di default
                                try:
                                    # cerca se l'anchor è registrata in anchors_online e trova il suo MAC
                                    anchor_mac = None
                                    for mac, info in anchors_online.items():
                                        if info.get("anchor_id") == anchor_id:
                                            anchor_mac = mac
                                            break

                                    # se trovato, cerca nella collezione rooms
                                    if anchor_mac:
                                        room_doc = db.rooms.find_one({"mac_address": anchor_mac})
                                        if room_doc and room_doc.get("name"):
                                            room_name = room_doc["name"]
                                except Exception as e:
                                    print(f"[BLE MAP] Errore durante lookup stanza per anchor {anchor_id}: {e}")

                                # --- risolvi il nome del pet partendo da bt_name ---
                                pet_name_final = bt_name
                                try:
                                    pet_doc = db.pets.find_one({"bt_name": bt_name})
                                    if pet_doc and pet_doc.get("name"):
                                        pet_name_final = pet_doc["name"]
                                except Exception as e:
                                    print(f"[BLE MAP] Errore durante lookup pet name per {bt_name}: {e}")

                                # --- ora salva con i campi risolti ---
                                try:
                                    db.save_position(
                                        pet_id=pet_id,
                                        entry_type=entry_type,
                                        source="ble",
                                        room=room_name,   
                                        rssi=avg,
                                        pet_name=pet_name_final  
                                    )
                                    print(f"[BLE SAVE] OK: stanza={room_name}, pet={pet_name_final}, rssi={avg:.1f}")
                                except Exception as e:
                                    print("[BLE SAVE] ERRORE durante save_position:", e)

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