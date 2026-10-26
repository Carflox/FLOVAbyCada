from flask import Flask, Response, render_template, request, jsonify, session, redirect
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, uuid, re
import psycopg
from psycopg.rows import dict_row
from pathlib import Path
from email.message import EmailMessage
import smtplib, secrets, hashlib, json, time, urllib.request, urllib.error, hmac
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode, quote, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "flova.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}


def load_local_env():
    """Load local settings without overriding environment variables set by the host."""
    env_file = BASE_DIR / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value.startswith(("\"", "'")) and value.endswith(("\"", "'")):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_local_env()

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_POSTGRES = DATABASE_URL.startswith("postgresql://")

app = Flask(__name__)
app.secret_key = os.environ.get("FLOVA_SECRET_KEY", "").strip() or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024

# Change these credentials before publishing the website.
ADMIN_USER = os.environ.get("FLOVA_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("FLOVA_ADMIN_PASSWORD", "cambia-esta-clave")


class PostgresCursor:
    def __init__(self, cursor, inserted_id=None):
        self._cursor = cursor
        self.lastrowid = inserted_id

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class PostgresConnection:
    """Small SQLite-compatible adapter so the existing queries can use PostgreSQL."""
    def __init__(self, url):
        self._conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
        self._failed = False

    def execute(self, query, params=()):
        # The app uses SQLite's ? placeholders and NOCASE collation. PostgreSQL
        # gets equivalent parameter placeholders; emails are normalized to lowercase.
        query = query.replace(" COLLATE NOCASE", "")
        query = re.sub(r"\bINTEGER PRIMARY KEY AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", query, flags=re.I)
        query = re.sub(r"\bINTEGER PRIMARY KEY\b", "BIGSERIAL PRIMARY KEY", query, flags=re.I)
        query = re.sub(r"\bINTEGER\b", "BIGINT", query, flags=re.I)
        query = re.sub(r"\bDATETIME\b", "TIMESTAMP", query, flags=re.I)
        query = query.replace("?", "%s")
        wants_id = bool(re.match(r"\s*INSERT\s+INTO\s+(customers|customer_addresses|items|orders)\b", query, re.I))
        if wants_id and " RETURNING " not in query.upper():
            query = query.rstrip().rstrip(";") + " RETURNING id"
        try:
            cursor = self._conn.execute(query, params)
            inserted_id = cursor.fetchone()["id"] if wants_id else None
            return PostgresCursor(cursor, inserted_id)
        except psycopg.Error:
            self._failed = True
            raise

    def rollback(self):
        self._conn.rollback()
        self._failed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is not None or self._failed:
                self._conn.rollback()
            else:
                self._conn.commit()
        finally:
            self._conn.close()
        return False


def db():
    if USE_POSTGRES:
        return PostgresConnection(DATABASE_URL)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn, table):
    if USE_POSTGRES:
        return {row["name"] for row in conn.execute(
            "SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?",
            (table,),
        ).fetchall()}
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('product','service')),
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'General',
                price REAL NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL DEFAULT '',
                stock INTEGER NOT NULL DEFAULT 0,
                reserved_stock INTEGER NOT NULL DEFAULT 0,
                payment_link_url TEXT NOT NULL DEFAULT '',
                payment_link_order_number TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Inicializa el catálogo de servicios una sola vez para que luego el
        # administrador pueda editar o eliminar cada servicio sin que reaparezca.
        if not conn.execute("SELECT 1 FROM app_settings WHERE key='default_services_seeded'").fetchone():
            default_services = [
                ("Portabilidad", "Telefonía", "Consulta cómo cambiar tu línea conservando tu número."),
                ("Línea nueva", "Telefonía", "Solicita información para activar una nueva línea."),
                ("Plan WiFi", "Internet", "Pregunta por los planes de internet WiFi disponibles."),
                ("Servicio técnico", "Soporte técnico", "Cuéntanos qué equipo o problema necesitas revisar."),
                ("Compra y venta de criptos", "Criptomonedas", "Solicita información sobre este servicio y sus condiciones."),
            ]
            for name, category, description in default_services:
                exists = conn.execute(
                    "SELECT 1 FROM items WHERE type='service' AND name=? COLLATE NOCASE LIMIT 1",
                    (name,),
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO items(type,name,category,price,description,stock,active) VALUES('service',?,?,0,?,0,1)",
                        (name, category, description),
                    )
            conn.execute("INSERT INTO app_settings(key,value) VALUES('default_services_seeded','1')")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                customer_contact TEXT NOT NULL,
                address TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                items_json TEXT NOT NULL,
                total REAL NOT NULL,
                payment_method TEXT NOT NULL DEFAULT 'Por coordinar',
                status TEXT NOT NULL DEFAULT 'Pendiente',
                customer_id INTEGER,
                customer_email TEXT NOT NULL DEFAULT '',
                payment_status TEXT NOT NULL DEFAULT 'unpaid',
                payment_provider TEXT NOT NULL DEFAULT '',
                provider_reference TEXT NOT NULL DEFAULT '',
                provider_details TEXT NOT NULL DEFAULT '{}',
                stock_deducted INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                document_type TEXT NOT NULL DEFAULT 'DNI',
                document_number TEXT NOT NULL DEFAULT '',
                email_verified INTEGER NOT NULL DEFAULT 1,
                last_login_at REAL,
                last_verified_at REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL COLLATE NOCASE,
                purpose TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(email, purpose)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customer_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                label TEXT NOT NULL DEFAULT 'Casa',
                recipient TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                district TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                reference TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE
            )
        """)
        # Safe migrations for existing installations; existing products and orders are preserved.
        item_columns = table_columns(conn, "items")
        if "reserved_stock" not in item_columns:
            conn.execute("ALTER TABLE items ADD COLUMN reserved_stock INTEGER NOT NULL DEFAULT 0")
        for column in ("payment_link_url", "payment_link_order_number"):
            if column not in item_columns:
                conn.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        # The owner supplied this fixed Izipay link for this exact product and amount.
        conn.execute("""UPDATE items SET payment_link_url=?, payment_link_order_number=?
            WHERE lower(name)=lower(?) AND price=? AND (payment_link_url='' OR payment_link_url IS NULL)""",
            ("https://checkout.izipay.pe/link/2a97fajagwzlm2", "LP04214119000001", "DRON E88PRO", 180.0))
        order_columns = table_columns(conn, "orders")
        if "stock_deducted" not in order_columns:
            # Existing orders used the old flow, which deducted stock when created.
            conn.execute("ALTER TABLE orders ADD COLUMN stock_deducted INTEGER NOT NULL DEFAULT 1")
            order_columns.add("stock_deducted")
        for column, definition in {
            "payment_method": "TEXT NOT NULL DEFAULT 'Por coordinar'",
            "customer_id": "INTEGER",
            "customer_email": "TEXT NOT NULL DEFAULT ''",
            "payment_status": "TEXT NOT NULL DEFAULT 'unpaid'",
            "payment_provider": "TEXT NOT NULL DEFAULT ''",
            "provider_reference": "TEXT NOT NULL DEFAULT ''",
            "provider_details": "TEXT NOT NULL DEFAULT '{}'",
            "stock_deducted": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in order_columns:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {definition}")
        customer_columns = table_columns(conn, "customers")
        for column, definition in {
            "phone": "TEXT NOT NULL DEFAULT ''",
            "document_type": "TEXT NOT NULL DEFAULT 'DNI'",
            "document_number": "TEXT NOT NULL DEFAULT ''",
            "email_verified": "INTEGER NOT NULL DEFAULT 1",
            "last_login_at": "REAL",
            "last_verified_at": "REAL",
        }.items():
            if column not in customer_columns:
                conn.execute(f"ALTER TABLE customers ADD COLUMN {column} {definition}")


def email_is_configured():
    return all(os.environ.get(k) for k in ("FLOVA_SMTP_HOST", "FLOVA_SMTP_USER", "FLOVA_SMTP_PASSWORD", "FLOVA_FROM_EMAIL"))


def send_email(to_email, subject, body):
    """Send email via configured SMTP. In local development, print the message to the terminal."""
    if not email_is_configured():
        print("\n" + "=" * 60 + "\nFLOVA EMAIL (SMTP NO CONFIGURADO - SOLO DESARROLLO)\nPara: " + to_email + "\nAsunto: " + subject + "\n" + body + "\n" + "=" * 60 + "\n")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("FLOVA_FROM_EMAIL")
    msg["To"] = to_email
    msg.set_content(body)
    host = os.environ["FLOVA_SMTP_HOST"]
    port = int(os.environ.get("FLOVA_SMTP_PORT", "587"))
    use_ssl = os.environ.get("FLOVA_SMTP_SSL", "0") == "1"
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                server.login(os.environ["FLOVA_SMTP_USER"], os.environ["FLOVA_SMTP_PASSWORD"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(os.environ["FLOVA_SMTP_USER"], os.environ["FLOVA_SMTP_PASSWORD"])
                server.send_message(msg)
        return True
    except Exception as exc:
        app.logger.exception("No se pudo enviar correo a %s", to_email)
        return False


def hash_code(code):
    return hashlib.sha256((app.secret_key + ":" + str(code)).encode("utf-8")).hexdigest()


def issue_code(email, purpose, payload=None):
    code = f"{secrets.randbelow(1000000):06d}"
    now = time.time()
    with db() as conn:
        old = conn.execute("SELECT created_at FROM auth_codes WHERE email=? AND purpose=?", (email, purpose)).fetchone()
        if old and now - old["created_at"] < 45:
            return None, "Espera unos segundos antes de solicitar otro código."
        conn.execute("""INSERT INTO auth_codes(email,purpose,code_hash,payload_json,expires_at,created_at,attempts)
            VALUES(?,?,?,?,?,?,0) ON CONFLICT(email,purpose) DO UPDATE SET
            code_hash=excluded.code_hash,payload_json=excluded.payload_json,expires_at=excluded.expires_at,
            created_at=excluded.created_at,attempts=0""",
            (email, purpose, hash_code(code), json.dumps(payload or {}, ensure_ascii=False), now + 600, now))
    subject = "Confirma tu registro en FLOVA by Cada" if purpose == "register" else "Confirma tu inicio de sesión en FLOVA by Cada"
    greeting = "Gracias por registrarte en FLOVA by Cada." if purpose == "register" else "Detectamos un inicio de sesión en tu cuenta de FLOVA by Cada."
    body = f"Hola.\n\n{greeting}\n\nTu código de verificación es: {code}\n\nEl código vence en 10 minutos. Si no solicitaste esta operación, ignora este correo y cambia tu contraseña si sospechas que alguien accedió a tu cuenta.\n\nFLOVA by Cada\nConfianza y buen servicio, siempre a tu lado."
    configured = email_is_configured()
    sent = send_email(email, subject, body)
    if configured and not sent:
        with db() as conn:
            conn.execute("DELETE FROM auth_codes WHERE email=? AND purpose=?", (email, purpose))
        return None, "No se pudo enviar el correo de verificación. Revisa la configuración SMTP e inténtalo de nuevo."
    return {"email_sent": sent, "development_mode": not configured}, None


def send_order_email(email, subject, greeting, order_id, customer_name, items, total, payment, status, notes=""):
    if not email:
        return
    lines = [f"Hola {customer_name},", "", greeting, f"Pedido: #{order_id}", f"Estado: {status}", f"Modalidad de pago elegida: {payment}", "", "Detalle:"]
    for item in items:
        lines.append(f"- {item.get('name')} x {item.get('quantity')} — S/ {float(item.get('price', 0))*int(item.get('quantity', 1)):.2f}")
    lines += ["", f"Total estimado: S/ {float(total):.2f}"]
    if notes:
        lines += [f"Nota: {notes}"]
    lines += ["", "Puedes consultar el estado de tu pedido iniciando sesión en tu cuenta de cliente.", "FLOVA by Cada"]
    return send_email(email, subject, "\n".join(lines))


def izipay_is_configured():
    # Fixed hosted links need no browser/API keys; the hash key is required to
    # authenticate Izipay notifications before updating an order or inventory.
    return bool(os.environ.get("FLOVA_IZIPAY_HASH_KEY", "").strip())


def izipay_signature_valid(data):
    secret=os.environ.get("FLOVA_IZIPAY_HASH_KEY","").strip()
    payload=data.get("payloadHttp") if isinstance(data,dict) else None
    signature=data.get("signature") if isinstance(data,dict) else None
    if not secret or not isinstance(payload,str) or not isinstance(signature,str): return False
    expected=hmac.new(secret.encode(),payload.encode(),hashlib.sha256).digest()
    try: supplied=__import__("base64").b64decode(signature,validate=True)
    except Exception: return False
    return hmac.compare_digest(expected,supplied)


def item_dict(row, public=False):
    d = dict(row)
    reserved = int(d.get("reserved_stock", 0) or 0)
    if public:
        d.pop("payment_link_url", None)
        d.pop("payment_link_order_number", None)
    if public and d.get("type") == "product":
        d["stock"] = max(0, int(d["stock"]) - reserved)
        d.pop("reserved_stock", None)
    else:
        d["reserved_stock"] = reserved
    d["stock_status"] = "Agotado" if d["stock"] <= 0 else ("Última unidad" if d["stock"] == 1 else "Disponible")
    return d


def order_product_lines(order):
    data = dict(order)
    try:
        items = json.loads(data.get("items_json") or "[]")
    except (TypeError, ValueError):
        items = []
    return [item for item in items if item.get("type") == "product"]


def deduct_confirmed_order_stock(conn, order):
    data = dict(order)
    if int(data.get("stock_deducted", 0) or 0):
        return
    for item in order_product_lines(data):
        quantity, item_id = int(item["quantity"]), int(item["id"])
        cur = conn.execute(
            """UPDATE items SET stock=stock-?,
               reserved_stock=CASE WHEN reserved_stock>=? THEN reserved_stock-? ELSE 0 END
               WHERE id=? AND stock>=?""",
            (quantity, quantity, quantity, item_id, quantity),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"No hay stock suficiente para confirmar el producto {item_id} del pedido {data['id']}.")
    conn.execute("UPDATE orders SET stock_deducted=1 WHERE id=?", (data["id"],))


def release_order_stock(conn, order):
    data = dict(order)
    deducted = int(data.get("stock_deducted", 0) or 0)
    for item in order_product_lines(data):
        quantity, item_id = int(item["quantity"]), int(item["id"])
        if deducted:
            conn.execute("UPDATE items SET stock=stock+? WHERE id=?", (quantity, item_id))
        else:
            conn.execute(
                "UPDATE items SET reserved_stock=CASE WHEN reserved_stock>=? THEN reserved_stock-? ELSE 0 END WHERE id=?",
                (quantity, quantity, item_id),
            )
    if deducted:
        conn.execute("UPDATE orders SET stock_deducted=0 WHERE id=?", (data["id"],))


def is_admin():
    return bool(session.get("is_admin"))


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/admin")
def admin_page():
    if not is_admin():
        return render_template("login.html")
    return render_template("admin.html")


@app.get("/cuenta")
def customer_account():
    return render_template("account.html")


@app.get("/checkout")
def checkout_page():
    return render_template("checkout.html", izipay_configured=izipay_is_configured())


@app.get("/api/items")
def get_items():
    kind = request.args.get("type")
    with db() as conn:
        if kind in ("product", "service"):
            rows = conn.execute("SELECT * FROM items WHERE active=1 AND type=? ORDER BY id DESC", (kind,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM items WHERE active=1 ORDER BY id DESC").fetchall()
    return jsonify([item_dict(r, public=True) for r in rows])


@app.get("/api/contact")
def get_contact_links():
    links = []
    whatsapp_value = os.environ.get("FLOVA_CONTACT_WHATSAPP", "").strip() or "+51 987 654 321"
    whatsapp = re.sub(r"\D", "", whatsapp_value)
    if 8 <= len(whatsapp) <= 15:
        links.append({"label": "WhatsApp", "href": f"https://wa.me/{whatsapp}", "icon": "WA"})

    email = os.environ.get("FLOVA_CONTACT_EMAIL", "").strip()
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        links.append({"label": "Correo", "href": f"mailto:{email}", "icon": "@"})

    social_profiles = [
        ("FLOVA_INSTAGRAM_URL", "Instagram", {"instagram.com"}),
        ("FLOVA_FACEBOOK_URL", "Facebook", {"facebook.com", "fb.me"}),
        ("FLOVA_TIKTOK_URL", "TikTok", {"tiktok.com"}),
    ]
    for env_name, label, allowed_hosts in social_profiles:
        value = os.environ.get(env_name, "").strip()
        if env_name == "FLOVA_FACEBOOK_URL" and not value:
            value = "https://www.facebook.com/flovabycadaofficial"
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "https" and any(host == domain or host.endswith("." + domain) for domain in allowed_hosts):
            links.append({"label": label, "href": value, "icon": label[0]})
    return jsonify({"links": links})


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    if data.get("username") == ADMIN_USER and data.get("password") == ADMIN_PASSWORD:
        session.clear()
        session["is_admin"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "message": "Usuario o contraseña incorrectos"}), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.post("/api/register")
def register_customer():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    phone = str(data.get("phone", "")).strip()
    document_type = str(data.get("document_type", "DNI")).strip()
    document_number = str(data.get("document_number", "")).strip()
    terms = bool(data.get("terms"))
    if not name or not email or not phone or len(password) < 8 or not terms:
        return jsonify({"message": "Completa los datos obligatorios, acepta los términos y utiliza una contraseña de al menos 8 caracteres."}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"message": "Introduce un correo electrónico válido."}), 400
    if not document_number:
        return jsonify({"message": "Introduce tu número de documento."}), 400
    with db() as conn:
        existing = conn.execute("SELECT id FROM customers WHERE email=?", (email,)).fetchone()
    if existing:
        return jsonify({"message": "Ya existe una cuenta con ese correo. Inicia sesión."}), 409
    with db() as conn:
        try:
            cur = conn.execute("""INSERT INTO customers(name,email,password_hash,phone,document_type,document_number,email_verified,last_login_at)
                VALUES(?,?,?,?,?,?,1,?)""", (name, email, generate_password_hash(password), phone, document_type, document_number, time.time()))
            customer_id = cur.lastrowid
        except (sqlite3.IntegrityError, psycopg.IntegrityError):
            return jsonify({"message": "Ya existe una cuenta con ese correo. Inicia sesión."}), 409
    establish_customer_session(customer_id, name, email)
    return jsonify({"ok": True, "name": name, "email": email})


@app.post("/api/customer/login")
def customer_login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    with db() as conn:
        customer = conn.execute("SELECT * FROM customers WHERE email=?", (email,)).fetchone()
    if not customer or not check_password_hash(customer["password_hash"], password):
        return jsonify({"message": "Correo o contraseña incorrectos."}), 401
    now = time.time()
    with db() as conn:
        conn.execute("UPDATE customers SET last_login_at=? WHERE id=?", (now, customer["id"]))
    establish_customer_session(customer["id"], customer["name"], email)
    return jsonify({"ok": True, "name": customer["name"], "email": email})


def establish_customer_session(customer_id, customer_name, email):
    session.pop("is_admin", None)
    session["customer_id"] = customer_id
    session["customer_name"] = customer_name
    session["customer_email"] = email


@app.post("/api/customer/verify-code")
def verify_customer_code():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    purpose = str(data.get("purpose", "")).strip()
    code = str(data.get("code", "")).strip()
    if purpose not in ("register", "login") or len(code) != 6 or not code.isdigit():
        return jsonify({"message": "Introduce el código de seis dígitos."}), 400
    with db() as conn:
        record = conn.execute("SELECT * FROM auth_codes WHERE email=? AND purpose=?", (email, purpose)).fetchone()
        if not record:
            return jsonify({"message": "No hay un código activo. Solicita uno nuevo."}), 400
        if record["expires_at"] < time.time():
            conn.execute("DELETE FROM auth_codes WHERE email=? AND purpose=?", (email, purpose))
            return jsonify({"message": "El código venció. Solicita uno nuevo."}), 400
        if record["attempts"] >= 5:
            conn.execute("DELETE FROM auth_codes WHERE email=? AND purpose=?", (email, purpose))
            return jsonify({"message": "Se superó el número de intentos. Solicita un código nuevo."}), 429
        if not secrets.compare_digest(record["code_hash"], hash_code(code)):
            conn.execute("UPDATE auth_codes SET attempts=attempts+1 WHERE email=? AND purpose=?", (email, purpose))
            return jsonify({"message": "El código no es correcto."}), 400
        payload = json.loads(record["payload_json"] or "{}")
        conn.execute("DELETE FROM auth_codes WHERE email=? AND purpose=?", (email, purpose))
        if purpose == "register":
            try:
                cur = conn.execute("""INSERT INTO customers(name,email,password_hash,phone,document_type,document_number,email_verified)
                    VALUES(?,?,?,?,?,?,1)""", (payload["name"], email, payload["password_hash"], payload.get("phone", ""),
                    payload.get("document_type", "DNI"), payload.get("document_number", "")))
                customer_id = cur.lastrowid
            except (sqlite3.IntegrityError, psycopg.IntegrityError):
                return jsonify({"message": "Ya existe una cuenta con ese correo. Inicia sesión."}), 409
            customer_name = payload["name"]
            conn.execute("UPDATE customers SET last_verified_at=? WHERE id=?", (time.time(), customer_id))
            send_email(email, "¡Bienvenido a FLOVA by Cada!", f"Hola {customer_name},\n\nTu registro se confirmó correctamente. Ya puedes iniciar sesión, realizar compras y consultar el estado de tus pedidos en tu panel de cliente.\n\nFLOVA by Cada\nConfianza y buen servicio, siempre a tu lado.")
        else:
            customer_id = int(payload.get("customer_id", 0))
            customer = conn.execute("SELECT id,name,email FROM customers WHERE id=?", (customer_id,)).fetchone()
            if not customer:
                return jsonify({"message": "No se encontró la cuenta. Inicia sesión de nuevo."}), 404
            customer_name = customer["name"]
            now = time.time()
            conn.execute("UPDATE customers SET last_login_at=?, last_verified_at=? WHERE id=?", (now, now, customer_id))
            send_email(email, "Inicio de sesión confirmado — FLOVA by Cada", f"Hola {customer_name},\n\nTu inicio de sesión en FLOVA by Cada se confirmó correctamente. Si no fuiste tú, cambia tu contraseña y contacta con nosotros.\n\nFLOVA by Cada")
    establish_customer_session(customer_id, customer_name, email)
    return jsonify({"ok": True, "name": customer_name, "email": email})


@app.post("/api/customer/resend-code")
def resend_customer_code():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    purpose = str(data.get("purpose", "")).strip()
    if purpose not in ("register", "login") or not email:
        return jsonify({"message": "Solicitud no válida."}), 400
    with db() as conn:
        existing = conn.execute("SELECT payload_json FROM auth_codes WHERE email=? AND purpose=?", (email, purpose)).fetchone()
        if not existing:
            return jsonify({"message": "No hay una verificación pendiente. Vuelve a iniciar el proceso."}), 400
        payload = json.loads(existing["payload_json"] or "{}")
    issued, error = issue_code(email, purpose, payload)
    if error:
        return jsonify({"message": error}), 429
    message = "Código reenviado." if issued["email_sent"] else "Modo de prueba: revisa la terminal de VS Code para ver el nuevo código."
    return jsonify({"ok": True, "message": message})


@app.post("/api/customer/logout")
def customer_logout():
    session.pop("customer_id", None)
    session.pop("customer_name", None)
    session.pop("customer_email", None)
    return jsonify({"ok": True})


@app.get("/api/customer/me")
def customer_me():
    customer_id = session.get("customer_id")
    if not customer_id:
        return jsonify({"authenticated": False})
    with db() as conn:
        customer = conn.execute("SELECT id,name,email,phone,document_type,document_number,created_at FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        session.pop("customer_id", None)
        return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, **dict(customer)})


@app.put("/api/customer/profile")
def update_customer_profile():
    customer_id = session.get("customer_id")
    if not customer_id:
        return jsonify({"message": "Inicia sesión para actualizar tus datos."}), 401
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:100]
    phone = str(data.get("phone", "")).strip()[:30]
    document_type = str(data.get("document_type", "DNI")).strip()
    document_number = str(data.get("document_number", "")).strip()[:20]
    if not name or not phone or not document_number or document_type not in {"DNI", "CE", "Pasaporte", "RUC"}:
        return jsonify({"message": "Completa tus datos personales correctamente."}), 400
    with db() as conn:
        conn.execute("UPDATE customers SET name=?,phone=?,document_type=?,document_number=? WHERE id=?",
                     (name,phone,document_type,document_number,customer_id))
    session["customer_name"] = name
    return jsonify({"ok": True})


@app.get("/api/customer/addresses")
def customer_addresses():
    customer_id = session.get("customer_id")
    if not customer_id:
        return jsonify({"message": "Inicia sesión para ver tus direcciones."}), 401
    with db() as conn:
        rows = conn.execute("SELECT * FROM customer_addresses WHERE customer_id=? ORDER BY is_default DESC,id DESC", (customer_id,)).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/customer/addresses")
def create_customer_address():
    customer_id = session.get("customer_id")
    if not customer_id:
        return jsonify({"message": "Inicia sesión para guardar una dirección."}), 401
    data = request.get_json(silent=True) or {}
    label = str(data.get("label", "Casa")).strip()[:40] or "Casa"
    recipient = str(data.get("recipient", "")).strip()[:100]
    phone = str(data.get("phone", "")).strip()[:30]
    address = str(data.get("address", "")).strip()[:200]
    district = str(data.get("district", "")).strip()[:100]
    city = str(data.get("city", "")).strip()[:100]
    region = str(data.get("region", "")).strip()[:100]
    reference = str(data.get("reference", "")).strip()[:200]
    if not recipient or not phone or not address or not city or not region:
        return jsonify({"message": "Completa nombre, celular, dirección, ciudad y región."}), 400
    with db() as conn:
        existing = conn.execute("SELECT COUNT(*) AS n FROM customer_addresses WHERE customer_id=?", (customer_id,)).fetchone()["n"]
        make_default = bool(data.get("is_default")) or existing == 0
        if make_default:
            conn.execute("UPDATE customer_addresses SET is_default=0 WHERE customer_id=?", (customer_id,))
        cur = conn.execute("""INSERT INTO customer_addresses(customer_id,label,recipient,phone,address,district,city,region,reference,is_default)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (customer_id,label,recipient,phone,address,district,city,region,reference,int(make_default)))
        row = conn.execute("SELECT * FROM customer_addresses WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.delete("/api/customer/addresses/<int:address_id>")
def delete_customer_address(address_id):
    customer_id = session.get("customer_id")
    if not customer_id:
        return jsonify({"message": "Inicia sesión para gestionar tus direcciones."}), 401
    with db() as conn:
        row = conn.execute("SELECT is_default FROM customer_addresses WHERE id=? AND customer_id=?", (address_id,customer_id)).fetchone()
        if not row:
            return jsonify({"message": "No encontramos esa dirección."}), 404
        conn.execute("DELETE FROM customer_addresses WHERE id=? AND customer_id=?", (address_id,customer_id))
        if row["is_default"]:
            next_row = conn.execute("SELECT id FROM customer_addresses WHERE customer_id=? ORDER BY id DESC LIMIT 1", (customer_id,)).fetchone()
            if next_row:
                conn.execute("UPDATE customer_addresses SET is_default=1 WHERE id=?", (next_row["id"],))
    return jsonify({"ok": True})


@app.patch("/api/customer/addresses/<int:address_id>/default")
def set_default_customer_address(address_id):
    customer_id = session.get("customer_id")
    if not customer_id:
        return jsonify({"message": "Inicia sesión para gestionar tus direcciones."}), 401
    with db() as conn:
        row = conn.execute("SELECT id FROM customer_addresses WHERE id=? AND customer_id=?", (address_id,customer_id)).fetchone()
        if not row:
            return jsonify({"message": "No encontramos esa dirección."}), 404
        conn.execute("UPDATE customer_addresses SET is_default=0 WHERE customer_id=?", (customer_id,))
        conn.execute("UPDATE customer_addresses SET is_default=1 WHERE id=? AND customer_id=?", (address_id,customer_id))
    return jsonify({"ok": True})


@app.get("/api/customer/orders")
def customer_orders():
    customer_id = session.get("customer_id")
    if not customer_id:
        return jsonify({"message": "Inicia sesión para consultar tus pedidos."}), 401
    with db() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE customer_id=? ORDER BY id DESC", (customer_id,)).fetchall()
    output = []
    for row in rows:
        d = dict(row)
        try:
            d["items"] = json.loads(d.pop("items_json"))
        except Exception:
            d["items"] = []
        try:
            d["payment_details"] = json.loads(d.get("provider_details") or "{}")
        except Exception:
            d["payment_details"] = {}
        output.append(d)
    return jsonify(output)


@app.get("/api/admin/items")
def admin_items():
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    with db() as conn:
        rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
    return jsonify([item_dict(r) for r in rows])


@app.post("/api/admin/customers/reset-password")
def admin_reset_customer_password():
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not email or len(password) < 8:
        return jsonify({"message": "Indica el correo del cliente y una contraseña de al menos 8 caracteres."}), 400
    with db() as conn:
        customer = conn.execute("SELECT id FROM customers WHERE email=?", (email,)).fetchone()
        if not customer:
            return jsonify({"message": "No existe una cuenta con ese correo."}), 404
        conn.execute("UPDATE customers SET password_hash=? WHERE id=?",
                     (generate_password_hash(password), customer["id"]))
    app.logger.info("Administrador restableció la contraseña de una cuenta de cliente.")
    return jsonify({"ok": True, "message": "Contraseña actualizada. El cliente ya puede iniciar sesión."})


@app.post("/api/admin/items")
def create_item():
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    data = request.form if request.form else (request.get_json(silent=True) or {})
    name = str(data.get("name", "")).strip()
    kind = str(data.get("type", "product"))
    if not name or kind not in ("product", "service"):
        return jsonify({"message": "Indica un nombre y un tipo válido."}), 400
    try:
        price = max(0, float(data.get("price", 0)))
        stock = max(0, int(data.get("stock", 0)))
    except (ValueError, TypeError):
        return jsonify({"message": "Precio o cantidad no válidos."}), 400
    image = str(data.get("image", "")).strip()
    uploaded = request.files.get("image_file") if request.files else None
    if uploaded and uploaded.filename and allowed_file(uploaded.filename):
        ext = uploaded.filename.rsplit(".", 1)[1].lower()
        filename = secure_filename(f"{uuid.uuid4().hex}.{ext}")
        uploaded.save(UPLOAD_DIR / filename)
        image = f"/static/uploads/{filename}"
    with db() as conn:
        payment_link_url = str(data.get("payment_link_url", "")).strip()
        payment_link_order_number = str(data.get("payment_link_order_number", "")).strip()
        if payment_link_url and (urlparse(payment_link_url).scheme != "https" or urlparse(payment_link_url).hostname != "checkout.izipay.pe" or not urlparse(payment_link_url).path.startswith("/link/")):
            return jsonify({"message": "Usa un link seguro de Izipay con formato https://checkout.izipay.pe/link/..."}), 400
        if payment_link_url and not payment_link_order_number:
            return jsonify({"message": "Agrega también el número de pedido/referencia que muestra Izipay para ese link."}), 400
        cur = conn.execute("""INSERT INTO items(type,name,category,price,description,image,stock,payment_link_url,payment_link_order_number,active)
            VALUES(?,?,?,?,?,?,?,?,?,1)""",
            (kind, name, str(data.get("category", "General")).strip() or "General", price,
             str(data.get("description", "")).strip(), image, stock, payment_link_url, payment_link_order_number))
        item_id = cur.lastrowid
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return jsonify(item_dict(row)), 201


@app.put("/api/admin/items/<int:item_id>")
def update_item(item_id):
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    data = request.form if request.form else (request.get_json(silent=True) or {})
    with db() as conn:
        old = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not old:
            return jsonify({"message": "No encontrado"}), 404
        name = str(data.get("name", old["name"])).strip()
        kind = str(data.get("type", old["type"]))
        try:
            price = max(0, float(data.get("price", old["price"])))
            stock = max(0, int(data.get("stock", old["stock"])))
        except (ValueError, TypeError):
            return jsonify({"message": "Precio o cantidad no válidos."}), 400
        reserved_stock = int(old["reserved_stock"] or 0)
        if stock < reserved_stock:
            return jsonify({"message": f"No puedes fijar el stock por debajo de las {reserved_stock} unidades reservadas en pagos pendientes."}), 409
        if old["type"] == "product" and kind != "product" and reserved_stock:
            return jsonify({"message": "No puedes convertir este producto en servicio mientras tenga pagos pendientes."}), 409
        image = str(data.get("image", old["image"])).strip()
        payment_link_url = str(data.get("payment_link_url", old["payment_link_url"] or "")).strip()
        payment_link_order_number = str(data.get("payment_link_order_number", old["payment_link_order_number"] or "")).strip()
        if payment_link_url and (urlparse(payment_link_url).scheme != "https" or urlparse(payment_link_url).hostname != "checkout.izipay.pe" or not urlparse(payment_link_url).path.startswith("/link/")):
            return jsonify({"message": "Usa un link seguro de Izipay con formato https://checkout.izipay.pe/link/..."}), 400
        if payment_link_url and not payment_link_order_number:
            return jsonify({"message": "Agrega también el número de pedido/referencia que muestra Izipay para ese link."}), 400
        uploaded = request.files.get("image_file") if request.files else None
        if uploaded and uploaded.filename and allowed_file(uploaded.filename):
            ext = uploaded.filename.rsplit(".", 1)[1].lower()
            filename = secure_filename(f"{uuid.uuid4().hex}.{ext}")
            uploaded.save(UPLOAD_DIR / filename)
            image = f"/static/uploads/{filename}"
        conn.execute("""UPDATE items SET type=?,name=?,category=?,price=?,description=?,image=?,stock=?,payment_link_url=?,payment_link_order_number=?,active=?
            WHERE id=?""",
            (kind, name, str(data.get("category", old["category"])).strip() or "General",
             price, str(data.get("description", old["description"])).strip(), image, stock, payment_link_url, payment_link_order_number,
             int(data.get("active", old["active"])), item_id))
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return jsonify(item_dict(row))


@app.delete("/api/admin/items/<int:item_id>")
def delete_item(item_id):
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    with db() as conn:
        item = conn.execute("SELECT reserved_stock FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            return jsonify({"message": "No encontrado"}), 404
        if int(item["reserved_stock"] or 0):
            return jsonify({"message": "No puedes eliminar un producto con pagos pendientes."}), 409
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    return jsonify({"ok": True})


@app.post("/api/orders")
def create_order():
    data = request.get_json(silent=True) or {}
    customer_id = session.get("customer_id")
    if not customer_id:
        return jsonify({"message": "Inicia sesión para realizar el pedido."}), 401
    if not izipay_is_configured():
        return jsonify({"message": "Falta configurar la Clave Hash de Izipay en Render para verificar los pagos."}), 503
    contact = str(data.get("customer_contact", "")).strip()
    address = str(data.get("address", "")).strip()
    notes = str(data.get("notes", "")).strip()
    cart = data.get("items", [])
    if not contact or not isinstance(cart, list) or len(cart) != 1:
        return jsonify({"message": "Cada pago debe contener un solo producto."}), 400
    try:
        item_id, quantity = int(cart[0].get("id")), int(cart[0].get("quantity", 1))
    except (ValueError, TypeError, AttributeError):
        return jsonify({"message": "El producto no es válido."}), 400
    if quantity != 1:
        return jsonify({"message": "El link fijo corresponde a una unidad del producto."}), 400
    with db() as conn:
        customer = conn.execute("SELECT id,name,email FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            session.pop("customer_id", None)
            return jsonify({"message": "Tu sesión venció. Inicia sesión de nuevo."}), 401
        item = conn.execute("SELECT * FROM items WHERE id=? AND active=1 AND type='product'", (item_id,)).fetchone()
        if not item:
            return jsonify({"message": "El producto ya no está disponible."}), 409
        payment_url = str(item["payment_link_url"] or "").strip()
        payment_reference = str(item["payment_link_order_number"] or "").strip()
        parsed_link = urlparse(payment_url)
        if parsed_link.scheme != "https" or parsed_link.hostname != "checkout.izipay.pe" or not parsed_link.path.startswith("/link/") or not payment_reference:
            return jsonify({"message": "Este producto aún no tiene configurado su link fijo de Izipay."}), 503
        price = Decimal(str(item["price"])).quantize(Decimal("0.01"))
        # Reuse this customer's still-pending checkout so returning to the form
        # cannot reserve the same unit repeatedly while the fixed link is open.
        pending_rows = conn.execute("""SELECT * FROM orders WHERE customer_id=? AND payment_provider='izipay_link'
            AND payment_status='pending' ORDER BY id DESC""", (customer_id,)).fetchall()
        for pending in pending_rows:
            try:
                pending_items = json.loads(pending["items_json"] or "[]")
            except (TypeError, ValueError):
                continue
            if (len(pending_items) == 1 and int(pending_items[0].get("id", 0)) == item_id
                    and int(pending_items[0].get("quantity", 0)) == 1
                    and Decimal(str(pending["total"])).quantize(Decimal("0.01")) == price
                    and pending["provider_reference"] == payment_reference):
                return jsonify({"ok": True, "order_id": pending["id"], "payment_url": payment_url,
                                "result_url": f"/pago/resultado?order_id={pending['id']}"}), 200
        if int(item["stock"]) - int(item["reserved_stock"] or 0) < 1:
            return jsonify({"message": "Este producto no tiene unidades disponibles."}), 409
        if price <= 0:
            return jsonify({"message": "El precio del producto no es válido."}), 400
        conn.execute("UPDATE customers SET phone=? WHERE id=? AND (phone='' OR phone IS NULL)", (contact, customer_id))
        cur = conn.execute("UPDATE items SET reserved_stock=reserved_stock+1 WHERE id=? AND stock-reserved_stock>=1", (item_id,))
        if cur.rowcount != 1:
            conn.rollback()
            return jsonify({"message": "El producto acaba de agotarse. Actualiza la página e inténtalo de nuevo."}), 409
        purchased = [{"id": item_id, "name": item["name"], "type": "product", "price": float(price), "quantity": 1}]
        cur = conn.execute("""INSERT INTO orders(customer_name,customer_contact,address,notes,items_json,total,payment_method,customer_id,customer_email,payment_status,stock_deducted,payment_provider,provider_reference)
            VALUES(?,?,?,?,?,?,?,?,?,'pending',0,'izipay_link',?)""",
            (customer["name"], contact, address, notes, json_dumps(purchased), float(price), "Izipay · Link de pago", customer_id, customer["email"], payment_reference))
        order_id = cur.lastrowid
    return jsonify({"ok": True, "order_id": order_id, "payment_url": payment_url,
                    "result_url": f"/pago/resultado?order_id={order_id}"}), 201


@app.get("/pago/resultado")
def payment_result():
    try: order_id=int(request.args.get("order_id","0"))
    except ValueError: order_id=0
    customer_id=session.get("customer_id")
    if not customer_id: return redirect("/cuenta?next=%2Fcheckout")
    with db() as conn: order=conn.execute("SELECT * FROM orders WHERE id=? AND customer_id=?",(order_id,customer_id)).fetchone() if order_id else None
    if not order: return redirect("/cuenta")
    result=dict(order); result["items"]=json.loads(result.pop("items_json") or "[]")
    try: result["payment_details"]=json.loads(result.get("provider_details") or "{}")
    except (TypeError,ValueError): result["payment_details"]={}
    return render_template("order_result.html",order=result)


@app.post("/api/payments/izipay/webhook")
def izipay_webhook():
    data = request.get_json(silent=True) or {}
    if "signature" not in data and request.headers.get("Signature"):
        data["signature"] = request.headers.get("Signature")
    if not izipay_signature_valid(data):
        app.logger.warning("Notificación de link Izipay rechazada: firma inválida")
        return jsonify({"ok": False, "message": "Firma de Izipay no válida."}), 401
    try:
        signed = json.loads(data["payloadHttp"])
        response = signed.get("response") or {}
        payments = response.get("order") or []
        payment = payments[0] if payments and isinstance(payments[0], dict) else response
        order_number = str(payment.get("orderNumber", "")).strip()
        txid = str(data.get("transactionId") or signed.get("transactionId") or "").strip()
        signed_txid = str(signed.get("transactionId") or txid).strip()
        amount = Decimal(str(payment.get("amount", "0"))).quantize(Decimal("0.01"))
        currency = str(payment.get("currency", ""))
        payer_email = str((response.get("billing") or {}).get("email", "")).strip().lower()
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return jsonify({"ok": False, "message": "Notificación de pago incompleta."}), 400
    header_txid = str(request.headers.get("transactionId", "")).strip()
    if not txid or txid != signed_txid or (header_txid and header_txid != txid):
        return jsonify({"ok": False, "message": "La referencia de transacción no coincide."}), 400
    if not order_number or currency != "PEN" or not payer_email:
        return jsonify({"ok": False, "message": "Faltan datos para asociar el pago a un pedido."}), 400
    approved = str(signed.get("code", data.get("code", ""))) == "00" and str(payment.get("stateMessage", "")).strip().lower() == "autorizado"
    send_order = None
    with db() as conn:
        already = conn.execute("SELECT payment_status FROM orders WHERE payment_provider='izipay_link' AND provider_reference=?", (txid,)).fetchone()
        if already:
            return jsonify({"ok": already["payment_status"] == "paid", "message": "Notificación ya procesada."}), 200
        item = conn.execute("SELECT * FROM items WHERE payment_link_order_number=? AND type='product'", (order_number,)).fetchone()
        if not item or Decimal(str(item["price"])).quantize(Decimal("0.01")) != amount:
            return jsonify({"ok": False, "message": "El link o importe no coincide con un producto configurado."}), 409
        rows = conn.execute("""SELECT * FROM orders WHERE payment_provider='izipay_link' AND payment_status='pending'
            AND customer_email=? AND total=? ORDER BY id""", (payer_email, float(amount))).fetchall()
        candidates = []
        for candidate in rows:
            try:
                lines = json.loads(candidate["items_json"] or "[]")
            except (TypeError, ValueError):
                continue
            if len(lines) == 1 and int(lines[0].get("id", 0)) == int(item["id"]) and int(lines[0].get("quantity", 0)) == 1:
                candidates.append(candidate)
        if len(candidates) != 1:
            app.logger.warning("Pago Izipay verificado pero no asociado de forma única: link=%s tx=%s", order_number, txid)
            return jsonify({"ok": False, "message": "No se pudo asociar el pago con un único pedido pendiente del mismo correo."}), 409
        order = candidates[0]
        details = {"status": "approved" if approved else "failed", "payment_link_order_number": order_number,
                   "payment_link_id": str(payment.get("paymentLinkId") or ""), "transaction_id": txid,
                   "authorization": str(payment.get("codeAuth") or ""), "reference": str(payment.get("referenceNumber") or ""),
                   "method": str(response.get("payMethod") or ""), "message": str(signed.get("messageUser") or "")[:180]}
        if approved:
            changed = conn.execute("UPDATE orders SET payment_status='paid',status='Confirmado',provider_reference=?,provider_details=? WHERE id=? AND payment_status='pending'",
                                   (txid, json_dumps(details), order["id"]))
            if changed.rowcount != 1:
                return jsonify({"ok": True, "message": "Notificación ya procesada."}), 200
            deduct_confirmed_order_stock(conn, order)
            send_order = dict(order)
        else:
            changed = conn.execute("UPDATE orders SET payment_status='failed',status='Cancelado',provider_reference=?,provider_details=? WHERE id=? AND payment_status='pending'",
                                   (txid, json_dumps(details), order["id"]))
            if changed.rowcount == 1:
                release_order_stock(conn, order)
    if send_order:
        items = json.loads(send_order["items_json"] or "[]")
        send_order_email(send_order["customer_email"], f"Pago confirmado — pedido #{send_order['id']}",
                         "Izipay confirmó tu pago. Ya estamos preparando tu compra.", send_order["id"], send_order["customer_name"],
                         items, send_order["total"], send_order["payment_method"], "Confirmado", send_order["notes"])
    return jsonify({"ok": True, "message": "Notificación procesada."}), 200


def json_dumps(value):
    return json.dumps(value,ensure_ascii=False)


@app.get("/api/admin/orders")
def get_orders():
    if not is_admin(): return jsonify({"message":"No autorizado"}),401
    with db() as conn: rows=conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200").fetchall()
    output=[]
    for row in rows:
        d=dict(row)
        try: d["items"]=json.loads(d.pop("items_json"))
        except Exception: d["items"]=[]
        try: d["payment_details"]=json.loads(d.get("provider_details") or "{}")
        except Exception: d["payment_details"]={}
        output.append(d)
    return jsonify(output)


@app.patch("/api/admin/orders/<int:order_id>")
def update_order(order_id):
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    data = request.get_json(silent=True) or {}
    status = data.get("status", "Pendiente")
    if status not in ("Pendiente", "Confirmado", "En preparación", "Entregado", "Cancelado"):
        return jsonify({"message": "Estado no válido"}), 400
    with db() as conn:
        old = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not old:
            return jsonify({"message": "Pedido no encontrado."}), 404
        if status == "Cancelado" and old["payment_status"] == "pending" and old["payment_provider"] in ("izipay", "izipay_link"):
            return jsonify({"message": "No puedes cancelar manualmente un pago Izipay pendiente; espera la notificación del proveedor."}), 409
        conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        order = dict(old)
    if order.get("customer_email") and order.get("status") != status:
        try:
            order_items = json.loads(order.get("items_json") or "[]")
        except Exception:
            order_items = []
        send_order_email(order["customer_email"], f"Actualización del pedido #{order_id} — FLOVA by Cada",
                         f"El estado de tu pedido cambió a: {status}.", order_id, order["customer_name"],
                         order_items, order["total"], order.get("payment_method", "Por coordinar"), status, order.get("notes", ""))
    return jsonify({"ok": True, "status": status})


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"message": "La imagen supera el límite de 5 MB."}), 413


init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLOVA_DEBUG", "0") == "1")
