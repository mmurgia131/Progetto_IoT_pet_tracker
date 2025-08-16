from pymongo import MongoClient, ASCENDING, DESCENDING
from gridfs import GridFS
from datetime import datetime, timezone
from bson import ObjectId
import bcrypt

class PetTrackerDB:
    def __init__(self, connection_string="mongodb://localhost:27017/"):
        try:
            self.client = MongoClient(connection_string, serverSelectionTimeoutMS=5000)
            self.client.admin.command('ping')
            print("✅ Connessione a MongoDB riuscita!")

            self.db = self.client.pettracker
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

    def _setup_indexes(self):
        try:
            self.users.create_index([("username", ASCENDING)], unique=True)
            self.pets.create_index([("owner_id", ASCENDING)])
            self.positions.create_index([("pet_id", ASCENDING), ("timestamp", DESCENDING)])
            self.rooms.create_index([("owner_id", ASCENDING)])
            self.perimeters.create_index([("pet_id", ASCENDING)])
        except Exception as e:
            print(f"⚠️ Errore nella creazione degli indici: {e}")

    # --- UTENTI ---
    def create_user(self, username, password):
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        return self.users.insert_one({
            "username": username,
            "password": password_hash
        }).inserted_id

    def get_user_by_username(self, username):
        return self.users.find_one({"username": username})

    def get_user_by_id(self, user_id):
        return self.users.find_one({"_id": ObjectId(user_id)})

    def authenticate_user(self, username, password):
        user = self.get_user_by_username(username)
        if user and bcrypt.checkpw(password.encode('utf-8'), user['password']):
            return user
        return None

    # --- PETS ---
    def add_pet(self, name, owner_id, mac_address=None, bt_name=None):
        pet_data = {
            "name": name,
            "owner_id": str(owner_id)
        }
        if mac_address:
            pet_data["mac_address"] = mac_address
        if bt_name:
            pet_data["bt_name"] = bt_name
        return self.pets.insert_one(pet_data).inserted_id

    def get_pets_for_user(self, user_id):
        return list(self.pets.find({"owner_id": str(user_id)}))

    def get_pet_by_id(self, pet_id):
        return self.pets.find_one({"_id": ObjectId(pet_id)})

    def update_pet(self, pet_id, name, mac_address=None, bt_name=None):
        update_fields = {"name": name}
        if mac_address is not None:
            update_fields["mac_address"] = mac_address
        if bt_name is not None:
            update_fields["bt_name"] = bt_name
        self.pets.update_one({"_id": ObjectId(pet_id)}, {"$set": update_fields})

    def delete_pet(self, pet_id):
        self.pets.delete_one({"_id": ObjectId(pet_id)})

    # --- ROOMS ---
    def add_room(self, owner_id, name, coords=None):
        return self.rooms.insert_one({
            "owner_id": str(owner_id),
            "name": name,
            "coords": coords or []
        }).inserted_id

    def get_rooms_for_user(self, user_id):
        return list(self.rooms.find({"owner_id": str(user_id)}))

    # --- PERIMETRO ---
    def set_perimeter(self, pet_id, area_coords):
        self.perimeters.update_one(
            {"pet_id": str(pet_id)},
            {"$set": {"area": area_coords}},
            upsert=True
        )

    def get_perimeter_for_pet(self, pet_id):
        return self.perimeters.find_one({"pet_id": str(pet_id)})

    # --- ALLOWED ROOMS per pet ---
    def set_pet_allowed_rooms(self, pet_id, room_ids):
        self.pets.update_one(
            {"_id": ObjectId(pet_id)},
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

    def get_last_position(self, pet_id):
        return self.positions.find_one(
            {"pet_id": str(pet_id)},
            sort=[("timestamp", DESCENDING)]
        )

    def get_positions(self, pet_id, limit=50):
        return list(self.positions.find({"pet_id": str(pet_id)}).sort("timestamp", DESCENDING).limit(limit))

    # --- IMMAGINI (opzionale, via GridFS) ---
    def save_pet_image(self, image_data, filename, pet_id):
        image_id = self.gridfs_images.put(image_data, filename=filename, pet_id=str(pet_id), upload_date=datetime.now(timezone.utc))
        return image_id

    def get_pet_image(self, image_id):
        file = self.gridfs_images.get(ObjectId(image_id))
        return file.read(), file.filename

    # --- TOOL UTILI ---
    @staticmethod
    def is_inside_perimeter(lat, lon, area):
        try:
            from shapely.geometry import Point, Polygon
            point = Point(lon, lat)
            polygon = Polygon([(lng, lt) for lt, lng in area])
            return polygon.contains(point)
        except ImportError:
            print("Installa shapely per usare questa funzione!")
            return False