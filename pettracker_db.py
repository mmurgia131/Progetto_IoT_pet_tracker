from pymongo import MongoClient, ASCENDING, DESCENDING
from gridfs import GridFS
from datetime import datetime, timezone
from bson import ObjectId
import bcrypt

class PetTrackerDB:
    def __init__(self, connection_string="mongodb+srv://mariucciamurgia:LHbPpnHsUBC88W7a@cluster0.4wplnch.mongodb.net/"):
        try:
            self.client = MongoClient(connection_string, serverSelectionTimeoutMS=5000)
            self.client.admin.command('ping')
            print("✅ Connessione a MongoDB riuscita!")

            self.db = self.client.PetTracker
            self.users = self.db.users
            self.pets = self.db.pets
            self.positions = self.db.positions
            self.rooms = self.db.rooms
            self.perimeters = self.db.perimeters
            self.gridfs_images = GridFS(self.db, collection="pet_images")

            self._setup_indexes()
            print("✅ Indici DB creati")
        except Exception as e:
            print(f"❌ Connessione a MongoDB fallita: {e}")
            raise

    # --- Utils ---
    @staticmethod
    def _ensure_oid(value):
        """Restituisce un ObjectId a partire da una stringa o passa attraverso se è già ObjectId."""
        if isinstance(value, ObjectId):
            return value
        return ObjectId(value)

    def _setup_indexes(self):
        try:
            self.users.create_index([("username", ASCENDING)], unique=True)
            self.pets.create_index([("owner_id", ASCENDING)])
            # Consigliato: evitare duplicati MAC
            self.pets.create_index([("mac_address", ASCENDING)], unique=True, name="uniq_mac_address")
            self.positions.create_index([("pet_id", ASCENDING), ("timestamp", DESCENDING)])
            self.rooms.create_index([("owner_id", ASCENDING)])
            self.perimeters.create_index([("pet_id", ASCENDING)])
            self.perimeters.create_index([("key", ASCENDING)])
        except Exception as e:
            print(f"⚠️ Errore nella creazione degli indici: {e}")

    # --- UTENTI ---
    def create_user(self, username, password):
        """
        Crea un utente salvando la hash sotto il campo 'password_hash'.
        Usa bcrypt con costo rounds=12 per coerenza con il resto dell'app.
        """
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=12))
        return self.users.insert_one({
            "username": username,
            "password_hash": password_hash,
            "created_at": datetime.now(timezone.utc)
        }).inserted_id

    def get_user_by_username(self, username):
        return self.users.find_one({"username": username})

    def get_user_by_id(self, user_id):
        oid = self._ensure_oid(user_id)
        return self.users.find_one({"_id": oid})

    def authenticate_user(self, username, password):
        """
        Autenticazione utente:
        - legge preferibilmente 'password_hash'
        - mantiene retrocompatibilità se dovesse esistere ancora il campo 'password'
        - accetta sia valori bytes che string memorizzati (se necessario)
        """
        user = self.get_user_by_username(username)
        if not user:
            return None

        # preferisci 'password_hash', fallback a 'password' (per retrocompatibilità)
        stored = user.get('password_hash') if 'password_hash' in user else user.get('password')
        if not stored:
            return None

        # stored può essere bytes oppure string; assicurati di avere bytes per bcrypt
        if isinstance(stored, str):
            stored_bytes = stored.encode('utf-8')
        else:
            stored_bytes = stored

        try:
            if bcrypt.checkpw(password.encode('utf-8'), stored_bytes):
                return user
        except Exception as e:
            # in caso di valore non valido per bcrypt, falliamo l'autenticazione
            print(f"[AUTH] errore verifica password per {username}: {e}")
        return None

    # --- PETS ---
    def add_pet(self, name, owner_id, mac_address=None, bt_name=None, temp_min=None, temp_max=None):
        pet_data = {
            "name": name,
            "owner_id": str(owner_id),  # restiamo coerenti con i dati esistenti
            "created_at": datetime.now(timezone.utc),
        }
        if mac_address:
            pet_data["mac_address"] = mac_address
        if bt_name:
            pet_data["bt_name"] = bt_name
        if temp_min is not None:
            pet_data["temp_min"] = temp_min
        if temp_max is not None:
            pet_data["temp_max"] = temp_max
        return self.pets.insert_one(pet_data).inserted_id

    def get_pets_for_user(self, user_id):
        # coerente con l'uso della stringa per owner_id
        return list(self.pets.find({"owner_id": str(user_id)}))

    def get_pet_by_id(self, pet_id):
        oid = self._ensure_oid(pet_id)
        return self.pets.find_one({"_id": oid})

    def update_pet(self, pet_id, name=None, mac_address=None, bt_name=None, temp_min=None, temp_max=None):
        update_fields = {}
        if name is not None:
            update_fields["name"] = name
        if mac_address is not None:
            update_fields["mac_address"] = mac_address
        if bt_name is not None:
            update_fields["bt_name"] = bt_name
        if temp_min is not None:
            update_fields["temp_min"] = temp_min
        if temp_max is not None:
            update_fields["temp_max"] = temp_max
        if update_fields:
            oid = self._ensure_oid(pet_id)
            self.pets.update_one({"_id": oid}, {"$set": update_fields})

    def delete_pet(self, pet_id):
        oid = self._ensure_oid(pet_id)
        self.pets.delete_one({"_id": oid})

    # --- ROOMS (globali, per configurazione area/stanze) ---
    def get_rooms(self):
        """Restituisce tutte le stanze (ancore BT)."""
        return list(self.rooms.find({}))

    def add_room(self, name, mac_address, allowed):
        return self.rooms.insert_one({
            "name": name,
            "mac_address": mac_address,
            "allowed": allowed
        }).inserted_id

    def get_room_by_id(self, room_id):
        oid = self._ensure_oid(room_id)
        return self.rooms.find_one({"_id": oid})

    def update_room(self, room_id, name, mac_address, allowed):
        oid = self._ensure_oid(room_id)
        update_data = {"name": name, "mac_address": mac_address, "allowed": allowed}
        self.rooms.update_one({"_id": oid}, {"$set": update_data})

    def delete_room(self, room_id):
        oid = self._ensure_oid(room_id)
        self.rooms.delete_one({"_id": oid})

    def update_room_access(self, room_id, allowed):
        oid = self._ensure_oid(room_id)
        self.rooms.update_one({"_id": oid}, {"$set": {"allowed": allowed}})

    # --- PERIMETRO (globale per tutti i pet) ---
    def get_perimeter_center(self):
        perim = self.perimeters.find_one({"key": "global"})
        if perim and "center" in perim:
            return tuple(perim["center"])
        return None

    def get_perimeter_radius(self):
        perim = self.perimeters.find_one({"key": "global"})
        if perim and "radius" in perim:
            return perim["radius"]
        return None

    def save_perimeter(self, center, radius):
        self.perimeters.update_one(
            {"key": "global"},
            {"$set": {"center": center, "radius": radius}},
            upsert=True
        )

    def set_pet_allowed_rooms(self, pet_id, room_ids):
        oid = self._ensure_oid(pet_id)
        self.pets.update_one(
            {"_id": oid},
            {"$set": {"allowed_rooms": [str(r) for r in room_ids]}}
        )

    # --- POSIZIONI ---
    def save_pet_position(self, pet_id, lat, lon, timestamp=None):
        if not timestamp:
            timestamp = datetime.now(timezone.utc)
        self.positions.insert_one({
            "pet_id": str(pet_id),
            "lat": lat,
            "lon": lon,
            "timestamp": timestamp
        })

    def save_position(self, pet_id, entry_type, timestamp=None, **kwargs):
        if not timestamp:
            timestamp = datetime.now(timezone.utc)
        record = {
            # Store pet_id only if non-None to avoid string "None"
            "pet_id": str(pet_id) if pet_id is not None else None,
            "entry_type": entry_type,
            "timestamp": timestamp
        }
        record.update(kwargs)
        print("=== SALVATAGGIO POSIZIONE ===")
        print(record)
        try:
            self.positions.insert_one(record)
            print("=== SALVATAGGIO OK ===")
        except Exception as e:
            print("ERRORE SALVATAGGIO:", e)

    def get_last_position(self, pet_id):
        return self.positions.find_one(
            {"pet_id": str(pet_id)},
            sort=[("timestamp", DESCENDING)]
        )

    def get_positions(self, pet_id, limit=50):
        return list(self.positions.find({"pet_id": str(pet_id)}).sort("timestamp", DESCENDING).limit(limit))

    # --- IMMAGINI (opzionale, via GridFS) ---
    def save_pet_image(self, image_data, filename, pet_id):
        image_id = self.gridfs_images.put(
            image_data,
            filename=filename,
            pet_id=str(pet_id),
            upload_date=datetime.now(timezone.utc)
        )
        return image_id

    def get_pet_image(self, image_id):
        file = self.gridfs_images.get(self._ensure_oid(image_id))
        return file.read(), file.filename

    # --- ENV DATA ---
    def save_env_data(self, pet_id, temp, hum, timestamp):
        self.db.envdata.insert_one({
            "pet_id": str(pet_id) if pet_id else "global",
            "temp": temp,
            "hum": hum,
            "timestamp": timestamp
        })

    def get_latest_env(self, pet_id=None):
        # se pet_id non è passato o è "global", prendiamo il dato globale
        key = str(pet_id) if pet_id else "global"
        return self.db.envdata.find_one({"pet_id": key}, sort=[("timestamp", -1)])


    @staticmethod
    def is_inside_perimeter(lat, lon, area):
        try:
            from shapely.geometry import Point, Polygon
            point = Point(lon, lat)
            polygon = Polygon([(lng, lt) for lt, lng in area])  # area = [(lat, lon), ...]
            return polygon.contains(point)
        except ImportError:
            print("Installa shapely per usare questa funzione!")
            return False

    # --- Migrazione (opzionale) ---
    def migrate_password_field(self):
        """
        Operazione opzionale per migrare eventuali documenti che usano ancora il campo 'password'
        al campo 'password_hash' senza alterare il valore.
        Esegue $rename su tutti i documenti che contengono 'password' ma non 'password_hash'.
        ATTENZIONE: se il campo 'password' contiene una password in chiaro questo non la
        trasformerà in hash. Verificare lo stato prima di usare.
        """
        query = {"password": {"$exists": True}, "password_hash": {"$exists": False}}
        docs = list(self.users.find(query, {"_id": 1, "password": 1}))
        if not docs:
            print("Nessun documento da migrare (campo 'password' mancante).")
            return 0
        count = 0
        for d in docs:
            try:
                self.users.update_one({"_id": d["_id"]}, {"$rename": {"password": "password_hash"}})
                count += 1
            except Exception as e:
                print(f"Errore migrazione user {d['_id']}: {e}")
        print(f"Migrate: rinominati {count} documenti 'password' -> 'password_hash'")
        return count