import json
import os
import uuid
from datetime import datetime
from passlib.context import CryptContext

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "users.json")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {"users": []}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_users(data: dict) -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_user_by_email(email: str) -> dict | None:
    data = load_users()
    for user in data["users"]:
        if user["email"].lower() == email.lower():
            return user
    return None


def get_user_by_id(user_id: str) -> dict | None:
    data = load_users()
    for user in data["users"]:
        if user["id"] == user_id:
            return user
    return None


def create_user(email: str, password: str, name: str = "", company: str = "") -> dict:
    data = load_users()
    user = {
        "id": str(uuid.uuid4()),
        "email": email.lower().strip(),
        "name": name.strip(),
        "company": company.strip(),
        "hashed_password": pwd_context.hash(password),
        "created_at": datetime.utcnow().isoformat(),
        "is_active": True,
    }
    data["users"].append(user)
    save_users(data)
    return user


def update_user(user_id: str, updates: dict) -> dict | None:
    data = load_users()
    for i, user in enumerate(data["users"]):
        if user["id"] == user_id:
            data["users"][i].update(updates)
            save_users(data)
            return data["users"][i]
    return None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)
