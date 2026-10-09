import os
import uuid
from datetime import datetime, timedelta
from typing import List, Optional
import hashlib

from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import Response, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey,
    UniqueConstraint, func, or_, inspect, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from jose import jwt, JWTError

from io import BytesIO
from PIL import Image as PILImage, ImageOps


# =========================================================
# SETTINGS FROM THE ".env" FILE (next to main.py)
# On your Mac you can leave it out. Online, the host's environment variables are used.
# =========================================================

def _load_env_file():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path) as env_file:
        for line in env_file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


# =========================================================
# DATABASE CONFIGURATION
# =========================================================

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:rahul%40@localhost:5432/room_based_image"
)
DATABASE_URL = DATABASE_URL.strip().strip("'\"")       # pasted with quotes / spaces
for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://", "postgresql+asyncpg://"):
    # Always use the psycopg2 driver (newer SQLAlchemy would otherwise look for "psycopg" v3)
    if DATABASE_URL.startswith(prefix):
        DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len(prefix):]
        break

# pool_pre_ping: reconnects by itself if the online database closed an idle connection
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =========================================================
# JWT CONFIGURATION
# =========================================================

# Online, set SECRET_KEY to a long random value (the host's environment variables or .env)
SECRET_KEY = os.getenv("SECRET_KEY", "room-based-image-secret-key-change-later")

ALGORITHM = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES = 120


# =========================================================
# TABLES
# =========================================================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    college = Column(String, nullable=True, index=True)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Admin(Base):
    """Admins are stored separately from regular users."""
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    created_by = Column(Integer, nullable=True)        # id of the admin who created the room
    created_at = Column(DateTime, default=datetime.utcnow)


class RoomUser(Base):
    """Which users have access to which room."""
    __tablename__ = "room_users"

    room_id = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    added_at = Column(DateTime, default=datetime.utcnow)


class Subject(Base):
    __tablename__ = "subjects"
    __table_args__ = (UniqueConstraint("room_id", "name", name="uq_subject_room_name"),)

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Image(Base):
    __tablename__ = "images"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True)
    subject_id = Column(Integer, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=True, index=True)
    # Storage key, e.g. "rooms/3/subjects/7/42.webp" (thumbnail: ".../42_thumb.webp").
    # Same key in Cloudflare R2 and in the local uploads folder. Older images have just "a1b2c3.webp".
    file_path = Column(String, nullable=False)
    original_name = Column(String, nullable=True)       # the file name the admin picked
    description = Column(Text, nullable=True)
    uploaded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    width = Column(Integer, nullable=True)              # size of the stored (compressed) image
    height = Column(Integer, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# Creates any tables that don't exist yet (existing tables are left untouched)
class UploadJob(Base):
    """Tracks one bulk upload (PRD: progress, created / skipped / failed, error report)."""
    __tablename__ = "upload_jobs"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(String, nullable=False, default="users")
    room_name = Column(String, nullable=True)       # room everyone was added to (bulk users)
    existing_added = Column(Integer, default=0)     # users who already existed, now added to the room
    existing_already = Column(Integer, default=0)   # users who already existed and were already in the room
    status = Column(String, nullable=False, default="running")   # running / done / failed
    total = Column(Integer, default=0)
    succeeded = Column(Integer, default=0)
    skipped = Column(Integer, default=0)     # rows that failed the checks
    failed = Column(Integer, default=0)      # rows in a batch the database refused
    batches_total = Column(Integer, default=0)
    batches_done = Column(Integer, default=0)
    error_report = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Setting(Base):
    """Simple key/value admin settings, e.g. the default password."""
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)


class Notification(Base):
    """Bell notifications shown on the admin dashboard."""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    message = Column(Text, nullable=True)
    link = Column(String, nullable=True)            # page to open when clicked, e.g. "room.html?id=3"
    read = Column(Integer, default=0, nullable=False)   # 0 = unread, 1 = read
    read_at = Column(DateTime, nullable=True)           # when it was seen (it disappears 15 min later)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


def notify(db: Session, title: str, message: str = None, link: str = None):
    """Adds a bell notification. The route's own db.commit() saves it."""
    db.add(Notification(title=title, message=message, link=link))

Base.metadata.create_all(bind=engine)


def notify_users(db: Session, count: int, action: str = "created"):
    """'User was created.' / 'Users were created.' (same for 'deleted').
    If an unseen notification of the same kind is still waiting, it is merged into one 'Users were ...'"""
    if count < 1:
        return
    kind = action.capitalize()                       # "Created" / "Deleted"
    titles = [f"User {kind}", f"Users {kind}"]
    waiting = (db.query(Notification)
                 .filter(Notification.read == 0, Notification.title.in_(titles))
                 .order_by(Notification.created_at.desc())
                 .first())
    if waiting:
        waiting.title = f"Users {kind}"
        waiting.message = f"Users were {action}."
        waiting.created_at = datetime.utcnow()
    elif count == 1:
        notify(db, f"User {kind}", f"User was {action}.", "users.html")
    else:
        notify(db, f"Users {kind}", f"Users were {action}.", "users.html")


# The images table may already exist from before, without the new columns.
# Add any missing columns so old databases keep working.
def add_missing_image_columns():
    existing = {col["name"] for col in inspect(engine).get_columns("images")}
    needed = {"original_name": "VARCHAR", "description": "TEXT",
              "width": "INTEGER", "height": "INTEGER", "size_bytes": "INTEGER"}
    with engine.begin() as conn:
        for name, sql_type in needed.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE images ADD COLUMN {name} {sql_type}"))
    if "created_by" not in {col["name"] for col in inspect(engine).get_columns("rooms")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE rooms ADD COLUMN created_by INTEGER"))


add_missing_image_columns()


# The PRD has no room code, so the old "code" column is removed from rooms
# (otherwise PostgreSQL would refuse new rooms, because the column was NOT NULL)
def drop_room_code_column():
    existing = {col["name"] for col in inspect(engine).get_columns("rooms")}
    if "code" in existing:
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_rooms_code"))
            conn.execute(text("ALTER TABLE rooms DROP COLUMN code"))


drop_room_code_column()


# Users get a college (PRD: admin searches users by email or college)
def add_college_column():
    existing = {col["name"] for col in inspect(engine).get_columns("users")}
    if "college" not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN college VARCHAR"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_college ON users (college)"))


add_college_column()


def add_job_room_column():
    existing = {col["name"] for col in inspect(engine).get_columns("upload_jobs")}
    needed = {"room_name": "VARCHAR", "existing_added": "INTEGER DEFAULT 0", "existing_already": "INTEGER DEFAULT 0"}
    with engine.begin() as conn:
        for name, sql_type in needed.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE upload_jobs ADD COLUMN {name} {sql_type}"))


add_job_room_column()


# Notifications get a "read_at" time (seen notifications disappear 15 minutes later)
def add_notification_read_at():
    existing = {col["name"] for col in inspect(engine).get_columns("notifications")}
    if "read_at" not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE notifications ADD COLUMN read_at TIMESTAMP"))


add_notification_read_at()


# Images uploaded before subjects existed belong to no subject.
# Put them in a subject called "General" in their room, so every image has a subject.
def move_loose_images_into_subjects():
    with SessionLocal() as db:
        room_ids = [r for (r,) in db.query(Image.room_id).filter(Image.subject_id.is_(None)).distinct().all()]
        for room_id in room_ids:
            subject = db.query(Subject).filter(Subject.room_id == room_id, Subject.name == "General").first()
            if not subject:
                subject = Subject(room_id=room_id, name="General")
                db.add(subject)
                db.flush()
            db.query(Image).filter(Image.room_id == room_id, Image.subject_id.is_(None)) \
                .update({Image.subject_id: subject.id}, synchronize_session=False)
        db.commit()


move_loose_images_into_subjects()


# =========================================================
# IMAGE STORAGE
# =========================================================

# Uploaded images are saved in an "uploads" folder next to main.py
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


# =========================================================
# IMAGE STORAGE: Cloudflare R2 (PRD) or the local uploads folder
# ---------------------------------------------------------
# Put the four R2 values in a file called ".env" next to main.py:
#     R2_ACCOUNT_ID=...
#     R2_ACCESS_KEY_ID=...
#     R2_SECRET_ACCESS_KEY=...
#     R2_BUCKET=...
# With all four set (and "pip3 install boto3"), images go to the private R2 bucket.
# Without them, images are kept in the local "uploads" folder (fine for testing).
# =========================================================



class LocalStorage:
    name = "local uploads folder"

    def _path(self, key: str) -> str:
        path = os.path.normpath(os.path.join(UPLOAD_DIR, key))
        if not path.startswith(UPLOAD_DIR):              # keys are made by the server, but be safe
            raise ValueError("Bad storage key")
        return path

    def put(self, key: str, data: bytes):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as out:
            out.write(data)

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def delete(self, key: str):
        path = self._path(key)
        if os.path.isfile(path):
            os.remove(path)


