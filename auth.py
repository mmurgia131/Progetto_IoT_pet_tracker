from functools import wraps
from flask import session, request, redirect, flash
import bcrypt
from datetime import datetime
import secrets

class AuthManager:
    def __init__(self, db):
        self.db = db
        self.users_collection = db.db['users']
        self._ensure_admin_user()

    def _ensure_admin_user(self):
        """Crea utente admin di default SOLO se nessun utente esiste"""
        if self.users_collection.count_documents({}) == 0:
            default_password = "admin123"
            password_hash = bcrypt.hashpw(default_password.encode('utf-8'), bcrypt.gensalt(rounds=12))
            self.users_collection.insert_one({
                "username": "admin",
                "password_hash": password_hash,
                "created_at": datetime.now(),
                "last_login": None,
                "is_active": True
            })
            print("🔐 Default admin user created (username: admin, password: admin123)")
            print("⚠️  Cambia la password dopo il primo accesso!")
            
    def verify_password(self, username, password):
        user = self.users_collection.find_one({"username": username, "is_active": True})
        is_first_login = user and (user.get('last_login') is None)
        if user and bcrypt.checkpw(password.encode('utf-8'), user['password_hash']):
            self.users_collection.update_one(
                {"_id": user["_id"]},
                {"$set": {"last_login": datetime.now()}}
            )
            user['is_first_login'] = is_first_login  
            return user
        return None
    
    def register_user(self, username, password):
        if self.users_collection.find_one({"username": username}):
            return {"success": False, "message": "Username già esistente"}
        if len(password) < 6:
            return {"success": False, "message": "La password deve essere almeno 6 caratteri"}
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=12))
        self.users_collection.insert_one({
            "username": username,
            "password_hash": password_hash,
            "created_at": datetime.now(),
            "last_login": None,
            "is_active": True
        })
        return {"success": True, "message": "Registrazione completata"}
    
    def delete_account(self, username):
        result = self.users_collection.delete_one({"username": username})
        return result.deleted_count == 1
    
    def change_credentials(self, current_username, current_password, new_username=None, new_password=None):
        user = self.verify_password(current_username, current_password)
        if not user:
            return {"success": False, "message": "Credenziali attuali non valide"}

        updates = {}
        if new_username and new_username != current_username:
            existing = self.users_collection.find_one({"username": new_username})
            if existing:
                return {"success": False, "message": "Username già esistente"}
            updates["username"] = new_username
        if new_password:
            if len(new_password) < 6:
                return {"success": False, "message": "La password deve essere almeno 6 caratteri"}
            password_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt(rounds=12))
            updates["password_hash"] = password_hash
        if not updates:
            return {"success": False, "message": "Nessuna modifica fornita"}
        self.users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": updates}
        )
        if "username" in updates:
            session['username'] = new_username
        return {"success": True, "message": "Credenziali aggiornate con successo"}

    def get_user_info(self, username):
        user = self.users_collection.find_one(
            {"username": username, "is_active": True},
            {"password_hash": 0}
        )
        return user

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            #flash('Devi effettuare il login per accedere a questa pagina', 'warning')
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function

def generate_secret_key():
    return secrets.token_hex(32)