class R2Storage:
    """Private Cloudflare R2 bucket (S3-compatible). Images are never public:
    after the signed /media link is checked, the browser is sent to a short-lived R2 link."""

    def __init__(self, account_id, access_key, secret_key, bucket, endpoint=None, region=None, serve=None):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise RuntimeError("Cloudflare R2 needs boto3: run  pip3 install boto3")
        self.bucket = bucket
        self.name = f"Cloudflare R2 bucket '{bucket}'" if not endpoint else f"S3 storage bucket '{bucket}' ({endpoint})"
        # "redirect" = send the browser to a short-lived link (R2); "proxy" = the server sends the bytes
        self.serve = serve or ("redirect" if not endpoint or "r2.cloudflarestorage.com" in endpoint else "proxy")
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint or f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region or "auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 3},
                          s3={"addressing_style": "path"}),
        )

    def put(self, key: str, data: bytes):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="image/webp")

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, key: str):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass

    def read(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def temporary_url(self, key: str, seconds: int) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key,
                    "ResponseContentType": "image/webp", "ResponseContentDisposition": "inline",
                    "ResponseCacheControl": "private, max-age=600"},
            ExpiresIn=seconds,
        )


def _make_storage():
    values = [os.getenv(k, "").strip() for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")]
    if all(values):
        return R2Storage(*values, endpoint=os.getenv("R2_ENDPOINT_URL") or None,
                         region=os.getenv("R2_REGION") or None, serve=os.getenv("R2_SERVE") or None)
    return LocalStorage()


storage = _make_storage()
print(f"Image storage: {storage.name}")


def image_key(room_id: int, subject_id: int, image_id: int) -> str:
    """PRD key pattern: rooms/{roomId}/subjects/{subjectId}/{imageId}.webp"""
    return f"rooms/{room_id}/subjects/{subject_id}/{image_id}.webp"

# PRD 4.4: JPG, JPEG, PNG, WEBP (and HEIC from iPhones, converted on upload). Anything else is rejected.
# The check is done on the real file contents, not just the name, so a renamed PDF can't get through.
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
try:
    from pillow_heif import register_heif_opener    # pip3 install pillow-heif
    register_heif_opener()
    ALLOWED_IMAGE_FORMATS.add("HEIF")
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

MAX_IMAGE_MB = 20                                   # PRD: 20 MB per original file (to confirm with the client)
MAX_IMAGE_SIZE = MAX_IMAGE_MB * 1024 * 1024
SUPPORTED_TYPES_TEXT = "JPG, PNG or WEBP" + (" (or HEIC from an iPhone)" if HEIC_SUPPORTED else "")


def check_image_upload(filename: str, data: bytes):
    """Rejects empty, too-big and non-image files with a clear message (nothing is stored)."""
    if len(data) == 0:
        raise HTTPException(status_code=400, detail=f"'{filename}' is empty")
    if len(data) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail=f"'{filename}' is larger than {MAX_IMAGE_MB} MB")


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(title="Room Based Image Viewing Portal API")

# PRD: no public image URLs. The uploads folder is NOT served directly any more;
# every image (admin and user) goes through /media with a signed link that expires.


# =========================================================
# CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# JWT AUTHENTICATION
# =========================================================

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


# =========================================================
# REQUEST MODELS
# =========================================================

class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    college: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AdminSignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str


class RoomRequest(BaseModel):
    name: str
    user_ids: Optional[List[int]] = None     # None = leave the members as they are


class ImageUpdateRequest(BaseModel):
    description: str = ""


class SubjectRequest(BaseModel):
    title: str


class ImageIdsRequest(BaseModel):
    ids: List[int]


class ImageCountRequest(BaseModel):
    count: int


# =========================================================
# PASSWORD HASHING
# =========================================================

# PRD: passwords are stored hashed with bcrypt (never plain text).
# Accounts made before this change have an old sha256 hash; they still log in,
# and their hash is upgraded to bcrypt the first time they do.
import bcrypt
from concurrent.futures import ThreadPoolExecutor

BCRYPT_ROUNDS = 12          # single accounts and logins
BULK_BCRYPT_ROUNDS = 10     # bulk upload: still strong, ~4x faster, so 5,000 own passwords fit the 2-minute target


def _pw_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:72]          # bcrypt only uses the first 72 bytes


def hash_password(password: str, rounds: int = BCRYPT_ROUNDS) -> str:
    return bcrypt.hashpw(_pw_bytes(password), bcrypt.gensalt(rounds)).decode()


def hash_many(passwords: List[str]) -> List[str]:
    """Hashes many passwords at once (bulk upload). The same password is hashed only once,
    and the rest run in parallel, so 5,000 users stay well under the PRD's 2 minutes."""
    unique = list(dict.fromkeys(passwords))
    with ThreadPoolExecutor(max_workers=8) as pool:
        hashed = dict(zip(unique, pool.map(lambda p: hash_password(p, BULK_BCRYPT_ROUNDS), unique)))
    return [hashed[p] for p in passwords]


def is_old_hash(stored: str) -> bool:
    return not stored.startswith("$2")


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if is_old_hash(stored):                          # old sha256 hash
        return hashlib.sha256(password.encode()).hexdigest() == stored
    try:
        return bcrypt.checkpw(_pw_bytes(password), stored.encode())
    except ValueError:
        return False


# ---------- Login limit (PRD: 5 tries / 15 min) ----------
import threading as _threading
import time as _time
from fastapi import Request

MAX_FAILED_LOGINS = 5            # wrong tries allowed for one email ...
LOGIN_WINDOW_SECONDS = 15 * 60   # ... in 15 minutes
MAX_FAILED_PER_IP = 50           # one network trying many different emails (a college Wi-Fi shares one IP)

_failed_logins = {}              # key -> list of times of wrong tries
_failed_lock = _threading.Lock()


def _recent(key: str) -> list:
    now = _time.time()
    times = [t for t in _failed_logins.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    if times:
        _failed_logins[key] = times
    else:
        _failed_logins.pop(key, None)
    return times


def check_login_allowed(role: str, email: str, ip: str):
    """Stops the login with 429 when there were too many wrong tries in the last 15 minutes."""
    with _failed_lock:
        by_email = _recent(f"{role}:{email.lower()}")
        by_ip = _recent(f"ip:{ip}")
        blocked = by_email if len(by_email) >= MAX_FAILED_LOGINS else (by_ip if len(by_ip) >= MAX_FAILED_PER_IP else None)
        if blocked:
            wait = int(LOGIN_WINDOW_SECONDS - (_time.time() - blocked[0])) + 1
            minutes = max(1, (wait + 59) // 60)
            raise HTTPException(
                status_code=429,
                detail=f"Too many wrong attempts. Please try again in {minutes} minute{'s' if minutes != 1 else ''}.",
            )


def record_failed_login(role: str, email: str, ip: str):
    now = _time.time()
    with _failed_lock:
        _failed_logins.setdefault(f"{role}:{email.lower()}", []).append(now)
        _failed_logins.setdefault(f"ip:{ip}", []).append(now)


def clear_failed_logins(role: str, email: str):
    with _failed_lock:
        _failed_logins.pop(f"{role}:{email.lower()}", None)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# Used when the email doesn't exist, so a wrong email takes as long as a wrong password
_DUMMY_HASH = hash_password("not-a-real-password")


# =========================================================
# CREATE JWT TOKEN
# =========================================================

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# =========================================================
# VERIFY JWT TOKEN
# =========================================================

def read_token(token: str):
    """Returns (account_id, role) from a login token, or raises 401."""
    credentials_exception = HTTPException(
        status_code=401,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"}
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        account_id = payload.get("sub")
        if account_id is None:
            raise credentials_exception
        return int(account_id), payload.get("role", "user")

    except (JWTError, ValueError):
        raise credentials_exception


def get_current_user(token: str = Depends(oauth2_scheme)):
    """For regular users only."""
    user_id, role = read_token(token)
    if role != "user":
        raise HTTPException(status_code=403, detail="This page is for users only")
    return user_id


def get_current_admin(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """For admins only. A regular user's login is rejected here,
    and so is the login of an admin who has been removed."""
    admin_id, role = read_token(token)
    if role != "admin" or not db.query(Admin.id).filter(Admin.id == admin_id).first():
        raise HTTPException(
            status_code=401,
            detail="Admin login required",
            headers={"WWW-Authenticate": "Bearer"}
        )
    return admin_id


# =========================================================
# HOME
# =========================================================

@app.get("/api/health")
def health():
    return {"message": "Room Based Image Viewing Portal API is running"}


# =========================================================
# SIGNUP (regular users)
# =========================================================

@app.post("/signup")
def signup(user: SignupRequest):
    # PRD: no self sign-up. Users are created only by the admin.
    raise HTTPException(status_code=403, detail="Sign-up is closed. Your admin creates your account.")
    db = SessionLocal()
    try:
        existing_user = db.query(User).filter(User.email == user.email).first()

        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")

        new_user = User(
            name=user.name,
            email=user.email,
            college=(user.college or "").strip() or None,
            password_hash=hash_password(user.password)
        )

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        return {"message": "Account created successfully", "user_id": new_user.id}

    finally:
        db.close()


# =========================================================
# LOGIN (regular users)
# =========================================================

@app.post("/login")
def login(user: LoginRequest, request: Request):
    ip = client_ip(request)
    check_login_allowed("user", user.email, ip)
    db = SessionLocal()
    try:
        existing_user = db.query(User).filter(func.lower(User.email) == user.email.lower()).first()

        if not existing_user or not verify_password(user.password, existing_user.password_hash):
            if not existing_user:
                verify_password(user.password, _DUMMY_HASH)
            record_failed_login("user", user.email, ip)
            raise HTTPException(status_code=401, detail="Invalid email or password")
        clear_failed_logins("user", user.email)
        if is_old_hash(existing_user.password_hash):     # upgrade old sha256 hash to bcrypt
            existing_user.password_hash = hash_password(user.password)
            db.commit()

        access_token = create_access_token({"sub": str(existing_user.id), "role": "user"})

        return {
            "message": "Login successful",
            "access_token": access_token,
            "token_type": "bearer",
            "user_id": existing_user.id,
            "name": existing_user.name,
            "email": existing_user.email
        }

    finally:
        db.close()


# =========================================================
# ADMIN SIGNUP (saves to the admins table)
# =========================================================

@app.post("/admin/signup")
def admin_signup(data: AdminSignupRequest, db: Session = Depends(get_db)):

    name = data.name.strip()
    email = data.email.strip().lower()
    if not name or not data.password:
        raise HTTPException(status_code=400, detail="Name and password are required")

    if len(data.password) < 6:
        raise HTTPException(
        status_code=400,
        detail="Password must be at least 6 characters long"
    )
    
    if db.query(Admin).filter(func.lower(Admin.email) == email).first():
        raise HTTPException(status_code=400, detail="Admin email already registered")

    # Online safety: once an admin exists, nobody else can make an admin account from the
    # sign-up page (set ALLOW_ADMIN_SIGNUP=true to allow more admins for a while)
    allow_signup = os.getenv("ALLOW_ADMIN_SIGNUP", "").strip().lower() == "true"
    if db.query(Admin.id).first() and not allow_signup:
        raise HTTPException(
            status_code=403,
            detail="Admin sign-up is closed. Ask an existing admin to add you from Settings."
        )

    admin = Admin(name=name, email=email, password_hash=hash_password(data.password))
    db.add(admin)
    db.commit()
    db.refresh(admin)

    return {"message": "Admin account created successfully", "admin_id": admin.id}


# =========================================================
# ADMIN LOGIN (checks the admins table, not users)
# =========================================================

@app.post("/admin/login")
def admin_login(data: LoginRequest, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    check_login_allowed("admin", data.email, ip)
    admin = db.query(Admin).filter(func.lower(Admin.email) == data.email.lower()).first()

    if not admin or not verify_password(data.password, admin.password_hash):
        if not admin:
            verify_password(data.password, _DUMMY_HASH)
        record_failed_login("admin", data.email, ip)
        raise HTTPException(status_code=401, detail="Invalid admin email or password")
    clear_failed_logins("admin", data.email)
    if is_old_hash(admin.password_hash):                 # upgrade old sha256 hash to bcrypt
        admin.password_hash = hash_password(data.password)
        db.commit()

    access_token = create_access_token({"sub": str(admin.id), "role": "admin"})

    return {
        "message": "Login successful",
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": admin.id,
        "name": admin.name,
        "email": admin.email,
        "role": "admin"
    }


@app.get("/admin/me")
def get_admin_profile(
    admin_id: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    admin = db.query(Admin).filter(Admin.id == admin_id).first()

    if not admin:
        raise HTTPException(status_code=401, detail="Admin not found")

    return {"id": admin.id, "name": admin.name, "email": admin.email, "role": "admin"}


# =========================================================
# CURRENT USER (regular users)
# =========================================================

@app.get("/me")
def get_my_profile(
    user_id: int = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {"id": user.id, "name": user.name, "email": user.email}


# =========================================================
# ADMIN DASHBOARD STATS
# =========================================================

@app.get("/admin/dashboard")
def get_dashboard(
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    def total(model):
        return db.query(func.count(model.id)).scalar() or 0

    return {
        "total_rooms": total(Room),
        "total_users": total(User),
        "total_subjects": total(Subject),
        "total_images": total(Image),
    }

# =========================================================
# NOTIFICATIONS (the bell on the dashboard)
# =========================================================

NOTIFICATION_KEEP_MINUTES = 15     # a seen notification disappears this long after it was seen


class NotificationIdsRequest(BaseModel):
    ids: List[int] = []


@app.get("/admin/notifications")
def list_notifications(
    limit: int = 50,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    limit = max(1, min(limit, 100))

    # Seen notifications disappear NOTIFICATION_KEEP_MINUTES after they were seen
    now = datetime.utcnow()
    db.query(Notification).filter(Notification.read == 1, Notification.read_at.is_(None)) \
        .update({Notification.read_at: now}, synchronize_session=False)      # seen before this change
    db.query(Notification).filter(
        Notification.read == 1,
        Notification.read_at < now - timedelta(minutes=NOTIFICATION_KEEP_MINUTES)
    ).delete(synchronize_session=False)
    db.commit()

    rows = (
        db.query(Notification)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": n.id,
            "title": n.title,
            "message": n.message or "",
            "link": n.link or "",
            "read": bool(n.read),
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in rows
    ]


@app.post("/admin/notifications/mark-read")
def mark_notifications_read(
    data: NotificationIdsRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    ids = list(set(data.ids))
    if ids:
        db.query(Notification).filter(Notification.id.in_(ids), Notification.read == 0) \
            .update({Notification.read: 1, Notification.read_at: datetime.utcnow()}, synchronize_session=False)
        db.commit()
    return {"marked": len(ids)}


@app.delete("/admin/notifications/{notification_id}")
def delete_notification(
    notification_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """The x on one notification: removes it."""
    db.query(Notification).filter(Notification.id == notification_id).delete(synchronize_session=False)
    db.commit()
    return {"deleted": 1}


@app.delete("/admin/notifications")
def clear_notifications(
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Clear all: removes every notification."""
    deleted = db.query(Notification).delete(synchronize_session=False)
    db.commit()
    return {"deleted": deleted}


# =========================================================
# HELPERS FOR ROOMS
# =========================================================

def clean_room_input(data: RoomRequest):
    name = data.name.strip()

    if not name:
        raise HTTPException(status_code=400, detail="Room name is required")

    user_ids = None if data.user_ids is None else sorted(set(data.user_ids))
    return name, user_ids


def check_user_ids(db: Session, user_ids: List[int]):
    if not user_ids:
        return
    found = {uid for (uid,) in db.query(User.id).filter(User.id.in_(user_ids)).all()}
    missing = [uid for uid in user_ids if uid not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"Users not found: {missing}")


# ---------- Image compression ----------
FULL_MAX_SIDE = 2560     # longest side of the stored image (smaller images are not enlarged)
FULL_QUALITY = 80        # WebP quality for the full image
THUMB_MAX_SIDE = 400     # longest side of the thumbnail
THUMB_QUALITY = 70


def thumb_name_for(file_path: str) -> str:
    """'rooms/1/subjects/2/42.webp' -> 'rooms/1/subjects/2/42_thumb.webp' (old 'abc.webp' -> 'abc_thumb.webp')"""
    base, _ = os.path.splitext(file_path or "")
    return f"{base}_thumb.webp"


def compress_image(data: bytes, filename: str):
    """Turns an uploaded image into (full_webp_bytes, thumb_webp_bytes).
    Fixes the rotation from the camera, then drops EXIF data (GPS, camera info)."""
    not_supported = HTTPException(
        status_code=400,
        detail=f"'{filename}' is not a supported image. Use {SUPPORTED_TYPES_TEXT}.",
    )
    try:
        picture = PILImage.open(BytesIO(data))
        real_format = picture.format                 # read from the file itself (JPEG, PNG, GIF, ...)
    except Exception:
        raise not_supported                          # PDF, video, document, broken file ...
    if real_format not in ALLOWED_IMAGE_FORMATS:
        raise not_supported                          # e.g. GIF, BMP, TIFF
    try:
        picture = ImageOps.exif_transpose(picture)   # keep phone photos the right way up
        picture.load()
    except Exception:
        raise not_supported

    # WebP needs RGB, or RGBA when the image has transparency
    has_alpha = picture.mode in ("RGBA", "LA") or (picture.mode == "P" and "transparency" in picture.info)
    picture = picture.convert("RGBA" if has_alpha else "RGB")

    full = picture.copy()
    full.thumbnail((FULL_MAX_SIDE, FULL_MAX_SIDE), PILImage.LANCZOS)
    full_out = BytesIO()
    full.save(full_out, "WEBP", quality=FULL_QUALITY, method=4)   # no exif= so EXIF is removed

    thumb = picture.copy()
    thumb.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE), PILImage.LANCZOS)
    thumb_out = BytesIO()
    thumb.save(thumb_out, "WEBP", quality=THUMB_QUALITY, method=4)

    return full_out.getvalue(), thumb_out.getvalue()


def image_size_of(data: bytes):
    with PILImage.open(BytesIO(data)) as picture:
        return picture.size


def add_image(db: Session, room_id: int, subject_id: int, filename: str,
              full: bytes, thumb: bytes, description: str = "") -> "Image":
    """PRD: compress -> write to storage (R2) -> then the Image row.
    The row gets its id first (not committed), the files are written with that id in the key,
    and if writing fails the row is rolled back, so no row exists without its files.
    The caller commits."""
    width, height = image_size_of(full)
    image = Image(
        room_id=room_id,
        subject_id=subject_id,
        file_path="",
        original_name=filename,
        description=(description or "").strip() or None,
        width=width,
        height=height,
        size_bytes=len(full),
    )
    db.add(image)
    db.flush()                                   # gives image.id
    key = image_key(room_id, subject_id, image.id)
    try:
        storage.put(key, full)
        storage.put(thumb_name_for(key), thumb)
    except Exception as exc:
        print("Image storage write failed:", exc)
        storage.delete(key)
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Couldn't store '{filename}'. Please try again.")
    image.file_path = key
    return image


def image_to_dict(image: Image):
    """Admin view of an image. Links are signed for the admin (u=0) and expire."""
    links = signed_links(image, ADMIN_MEDIA_USER, ADMIN_LINK_LIFETIME_SECONDS)
    return {
        "id": image.id,
        "room_id": image.room_id,
        "subject_id": image.subject_id,
        "url": links["url"],
        "thumb_url": links["thumb_url"],
        "original_name": image.original_name,
        "description": image.description or "",
        "width": image.width,
        "height": image.height,
        "size_bytes": image.size_bytes,
        "created_at": image.created_at.isoformat() if image.created_at else None,
    }


def delete_image_file(file_path: str):
    """Deletes an image and its thumbnail from storage (R2 or the uploads folder)."""
    if not file_path:
        return
    for key in (file_path, thumb_name_for(file_path)):
        try:
            storage.delete(key)
        except Exception as exc:
            print("Image storage delete failed:", key, exc)


def room_to_dict(db: Session, room: Room):
    users = (
        db.query(User.id, User.name, User.email, User.college)
        .join(RoomUser, RoomUser.user_id == User.id)
        .filter(RoomUser.room_id == room.id)
        .order_by(User.name)
        .all()
    )
    subjects = db.query(Subject).filter(Subject.room_id == room.id).order_by(Subject.created_at, Subject.id).all()
    images = (
        db.query(Image)
        .filter(Image.room_id == room.id)
        .order_by(Image.created_at, Image.id)
        .all()
    )

    return {
        "id": room.id,
        "name": room.name,
        "created_at": room.created_at.isoformat() if room.created_at else None,
        "user_count": len(users),
        "subject_count": len(subjects),
        "image_count": len(images),
        "users": [{"id": u.id, "name": u.name, "email": u.email, "college": u.college} for u in users],
        "subjects": [
            {
                "id": sub.id,
                "title": sub.name,
                "image_count": sum(1 for img in images if img.subject_id == sub.id),
            }
            for sub in subjects
        ],
        "images": [image_to_dict(img) for img in images],
    }


# =========================================================
# LIST ROOMS
# =========================================================

@app.get("/admin/rooms")
def list_rooms(
    search: Optional[str] = None,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    user_counts = (
        db.query(RoomUser.room_id, func.count(RoomUser.user_id).label("c"))
        .group_by(RoomUser.room_id).subquery()
    )
    subject_counts = (
        db.query(Subject.room_id, func.count(Subject.id).label("c"))
        .group_by(Subject.room_id).subquery()
    )
    image_counts = (
        db.query(Image.room_id, func.count(Image.id).label("c"))
        .group_by(Image.room_id).subquery()
    )

    query = (
        db.query(
            Room,
            func.coalesce(user_counts.c.c, 0),
            func.coalesce(subject_counts.c.c, 0),
            func.coalesce(image_counts.c.c, 0),
        )
        .outerjoin(user_counts, user_counts.c.room_id == Room.id)
        .outerjoin(subject_counts, subject_counts.c.room_id == Room.id)
        .outerjoin(image_counts, image_counts.c.room_id == Room.id)
    )

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(Room.name.ilike(term))

    rows = query.order_by(Room.created_at.desc(), Room.id.desc()).all()

    return [
        {
            "id": room.id,
            "name": room.name,
            "created_at": room.created_at.isoformat() if room.created_at else None,
            "user_count": users,
            "subject_count": subjects,
            "image_count": images,
        }
        for room, users, subjects, images in rows
    ]


# =========================================================
# GET ONE ROOM
# =========================================================

@app.get("/admin/rooms/{room_id}")
def get_room(
    room_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room_to_dict(db, room)


# =========================================================
# CREATE ROOM
# =========================================================

@app.post("/admin/rooms", status_code=201)
def create_room(
    data: RoomRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    name, user_ids = clean_room_input(data)

    check_user_ids(db, user_ids)

    room = Room(name=name, created_by=current_admin)
    db.add(room)
    db.flush()  # gets room.id

    for uid in user_ids or []:
        db.add(RoomUser(room_id=room.id, user_id=uid))

    notify(db, "Room Created", f'Room "{name}" was created.', f"room.html?id={room.id}")
    db.commit()
    db.refresh(room)

    return room_to_dict(db, room)


# =========================================================
# UPDATE ROOM
# =========================================================

@app.put("/admin/rooms/{room_id}")
def update_room(
    room_id: int,
    data: RoomRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    name, user_ids = clean_room_input(data)
    check_user_ids(db, user_ids)

    room.name = name

    # Replace the room's user list only when one is sent
    # (renaming sends just the name; members are managed on the room page)
    if user_ids is not None:
        db.query(RoomUser).filter(RoomUser.room_id == room_id).delete(synchronize_session=False)
        for uid in user_ids:
            db.add(RoomUser(room_id=room_id, user_id=uid))

    db.commit()
    db.refresh(room)

    return room_to_dict(db, room)


# =========================================================
# DELETE ROOM
# =========================================================

@app.delete("/admin/rooms/{room_id}")
def delete_room(
    room_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    # Remove everything that belongs to the room, including image files on disk
    for (file_path,) in db.query(Image.file_path).filter(Image.room_id == room_id).all():
        delete_image_file(file_path)
    db.query(Image).filter(Image.room_id == room_id).delete(synchronize_session=False)
    db.query(Subject).filter(Subject.room_id == room_id).delete(synchronize_session=False)
    db.query(RoomUser).filter(RoomUser.room_id == room_id).delete(synchronize_session=False)
    db.delete(room)
    notify(db, "Room Deleted", f'Room "{room.name}" was deleted.')
    db.commit()

    return {"message": f"Room '{room.name}' deleted"}


# =========================================================
# LIST USERS (for the "Add Users" picker) - regular users only
# =========================================================

@app.get("/admin/users")
def list_users(
    search: Optional[str] = None,
    limit: int = 20,
    exclude_room: Optional[int] = None,       # leave out users already in this room
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    limit = max(1, min(limit, 100))
    query = db.query(User)
    if exclude_room:
        in_room = db.query(RoomUser.user_id).filter(RoomUser.room_id == exclude_room)
        query = query.filter(~User.id.in_(in_room))

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(or_(
            User.name.ilike(term),
            User.email.ilike(term),
            User.college.ilike(term),
        ))

    total = query.count()
    users = query.order_by(User.name).limit(limit).all()

    return {
        "total": total,
        "users": [{"id": u.id, "name": u.name, "email": u.email, "college": u.college} for u in users],
    }


# =========================================================
# ROOM IMAGES
# =========================================================

@app.get("/admin/rooms/{room_id}/images")
def list_room_images(
    room_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if not db.query(Room.id).filter(Room.id == room_id).first():
        raise HTTPException(status_code=404, detail="Room not found")

    images = (
        db.query(Image)
        .filter(Image.room_id == room_id)
        .order_by(Image.created_at, Image.id)
        .all()
    )
    return [image_to_dict(img) for img in images]


@app.post("/admin/rooms/{room_id}/images", status_code=201)
async def upload_room_image(
    room_id: int,
    file: UploadFile = File(...),
    description: str = Form(""),
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if not db.query(Room.id).filter(Room.id == room_id).first():
        raise HTTPException(status_code=404, detail="Room not found")

    data = await file.read()
    check_image_upload(file.filename, data)

    full, thumb = compress_image(data, file.filename)
    image = add_image(db, room_id, get_general_subject(db, room_id).id, file.filename, full, thumb, description)
    try:
        db.commit()
    except Exception:
        db.rollback()
        delete_image_file(image.file_path)
        raise HTTPException(status_code=500, detail="Couldn't save the image. Please try again.")
    db.refresh(image)

    return image_to_dict(image)


MAX_BULK_IMAGES = 200  # PRD: one bulk session of up to 200 images (to confirm)


@app.post("/admin/rooms/{room_id}/images/bulk", status_code=201)
async def upload_room_images_bulk(
    room_id: int,
    files: List[UploadFile] = File(...),
    descriptions: List[str] = Form([]),
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if not db.query(Room.id).filter(Room.id == room_id).first():
        raise HTTPException(status_code=404, detail="Room not found")

    if len(files) > MAX_BULK_IMAGES:
        raise HTTPException(status_code=400, detail=f"You can upload up to {MAX_BULK_IMAGES} images at once")

    # Check every file first, so nothing is saved if one of them is bad
    checked = []
    for file in files:
        data = await file.read()
        check_image_upload(file.filename, data)
        full, thumb = compress_image(data, file.filename)
        checked.append((file.filename, full, thumb))

    images = []
    general_id = get_general_subject(db, room_id).id
    try:
        for i, (filename, full, thumb) in enumerate(checked):
            description = descriptions[i] if i < len(descriptions) else ""
            images.append(add_image(db, room_id, general_id, filename, full, thumb, description))
        db.commit()
    except Exception:
        # Undo everything if saving failed halfway
        db.rollback()
        for image in images:
            delete_image_file(image.file_path)
        raise HTTPException(status_code=500, detail="Couldn't save the images. Please try again.")

    for image in images:
        db.refresh(image)

    return {"uploaded": len(images), "images": [image_to_dict(img) for img in images]}


@app.put("/admin/images/{image_id}")
def update_image(
    image_id: int,
    data: ImageUpdateRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    image = db.query(Image).filter(Image.id == image_id).first()
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    image.description = data.description.strip() or None
    db.commit()
    db.refresh(image)

    return image_to_dict(image)


@app.delete("/admin/images/{image_id}")
def delete_image(
    image_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    image = db.query(Image).filter(Image.id == image_id).first()
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    delete_image_file(image.file_path)
    db.delete(image)
    db.commit()

    return {"message": "Image deleted"}

# =========================================================
# USER SIDE (view-only)
# Users see only the rooms they were added to. Images are sent
# through short-lived signed links, so a copied link stops working.
# =========================================================

import hmac
import time
from fastapi.responses import FileResponse

LINK_LIFETIME_SECONDS = 10 * 60      # signed image links work for 10 minutes
ADMIN_LINK_LIFETIME_SECONDS = 30 * 60  # admin pages stay open longer while uploading / editing
ADMIN_MEDIA_USER = 0                  # "u=0" in a signed link = the admin (real user ids start at 1)
USER_PAGE_SIZE = 30                  # images per page in the grid


def require_room_member(db: Session, user_id: int, room_id: int):
    """403 unless this user was added to the room (also 403 if the room doesn't exist)."""
    member = (
        db.query(RoomUser)
        .filter(RoomUser.room_id == room_id, RoomUser.user_id == user_id)
        .first()
    )
    if not member:
        raise HTTPException(status_code=403, detail="You don't have access to this room")


def sign_media(image_id: int, kind: str, user_id: int, expires: int) -> str:
    message = f"{image_id}:{kind}:{user_id}:{expires}".encode()
    return hmac.new(SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def signed_links(image: Image, user_id: int, lifetime: int = None) -> dict:
    """Fresh signed links for the full image and its thumbnail."""
    expires = int(time.time()) + (lifetime or LINK_LIFETIME_SECONDS)

    def link(kind):
        sig = sign_media(image.id, kind, user_id, expires)
        return f"/media/{image.id}/{kind}?u={user_id}&exp={expires}&sig={sig}"

    return {"url": link("full"), "thumb_url": link("thumb"), "expires": expires}


def user_image_to_dict(image: Image, user_id: int) -> dict:
    return {
        "id": image.id,
        "name": image.original_name or "",
        "description": image.description or "",
        **signed_links(image, user_id),
    }


@app.get("/me/rooms")
def my_rooms(
    user_id: int = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    rooms = (
        db.query(Room)
        .join(RoomUser, RoomUser.room_id == Room.id)
        .filter(RoomUser.user_id == user_id)
        .order_by(Room.name)
        .all()
    )
    result = []
    for room in rooms:
        count = db.query(func.count(Image.id)).filter(Image.room_id == room.id).scalar() or 0
        cover = (
            db.query(Image)
            .filter(Image.room_id == room.id)
            .order_by(Image.created_at, Image.id)
            .first()
        )
        subject_count = db.query(func.count(Subject.id)).filter(Subject.room_id == room.id).scalar() or 0
        result.append({
            "id": room.id,
            "name": room.name,
            "subject_count": subject_count,
            "image_count": count,
            "cover_url": signed_links(cover, user_id)["thumb_url"] if cover else None,
        })
    return result


@app.get("/rooms/{room_id}")
def user_room(
    room_id: int,
    user_id: int = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    require_room_member(db, user_id, room_id)
    room = db.query(Room).filter(Room.id == room_id).first()
    count = db.query(func.count(Image.id)).filter(Image.room_id == room_id).scalar() or 0
    subject_count = db.query(func.count(Subject.id)).filter(Subject.room_id == room_id).scalar() or 0
    return {"id": room.id, "name": room.name, "subject_count": subject_count, "image_count": count}


@app.get("/rooms/{room_id}/images")
def user_room_images(
    room_id: int,
    offset: int = 0,
    limit: int = USER_PAGE_SIZE,
    user_id: int = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    require_room_member(db, user_id, room_id)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    query = db.query(Image).filter(Image.room_id == room_id)
    total = query.count()
    images = query.order_by(Image.created_at, Image.id).offset(offset).limit(limit).all()

    return {
        "total": total,
        "offset": offset,
        "images": [user_image_to_dict(img, user_id) for img in images],
    }


@app.get("/images/{image_id}/link")
def user_image_link(
    image_id: int,
    user_id: int = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """New signed links for one image (used when the old ones are about to expire)."""
    image = db.query(Image).filter(Image.id == image_id).first()
    if not image:
        raise HTTPException(status_code=403, detail="You don't have access to this image")
    require_room_member(db, user_id, image.room_id)
    return signed_links(image, user_id)


@app.get("/media/{image_id}/{kind}")
def serve_media(
    image_id: int,
    kind: str,
    u: int,
    exp: int,
    sig: str,
    db: Session = Depends(get_db)
):
    """Sends an image only if the signed link is valid, not expired,
    and (for users) the user is still a member of the image's room.
    Every failure gives the same plain "Not found", so a bad link doesn't tell
    anyone WHY it failed (expired, wrong signature, no access, or no such image)."""
    not_found = HTTPException(status_code=404, detail="Not found")

    if kind not in ("full", "thumb"):
        raise not_found
    if exp < time.time():
        raise not_found
    if not hmac.compare_digest(sig, sign_media(image_id, kind, u, exp)):
        raise not_found

    image = db.query(Image).filter(Image.id == image_id).first()
    if not image:
        raise not_found
    if u != ADMIN_MEDIA_USER:            # admin links (u=0) can only be made by the server, for admins
        is_member = (
            db.query(RoomUser.user_id)
            .filter(RoomUser.room_id == image.room_id, RoomUser.user_id == u)
            .first()
        )
        if not is_member:
            raise not_found

    key = image.file_path
    if kind == "thumb" and storage.exists(thumb_name_for(key)):
        key = thumb_name_for(key)         # older images have no thumbnail, so they use the full image

    if isinstance(storage, R2Storage):
        if storage.serve == "proxy":
            # The server reads the image from the private bucket and sends it
            try:
                body = storage.read(key)
            except Exception:
                raise not_found
            return Response(body, media_type="image/webp", headers={
                "Cache-Control": "private, max-age=600",
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
            })
        # Private bucket: send the browser to a short-lived R2 link (at most 10 minutes)
        seconds = max(60, min(LINK_LIFETIME_SECONDS, exp - int(time.time())))
        return RedirectResponse(storage.temporary_url(key, seconds), status_code=302,
                                headers={"Cache-Control": "no-store"})

    path = storage._path(key)
    if not os.path.isfile(path):
        raise not_found

    return FileResponse(
        path,
        headers={
            "Cache-Control": "private, max-age=600",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


# =========================================================
# SUBJECTS (Room -> Subject -> Image)
# A subject has a title only. Images are uploaded into a subject.
# =========================================================

def get_general_subject(db: Session, room_id: int) -> Subject:
    """The "General" subject of a room (created if missing)."""
    subject = db.query(Subject).filter(Subject.room_id == room_id, Subject.name == "General").first()
    if not subject:
        subject = Subject(room_id=room_id, name="General")
        db.add(subject)
        db.flush()
    return subject


def clean_subject_title(title: str) -> str:
    title = (title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Subject title is required")
    if len(title) > 120:
        raise HTTPException(status_code=400, detail="Subject title is too long (max 120 characters)")
    return title


def check_subject_title_free(db: Session, room_id: int, title: str, except_id: Optional[int] = None):
    query = db.query(Subject.id).filter(Subject.room_id == room_id, func.lower(Subject.name) == title.lower())
    if except_id is not None:
        query = query.filter(Subject.id != except_id)
    if query.first():
        raise HTTPException(status_code=409, detail=f"This room already has a subject called '{title}'")


def subject_to_dict(db: Session, subject: Subject) -> dict:
    count = db.query(func.count(Image.id)).filter(Image.subject_id == subject.id).scalar() or 0
    return {"id": subject.id, "room_id": subject.room_id, "title": subject.name, "image_count": count}


def get_subject_or_404(db: Session, subject_id: int) -> Subject:
    subject = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")
    return subject


@app.get("/admin/rooms/{room_id}/subjects")
def list_subjects(
    room_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if not db.query(Room.id).filter(Room.id == room_id).first():
        raise HTTPException(status_code=404, detail="Room not found")
    subjects = db.query(Subject).filter(Subject.room_id == room_id).order_by(Subject.created_at, Subject.id).all()
    return [subject_to_dict(db, sub) for sub in subjects]


@app.post("/admin/rooms/{room_id}/subjects", status_code=201)
def create_subject(
    room_id: int,
    data: SubjectRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if not db.query(Room.id).filter(Room.id == room_id).first():
        raise HTTPException(status_code=404, detail="Room not found")
    title = clean_subject_title(data.title)
    check_subject_title_free(db, room_id, title)

    subject = Subject(room_id=room_id, name=title)
    db.add(subject)
    notify(db, "Subject Created",
           f'Subject "{title}" was created.',
           f"room.html?id={room_id}#subjects")
    db.commit()
    db.refresh(subject)
    return subject_to_dict(db, subject)


@app.put("/admin/subjects/{subject_id}")
def rename_subject(
    subject_id: int,
    data: SubjectRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    subject = get_subject_or_404(db, subject_id)
    title = clean_subject_title(data.title)
    check_subject_title_free(db, subject.room_id, title, except_id=subject.id)

    subject.name = title
    db.commit()
    db.refresh(subject)
    return subject_to_dict(db, subject)


@app.delete("/admin/subjects/{subject_id}")
def delete_subject(
    subject_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Deletes the subject and all of its images (files too)."""
    subject = get_subject_or_404(db, subject_id)
    for (file_path,) in db.query(Image.file_path).filter(Image.subject_id == subject_id).all():
        delete_image_file(file_path)
    db.query(Image).filter(Image.subject_id == subject_id).delete(synchronize_session=False)
    room_id = subject.room_id
    db.delete(subject)
    notify(db, "Subject Deleted",
           f'Subject "{subject.name}" was deleted.',
           f"room.html?id={room_id}#subjects")
    db.commit()
    return {"message": f"Subject '{subject.name}' deleted"}


@app.get("/admin/subjects/{subject_id}/images")
def list_subject_images(
    subject_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    get_subject_or_404(db, subject_id)
    images = (
        db.query(Image)
        .filter(Image.subject_id == subject_id)
        .order_by(Image.created_at, Image.id)
        .all()
    )
    return [image_to_dict(img) for img in images]


@app.post("/admin/subjects/{subject_id}/images", status_code=201)
async def upload_subject_image(
    subject_id: int,
    file: UploadFile = File(...),
    description: str = Form(""),
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    subject = get_subject_or_404(db, subject_id)

    data = await file.read()
    check_image_upload(file.filename, data)

    full, thumb = compress_image(data, file.filename)
    image = add_image(db, subject.room_id, subject.id, file.filename, full, thumb, description)
    try:
        db.commit()
    except Exception:
        db.rollback()
        delete_image_file(image.file_path)   # nothing is kept if saving fails
        raise HTTPException(status_code=500, detail="Couldn't save the image. Please try again.")
    db.refresh(image)
    return image_to_dict(image)


@app.post("/admin/subjects/{subject_id}/images/uploaded")
def report_images_uploaded(
    subject_id: int,
    data: ImageCountRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """The page calls this once when a batch of uploads finishes, so the bell shows one notification."""
    subject = get_subject_or_404(db, subject_id)
    count = data.count
    if count > 0:
        word = "image" if count == 1 else "images"
        notify(db, "Images Uploaded",
               f'{count} {word} uploaded to "{subject.name}".',
               f"room.html?id={subject.room_id}#subject/{subject.id}")
        db.commit()
    return {"ok": True}


@app.delete("/admin/images")
def delete_many_images(
    data: ImageIdsRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Deletes several images at once (multi-select delete)."""
    ids = list(set(data.ids))
    if not ids:
        return {"deleted": 0}
    images = db.query(Image).filter(Image.id.in_(ids)).all()
    subject_ids = {image.subject_id for image in images}
    room_ids = {image.room_id for image in images}
    for image in images:
        delete_image_file(image.file_path)
        db.delete(image)
    if images:
        count = len(images)
        word = "image" if count == 1 else "images"
        subject = None
        if len(subject_ids) == 1:
            subject = db.query(Subject).filter(Subject.id == next(iter(subject_ids))).first()
        if subject:
            notify(db, "Images Deleted",
                   f'{count} {word} deleted from "{subject.name}".',
                   f"room.html?id={subject.room_id}#subject/{subject.id}")
        else:
            notify(db, "Images Deleted", f"{count} {word} deleted.",
                   f"room.html?id={next(iter(room_ids))}#subjects" if len(room_ids) == 1 else None)
    db.commit()
    return {"deleted": len(images)}


# ---------- Subjects for users (view-only) ----------

@app.get("/rooms/{room_id}/subjects")
def user_room_subjects(
    room_id: int,
    user_id: int = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    require_room_member(db, user_id, room_id)
    subjects = db.query(Subject).filter(Subject.room_id == room_id).order_by(Subject.created_at, Subject.id).all()
    result = []
    for sub in subjects:
        count = db.query(func.count(Image.id)).filter(Image.subject_id == sub.id).scalar() or 0
        cover = (
            db.query(Image)
            .filter(Image.subject_id == sub.id)
            .order_by(Image.created_at, Image.id)
            .first()
        )
        result.append({
            "id": sub.id,
            "title": sub.name,
            "image_count": count,
            "cover_url": signed_links(cover, user_id)["thumb_url"] if cover else None,
        })
    return result


@app.get("/subjects/{subject_id}")
def user_subject(
    subject_id: int,
    user_id: int = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    subject = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=403, detail="You don't have access to this subject")
    require_room_member(db, user_id, subject.room_id)
    room = db.query(Room).filter(Room.id == subject.room_id).first()
    count = db.query(func.count(Image.id)).filter(Image.subject_id == subject_id).scalar() or 0
    return {
        "id": subject.id,
        "title": subject.name,
        "room_id": room.id,
        "room_name": room.name,
        "image_count": count,
    }


@app.get("/subjects/{subject_id}/images")
def user_subject_images(
    subject_id: int,
    offset: int = 0,
    limit: int = USER_PAGE_SIZE,
    user_id: int = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    subject = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=403, detail="You don't have access to this subject")
    require_room_member(db, user_id, subject.room_id)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    query = db.query(Image).filter(Image.subject_id == subject_id)
    total = query.count()
    images = query.order_by(Image.created_at, Image.id).offset(offset).limit(limit).all()
    return {
        "total": total,
        "offset": offset,
        "images": [user_image_to_dict(img, user_id) for img in images],
    }


# =========================================================
# ROOM MEMBERS (Room detail page -> Members tab)
# =========================================================

class MemberIdsRequest(BaseModel):
    user_ids: List[int]


def get_room_or_404(db: Session, room_id: int) -> Room:
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


@app.post("/admin/rooms/{room_id}/members", status_code=201)
def add_room_members(
    room_id: int,
    data: MemberIdsRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Adds users to the room. Users already in the room are skipped."""
    get_room_or_404(db, room_id)
    user_ids = sorted(set(data.user_ids))
    check_user_ids(db, user_ids)

    existing = {uid for (uid,) in db.query(RoomUser.user_id).filter(RoomUser.room_id == room_id).all()}
    new_ids = [uid for uid in user_ids if uid not in existing]
    for uid in new_ids:
        db.add(RoomUser(room_id=room_id, user_id=uid))
    db.commit()
    return {"added": len(new_ids)}


class RemoveMembersRequest(BaseModel):
    user_ids: List[int] = []
    all: bool = False        # True = remove every member of the room


@app.post("/admin/rooms/{room_id}/members/remove")
def remove_room_members(
    room_id: int,
    data: RemoveMembersRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Bulk remove: the selected users, or everyone, from this room (the users themselves are kept)."""
    get_room_or_404(db, room_id)
    query = db.query(RoomUser).filter(RoomUser.room_id == room_id)
    if not data.all:
        ids = sorted(set(data.user_ids))
        if not ids:
            return {"removed": 0}
        query = query.filter(RoomUser.user_id.in_(ids))
    removed = query.delete(synchronize_session=False)
    db.commit()
    return {"removed": removed}


@app.delete("/admin/rooms/{room_id}/members/{user_id}")
def remove_room_member(
    room_id: int,
    user_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    get_room_or_404(db, room_id)
    removed = (
        db.query(RoomUser)
        .filter(RoomUser.room_id == room_id, RoomUser.user_id == user_id)
        .delete(synchronize_session=False)
    )
    db.commit()
    if not removed:
        raise HTTPException(status_code=404, detail="This user is not in the room")
    return {"message": "Removed from the room"}



# =========================================================
# USERS PANEL (PRD 4.2): create, bulk upload, list, delete
# =========================================================

import csv
import io
import re
import threading

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FALLBACK_DEFAULT_PASSWORD = "Welcome@123"
BATCH_SIZE = 500


def get_default_password(db: Session) -> str:
    row = db.query(Setting).filter(Setting.key == "default_password").first()
    return row.value if row and row.value else FALLBACK_DEFAULT_PASSWORD


def name_from_email(email: str) -> str:
    """The PRD has no name field, so the part before @ is used as a display name."""
    return email.split("@")[0]


def user_to_dict(user: User, room_count: int = 0) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "college": user.college,
        "room_count": room_count,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


class CreateUserRequest(BaseModel):
    email: str
    college: str
    password: Optional[str] = None           # blank = default password


class DefaultPasswordRequest(BaseModel):
    password: str


@app.get("/admin/settings")
def read_settings(current_admin: int = Depends(get_current_admin), db: Session = Depends(get_db)):
    return {"default_password": get_default_password(db)}


@app.put("/admin/settings/default-password")
def set_default_password(
    data: DefaultPasswordRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    password = data.password.strip()
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="The default password needs at least 6 characters")
    row = db.query(Setting).filter(Setting.key == "default_password").first()
    if row:
        row.value = password
    else:
        db.add(Setting(key="default_password", value=password))
    notify(db, "Default Password Changed", "Default password was changed.", "settings.html")
    db.commit()
    return {"default_password": password}


# =========================================================
# ADMINS (Settings page: list, add, remove)
# =========================================================

def admin_to_dict(admin: Admin, current_admin: int):
    return {
        "id": admin.id,
        "name": admin.name,
        "email": admin.email,
        "created_at": admin.created_at,
        "is_me": admin.id == current_admin,
    }


@app.get("/admin/admins")
def list_admins(current_admin: int = Depends(get_current_admin), db: Session = Depends(get_db)):
    admins = db.query(Admin).order_by(Admin.created_at, Admin.id).all()
    return [admin_to_dict(a, current_admin) for a in admins]


@app.post("/admin/admins", status_code=201)
def add_admin(
    data: AdminSignupRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    name = data.name.strip()
    email = data.email.strip().lower()
    password = data.password.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Enter the admin's name")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="The password needs at least 6 characters")
    if db.query(Admin).filter(func.lower(Admin.email) == email).first():
        raise HTTPException(status_code=400, detail="An admin with this email already exists")

    admin = Admin(name=name, email=email, password_hash=hash_password(password))
    db.add(admin)
    notify(db, "Admin Added", "Admin was added.", "settings.html")
    db.commit()
    db.refresh(admin)
    return admin_to_dict(admin, current_admin)


@app.delete("/admin/admins/{admin_id}")
def remove_admin(
    admin_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    admin = db.query(Admin).filter(Admin.id == admin_id).first()
    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")
    if db.query(Admin).count() <= 1:
        raise HTTPException(status_code=400, detail="There must be at least one admin")
    db.delete(admin)
    notify(db, "Admin Removed", "Admin was removed.", "settings.html")
    db.commit()
    return {"message": "Admin removed"}


@app.get("/admin/users/list")
def users_page(
    search: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """The Users panel table: search by email, college or name, 50 at a time."""
    limit = max(1, min(limit, 200))
    room_counts = (
        db.query(RoomUser.user_id, func.count(RoomUser.room_id).label("c"))
        .group_by(RoomUser.user_id).subquery()
    )
    query = db.query(User, func.coalesce(room_counts.c.c, 0)).outerjoin(room_counts, room_counts.c.user_id == User.id)
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(or_(User.email.ilike(term), User.college.ilike(term), User.name.ilike(term)))
    total = query.count()
    rows = query.order_by(User.created_at.desc(), User.id.desc()).offset(max(0, offset)).limit(limit).all()
    return {"total": total, "users": [user_to_dict(u, c) for u, c in rows]}


@app.post("/admin/users", status_code=201)
def create_user(
    data: CreateUserRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    email = data.email.strip().lower()
    college = data.college.strip()
    password = (data.password or "").strip() or get_default_password(db)
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="The password needs at least 6 characters")

    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    if not college:
        raise HTTPException(status_code=400, detail="College is required")
    if db.query(User.id).filter(func.lower(User.email) == email).first():
        raise HTTPException(status_code=409, detail=f"{email} already has an account")

    user = User(name=name_from_email(email), email=email, college=college, password_hash=hash_password(password))
    db.add(user)
    notify_users(db, 1, "created")
    db.commit()
    db.refresh(user)
    return user_to_dict(user)


@app.delete("/admin/users/{user_id}")
def delete_user(
    user_id: int,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.query(RoomUser).filter(RoomUser.user_id == user_id).delete(synchronize_session=False)
    db.delete(user)
    notify_users(db, 1, "deleted")
    db.commit()
    return {"message": f"{user.email} deleted"}


class DeleteUsersRequest(BaseModel):
    ids: List[int] = []
    all: bool = False                # True = every user (or every user matching "search")
    search: Optional[str] = None


@app.post("/admin/users/delete")
def delete_many_users(
    data: DeleteUsersRequest,
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Deletes the selected users, or all users (matching the search), with their room memberships."""
    if data.all:
        query = db.query(User.id)
        if data.search and data.search.strip():
            term = f"%{data.search.strip()}%"
            query = query.filter(or_(User.email.ilike(term), User.college.ilike(term), User.name.ilike(term)))
        ids = [uid for (uid,) in query.all()]
    else:
        ids = sorted(set(data.ids))
    if not ids:
        return {"deleted": 0}

    deleted = 0
    for start in range(0, len(ids), BATCH_SIZE):      # 500 at a time keeps each query small
        chunk = ids[start:start + BATCH_SIZE]
        db.query(RoomUser).filter(RoomUser.user_id.in_(chunk)).delete(synchronize_session=False)
        deleted += db.query(User).filter(User.id.in_(chunk)).delete(synchronize_session=False)
    notify_users(db, deleted, "deleted")
    db.commit()
    return {"deleted": deleted}


# ---------- Bulk upload ----------

def read_rows(filename: str, data: bytes) -> List[dict]:
    """Reads a CSV or XLSX file into [{email, college, password}] (header names are case-insensitive)."""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise HTTPException(status_code=500, detail="XLSX needs openpyxl: run  pip3 install openpyxl")
        try:
            sheet = load_workbook(BytesIO(data), read_only=True, data_only=True).active
            table = [["" if v is None else str(v) for v in row] for row in sheet.iter_rows(values_only=True)]
        except Exception:
            raise HTTPException(status_code=400, detail="Couldn't read the XLSX file")
    elif name.endswith(".csv"):
        text_data = data.decode("utf-8-sig", errors="replace")
        table = list(csv.reader(io.StringIO(text_data)))
    else:
        raise HTTPException(status_code=400, detail="Upload a .csv or .xlsx file")

    table = [row for row in table if any(cell.strip() for cell in row)]
    if not table:
        raise HTTPException(status_code=400, detail="The file is empty")

    header = [h.strip().lower() for h in table[0]]
    if "email" not in header or "college" not in header:
        raise HTTPException(status_code=400, detail="The first row must have the columns: email, college (password optional)")
    col = {key: header.index(key) for key in ("email", "college", "password") if key in header}

    def cell(row, key):
        i = col.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    return [
        {"row": n, "email": cell(r, "email").lower(), "college": cell(r, "college"), "password": cell(r, "password")}
        for n, r in enumerate(table[1:], start=2)        # row 1 is the header
    ]


def run_user_import(job_id: int, rows: List[dict], room_id: Optional[int]):
    """Checks every row first, then inserts the good ones 500 at a time (one transaction per batch)."""
    db = SessionLocal()
    errors = []      # (row, email, reason)
    try:
        job = db.query(UploadJob).get(job_id)
        default_password = get_default_password(db)

        # 1. Validate every row
        existing = {e.lower(): uid for (uid, e) in db.query(User.id, User.email).all()}
        seen = set()
        valid = []
        existing_ids = []    # users who already have an account: added to the chosen room instead
        for r in rows:
            if not r["email"]:
                errors.append((r["row"], r["email"], "Email is empty"))
            elif not EMAIL_RE.match(r["email"]):
                errors.append((r["row"], r["email"], "Not a valid email"))
            elif r["email"] in seen:
                errors.append((r["row"], r["email"], "Duplicate email in the file"))
            elif r["email"] in existing:
                if room_id:
                    existing_ids.append(existing[r["email"]])
                else:
                    errors.append((r["row"], r["email"], "Email already has an account"))
            elif not r["college"]:
                errors.append((r["row"], r["email"], "College is empty"))
            elif r["password"] and len(r["password"]) < 6:
                errors.append((r["row"], r["email"], "Password is shorter than 6 characters"))
            else:
                valid.append(r)
            if r["email"]:
                seen.add(r["email"])

        job.skipped = len(errors)
        job.batches_total = (len(valid) + BATCH_SIZE - 1) // BATCH_SIZE
        db.commit()

        # 1b. Existing users go into the room (those already in it are left alone)
        if room_id and existing_ids:
            members = {uid for (uid,) in db.query(RoomUser.user_id).filter(RoomUser.room_id == room_id).all()}
            to_add = [uid for uid in dict.fromkeys(existing_ids) if uid not in members]
            for start in range(0, len(to_add), BATCH_SIZE):
                db.add_all([RoomUser(room_id=room_id, user_id=uid) for uid in to_add[start:start + BATCH_SIZE]])
                db.commit()
            job.existing_added = len(to_add)
            job.existing_already = len(existing_ids) - len(to_add)
            db.commit()

        # 2. Hash the passwords (bcrypt is slow on purpose, so it runs in parallel)
        hashes = hash_many([r["password"] or default_password for r in valid])
        for r, h in zip(valid, hashes):
            r["hash"] = h

        # 3. Insert in batches of 500
        for start in range(0, len(valid), BATCH_SIZE):
            batch = valid[start:start + BATCH_SIZE]
            try:
                users = [
                    User(
                        name=name_from_email(r["email"]),
                        email=r["email"],
                        college=r["college"],
                        password_hash=r["hash"],
                    )
                    for r in batch
                ]
                db.add_all(users)
                db.flush()                       # gives the new users their ids
                if room_id:
                    db.add_all([RoomUser(room_id=room_id, user_id=u.id) for u in users])
                job.succeeded += len(batch)
                job.batches_done += 1
                db.commit()
            except Exception as exc:
                db.rollback()
                job = db.query(UploadJob).get(job_id)
                job.failed += len(batch)
                job.batches_done += 1
                reason = f"Batch {start // BATCH_SIZE + 1} failed to save: {str(exc).splitlines()[0][:120]}"
                errors.extend((r["row"], r["email"], reason) for r in batch)
                db.commit()

        # 4. Error report CSV
        if errors:
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(["row", "email", "reason"])
            writer.writerows(sorted(errors))
            job.error_report = out.getvalue()
        job.status = "done"
        notify_users(db, job.succeeded or 0, "created")
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.query(UploadJob).get(job_id)
        if job:
            job.status = "failed"
            job.error_report = f"row,email,reason\n,,{str(exc)[:200]}\n"
            db.commit()
    finally:
        db.close()


def job_to_dict(job: UploadJob) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "total": job.total,
        "created": job.succeeded,
        "skipped": job.skipped,
        "failed": job.failed,
        "batches_total": job.batches_total,
        "batches_done": job.batches_done,
        "has_error_report": bool(job.error_report),
        "room_name": job.room_name,
        "existing_added": job.existing_added or 0,
        "existing_already": job.existing_already or 0,
    }


@app.post("/admin/users/bulk", status_code=202)
async def bulk_upload_users(
    file: UploadFile = File(...),
    room_id: Optional[int] = Form(None),
    current_admin: int = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if room_id and not db.query(Room.id).filter(Room.id == room_id).first():
        raise HTTPException(status_code=404, detail="Room not found")
    data = await file.read()          # PRD: one file of any size (no size limit)
    rows = read_rows(file.filename, data)
    if not rows:
        raise HTTPException(status_code=400, detail="The file has a header but no users")

    room = db.query(Room).filter(Room.id == room_id).first() if room_id else None
    job = UploadJob(type="users", status="running", total=len(rows), room_name=room.name if room else None)
    db.add(job)
    db.commit()
    db.refresh(job)

    # Runs in the background; the page asks for progress with GET /admin/jobs/{id}
    threading.Thread(target=run_user_import, args=(job.id, rows, room_id), daemon=True).start()
    return job_to_dict(job)


@app.get("/admin/jobs/{job_id}")
def read_job(job_id: int, current_admin: int = Depends(get_current_admin), db: Session = Depends(get_db)):
    job = db.query(UploadJob).filter(UploadJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Upload not found")
    return job_to_dict(job)


@app.get("/admin/jobs/{job_id}/errors.csv")
def job_errors(job_id: int, current_admin: int = Depends(get_current_admin), db: Session = Depends(get_db)):
    job = db.query(UploadJob).filter(UploadJob.id == job_id).first()
    if not job or not job.error_report:
        raise HTTPException(status_code=404, detail="No error report for this upload")
    return Response(
        content=job.error_report,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="user_upload_{job_id}_errors.csv"'},
    )



# =========================================================
# ONE-TIME: move images from the local uploads folder into R2
#   python3 main.py move-images-to-r2
# Run it once after adding the R2 keys to .env. Safe to run again
# (images already in R2 are skipped). Local files are left in place.
# =========================================================

def move_images_to_r2():
    if not isinstance(storage, R2Storage):
        print("R2 is not set up. Add R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET to .env first.")
        return
    local = LocalStorage()
    moved = skipped = missing = 0
    with SessionLocal() as db:
        for image in db.query(Image).order_by(Image.id).all():
            new_key = image_key(image.room_id, image.subject_id or 0, image.id)
            if image.file_path == new_key and storage.exists(new_key):
                skipped += 1
                continue
            old_full = os.path.basename(image.file_path) if "/" not in image.file_path else image.file_path
            if not local.exists(old_full):
                missing += 1
                print("  missing on disk:", image.file_path)
                continue
            with open(local._path(old_full), "rb") as f:
                full = f.read()
            thumb_path = thumb_name_for(old_full)
            if local.exists(thumb_path):
                with open(local._path(thumb_path), "rb") as f:
                    thumb = f.read()
            else:
                thumb = full
            storage.put(new_key, full)
            storage.put(thumb_name_for(new_key), thumb)
            image.file_path = new_key
            if not image.size_bytes:
                image.width, image.height = image_size_of(full)
                image.size_bytes = len(full)
            db.commit()
            moved += 1
    print(f"Done: {moved} moved to R2, {skipped} already there, {missing} missing on disk.")



# =========================================================
# WEB PAGES: the backend also serves the "frontend" folder, so online the whole
# site is one address (https://your-app.onrender.com/). Must stay at the very end,
# after all the API routes.
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    # The .html pages were uploaded next to main.py (no "frontend" folder).
    # Serve ONLY these pages, so main.py / .env etc. can never be downloaded.
    PAGES = {"index.html", "login.html", "signup.html", "dashboard.html", "room.html",
             "users.html", "settings.html", "user_dashboard.html"}

    @app.get("/", include_in_schema=False)
    def home_page():
        return FileResponse(os.path.join(BASE_DIR, "index.html"))

    @app.get("/{page}", include_in_schema=False)
    def html_page(page: str):
        if page not in PAGES or not os.path.isfile(os.path.join(BASE_DIR, page)):
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(os.path.join(BASE_DIR, page))


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "move-images-to-r2":
        move_images_to_r2()
    else:
        print("To start the server:  python3 -m uvicorn main:app --reload --port 8001")
        print("To move images to R2: python3 main.py move-images-to-r2")