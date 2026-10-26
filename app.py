from flask import Flask, Response, render_template, request, jsonify, session, redirect
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, uuid, re
import psycopg
from psycopg.rows import dict_row
from pathlib import Path
from email.message import EmailMessage
import smtplib, secrets, hashlib, json, time, urllib.request, urllib.error, hmac, threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
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
                mp_category_id TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL DEFAULT '',
                stock INTEGER NOT NULL DEFAULT 0,
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
                mp_order_id TEXT NOT NULL DEFAULT '',
                mp_payment_details TEXT NOT NULL DEFAULT '{}',
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
        if "mp_category_id" not in item_columns:
            conn.execute("ALTER TABLE items ADD COLUMN mp_category_id TEXT NOT NULL DEFAULT ''")
        order_columns = table_columns(conn, "orders")
        for column, definition in {
            "payment_method": "TEXT NOT NULL DEFAULT 'Por coordinar'",
            "customer_id": "INTEGER",
            "customer_email": "TEXT NOT NULL DEFAULT ''",
            "payment_status": "TEXT NOT NULL DEFAULT 'unpaid'",
            "mp_order_id": "TEXT NOT NULL DEFAULT ''",
            "mp_payment_details": "TEXT NOT NULL DEFAULT '{}'",
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


_mp_oauth_token = {"access_token": "", "expires_at": 0}
_mp_oauth_lock = threading.Lock()


def mercado_pago_is_configured():
    client_id = os.environ.get("FLOVA_MP_CLIENT_ID", "").strip()
    client_secret = os.environ.get("FLOVA_MP_CLIENT_SECRET", "").strip()
    return bool((client_id and client_secret) or os.environ.get("FLOVA_MP_ACCESS_TOKEN", "").strip())


def mercado_pago_access_token():
    client_id = os.environ.get("FLOVA_MP_CLIENT_ID", "").strip()
    client_secret = os.environ.get("FLOVA_MP_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        now = time.time()
        if _mp_oauth_token["access_token"] and _mp_oauth_token["expires_at"] > now + 60:
            return _mp_oauth_token["access_token"]
        with _mp_oauth_lock:
            now = time.time()
            if _mp_oauth_token["access_token"] and _mp_oauth_token["expires_at"] > now + 60:
                return _mp_oauth_token["access_token"]
            body = json.dumps({"client_id": client_id, "client_secret": client_secret,
                               "grant_type": "client_credentials"}).encode("utf-8")
            req = urllib.request.Request("https://api.mercadopago.com/oauth/token", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    result = json.loads(response.read().decode("utf-8"))
                access_token = result.get("access_token")
                expires_in = int(result.get("expires_in", 0))
                if not access_token or expires_in <= 0:
                    raise RuntimeError("Mercado Pago no devolvió un Access Token OAuth válido.")
                _mp_oauth_token.update(access_token=access_token, expires_at=time.time() + expires_in)
                return access_token
            except urllib.error.HTTPError as exc:
                app.logger.error("Mercado Pago OAuth respondió %s al obtener credenciales.", exc.code)
                raise RuntimeError("No se pudo obtener el Access Token OAuth. Revisa el Client ID y Client Secret.") from exc
            except (ValueError, urllib.error.URLError) as exc:
                app.logger.exception("No se pudo completar la solicitud OAuth de Mercado Pago")
                raise RuntimeError("No se pudo conectar con OAuth de Mercado Pago.") from exc
    static_token = os.environ.get("FLOVA_MP_ACCESS_TOKEN", "").strip()
    if static_token:
        return static_token
    raise RuntimeError("Configura FLOVA_MP_CLIENT_ID y FLOVA_MP_CLIENT_SECRET, o un FLOVA_MP_ACCESS_TOKEN.")


class MercadoPagoRateLimitError(RuntimeError):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        message = (f"Mercado Pago limitó temporalmente las solicitudes. Espera {retry_after} segundos antes de volver a intentar."
                   if retry_after is not None else "Mercado Pago limitó temporalmente las solicitudes. Espera el tiempo indicado por Retry-After antes de volver a intentar.")
        super().__init__(message)

def mercado_pago_request(path, method="GET", payload=None, idempotency_key=None, caller_id=None):
    token = mercado_pago_access_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if caller_id:
        headers["X-Caller-Id"] = str(caller_id)
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    req = urllib.request.Request("https://api.mercadopago.com" + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        app.logger.error("Mercado Pago respondió %s: %s", exc.code, detail[:1000])
        if exc.code == 429:
            raw_retry_after = (exc.headers.get("Retry-After", "") if exc.headers else "").strip()
            try:
                retry_after = max(1, int(raw_retry_after))
            except (TypeError, ValueError):
                retry_after = None
            raise MercadoPagoRateLimitError(retry_after) from exc
        raise RuntimeError("Mercado Pago no pudo procesar el pago. Inténtalo nuevamente.") from exc


def mercado_pago_upload_chargeback_documents(chargeback_id, uploads, caller_id):
    token = mercado_pago_access_token()
    boundary = "----FlovaEvidence" + uuid.uuid4().hex
    chunks = []
    for filename, content, content_type in uploads:
        safe_name = secure_filename(filename) or "evidence"
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
            content, b"\r\n"
        ])
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    req = urllib.request.Request(
        "https://api.mercadopago.com/v1/chargebacks/" + quote(chargeback_id, safe="") + "/documentation",
        data=b"".join(chunks), method="POST", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Caller-Id": caller_id, "X-Idempotency-Key": str(uuid.uuid4())
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        app.logger.error("Mercado Pago respondió %s al subir evidencia del contracargo %s: %s",
                         exc.code, chargeback_id, detail[:1000])
        raise RuntimeError("Mercado Pago no pudo recibir la documentación. Revisa el estado del caso y los archivos.") from exc


def mercado_pago_get_chargeback_document(doc_type, doc_uuid, caller_id):
    token = mercado_pago_access_token()
    path = "/v1/chargebacks/documentation/" + quote(doc_type, safe="") + "/" + quote(doc_uuid, safe="")
    req = urllib.request.Request("https://api.mercadopago.com" + path, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/pdf,image/jpeg,image/png",
        "X-Caller-Id": caller_id
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            content_type = response.headers.get_content_type().lower()
            content = response.read(6 * 1024 * 1024 + 1)
            if content_type not in {"application/pdf", "image/jpeg", "image/png"}:
                raise RuntimeError("Mercado Pago devolvió un formato de documento no admitido.")
            if len(content) > 6 * 1024 * 1024:
                raise RuntimeError("El documento supera el tamaño admitido para visualizarlo.")
            return content, content_type
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        app.logger.error("Mercado Pago respondió %s al descargar documentación de contracargo: %s",
                         exc.code, detail[:1000])
        raise RuntimeError("No se pudo obtener el documento de Mercado Pago.") from exc


def payment_transaction_from_order(remote):
    transactions = remote.get("transactions") or {}
    payments = transactions.get("payments") or []
    return payments[0] if payments and isinstance(payments[0], dict) else {}

def reconcile_mp_order(order_id, mp_order_id=None, order_data=None):
    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or not (mp_order_id or row["mp_order_id"]):
        return False
    remote = order_data or mercado_pago_request("/v1/orders/" + quote(mp_order_id or row["mp_order_id"], safe=""))
    if remote.get("external_reference") != str(order_id):
        app.logger.error("Referencia externa no coincide para el pedido %s", order_id)
        return False
    payment_details = payment_details_from_order(remote)
    transaction = payment_transaction_from_order(remote)
    paid_amount = Decimal(str(transaction.get("paid_amount") or remote.get("total_paid_amount") or "0"))
    expected_amount = Decimal(str(row["total"]))
    status = str(remote.get("status") or "").lower()
    transaction_status = str(transaction.get("status") or "").lower()
    now_paid = status == "processed" and transaction_status == "processed" and paid_amount >= expected_amount
    terminal_failure = status in {"failed", "canceled", "cancelled", "expired"}
    changed = False
    with db() as conn:
        current = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not current:
            return False
        conn.execute("UPDATE orders SET mp_payment_details=? WHERE id=?",
                     (json_dumps(payment_details), order_id))
        if current["payment_status"] != "pending":
            return current["payment_status"] == "paid"
        if now_paid:
            cur = conn.execute(
                "UPDATE orders SET payment_status='paid',status='Confirmado' WHERE id=? AND payment_status='pending'",
                (order_id,),
            )
            changed = cur.rowcount == 1
        elif terminal_failure:
            cur = conn.execute(
                "UPDATE orders SET payment_status='failed',status='Cancelado' WHERE id=? AND payment_status='pending'",
                (order_id,),
            )
            if cur.rowcount == 1:
                for item in json.loads(current["items_json"] or "[]"):
                    if item.get("type") == "product":
                        conn.execute("UPDATE items SET stock=stock+? WHERE id=?",
                                     (int(item["quantity"]), int(item["id"])))
                changed = True
    if changed and now_paid:
        order_items = json.loads(row["items_json"] or "[]")
        send_order_email(
            row["customer_email"], f"Pago confirmado — pedido #{order_id}",
            "Tu pago fue aprobado. Ya estamos preparando tu compra.",
            order_id, row["customer_name"], order_items, expected_amount,
            row["payment_method"], "Confirmado", row["notes"],
        )
    return now_paid

def valid_mp_webhook_signature():
    secret = os.environ.get("FLOVA_MP_WEBHOOK_SECRET")
    signature = request.headers.get("x-signature", "")
    request_id = request.headers.get("x-request-id", "")
    data_id = str(request.args.get("data.id", "")).lower()
    parts = dict(piece.strip().split("=", 1) for piece in signature.split(",") if "=" in piece)
    if not secret or not parts.get("ts") or not parts.get("v1") or not request_id or not data_id:
        return False
    manifest = f"id:{data_id};request-id:{request_id};ts:{parts['ts']};"
    expected = hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])


def item_dict(row):
    d = dict(row)
    d["stock_status"] = "Agotado" if d["stock"] <= 0 else ("Última unidad" if d["stock"] == 1 else "Disponible")
    return d


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
    return render_template("checkout.html", mp_public_key=os.environ.get("FLOVA_MP_PUBLIC_KEY", "").strip())


@app.get("/api/items")
def get_items():
    kind = request.args.get("type")
    with db() as conn:
        if kind in ("product", "service"):
            rows = conn.execute("SELECT * FROM items WHERE active=1 AND type=? ORDER BY id DESC", (kind,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM items WHERE active=1 ORDER BY id DESC").fetchall()
    return jsonify([item_dict(r) for r in rows])


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
    for row in rows:
        if row["payment_status"] == "pending" and row["mp_order_id"]:
            try:
                reconcile_mp_order(row["id"])
            except Exception:
                app.logger.exception("No se pudo actualizar el pago pendiente del pedido %s", row["id"])
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
            d["payment_details"] = json.loads(d.pop("mp_payment_details", "{}") or "{}")
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
        cur = conn.execute("""INSERT INTO items(type,name,category,mp_category_id,price,description,image,stock,active)
            VALUES(?,?,?,?,?,?,?,?,1)""",
            (kind, name, str(data.get("category", "General")).strip() or "General",
             (str(data.get("mp_category_id", "")).strip() or ("electronics" if kind == "product" else "")), price,
             str(data.get("description", "")).strip(), image, stock))
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
        image = str(data.get("image", old["image"])).strip()
        uploaded = request.files.get("image_file") if request.files else None
        if uploaded and uploaded.filename and allowed_file(uploaded.filename):
            ext = uploaded.filename.rsplit(".", 1)[1].lower()
            filename = secure_filename(f"{uuid.uuid4().hex}.{ext}")
            uploaded.save(UPLOAD_DIR / filename)
            image = f"/static/uploads/{filename}"
        conn.execute("""UPDATE items SET type=?,name=?,category=?,mp_category_id=?,price=?,description=?,image=?,stock=?,active=?
            WHERE id=?""",
            (kind, name, str(data.get("category", old["category"])).strip() or "General",
             (str(data.get("mp_category_id", old["mp_category_id"])).strip() or ("electronics" if kind == "product" else "")),
             price, str(data.get("description", old["description"])).strip(), image, stock,
             int(data.get("active", old["active"])), item_id))
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return jsonify(item_dict(row))


@app.delete("/api/admin/items/<int:item_id>")
def delete_item(item_id):
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    with db() as conn:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    return jsonify({"ok": True})


def payment_details_from_order(remote):
    transactions = remote.get("transactions") or {}
    payments = transactions.get("payments") or []
    payment = payments[0] if payments and isinstance(payments[0], dict) else {}
    method = payment.get("payment_method") or {}
    allowed = ("id", "type", "ticket_url", "reference", "verification_code", "financial_institution")
    details = {key: method.get(key) for key in allowed if method.get(key)}
    details["status"] = payment.get("status") or remote.get("status") or ""
    details["status_detail"] = payment.get("status_detail") or remote.get("status_detail") or ""
    details["date_of_expiration"] = payment.get("date_of_expiration") or remote.get("date_of_expiration") or ""
    return details


@app.post("/api/orders")
def create_order():
    data = request.get_json(silent=True) or {}
    customer_id = session.get("customer_id")
    if not customer_id:
        return jsonify({"message": "Inicia sesión o crea una cuenta para realizar el pedido y poder seguir su estado desde tu panel."}), 401
    contact = str(data.get("customer_contact", "")).strip()
    address = str(data.get("address", "")).strip()
    notes = str(data.get("notes", "")).strip()
    cart = data.get("items", [])
    method_key = str(data.get("method", "")).strip().lower()
    methods = {
        "card": "Tarjeta (Mercado Pago)",
        "yape": "Yape",
        "pagoefectivo_atm": "PagoEfectivo",
    }
    if method_key not in methods:
        return jsonify({"message": "Elige tarjeta, Yape o PagoEfectivo."}), 400
    if not contact or not cart:
        return jsonify({"message": "Completa tu teléfono de contacto y agrega artículos al carrito."}), 400
    if not mercado_pago_is_configured():
        return jsonify({"message": "Los pagos todavía no están configurados. Contacta con FLOVA by Cada."}), 503

    with db() as conn:
        customer = conn.execute(
            "SELECT id,name,email,phone,document_type,document_number FROM customers WHERE id=?",
            (customer_id,),
        ).fetchone()
        if not customer:
            session.pop("customer_id", None)
            return jsonify({"message": "Tu sesión venció. Inicia sesión de nuevo."}), 401
        total = 0
        verified = []
        for line in cart:
            try:
                item_id = int(line.get("id"))
                qty = int(line.get("quantity", 1))
            except (ValueError, TypeError, AttributeError):
                return jsonify({"message": "Hay un artículo no válido en el carrito."}), 400
            if qty < 1:
                return jsonify({"message": "Cantidad no válida."}), 400
            item = conn.execute("SELECT * FROM items WHERE id=? AND active=1", (item_id,)).fetchone()
            if not item:
                return jsonify({"message": "Uno de los artículos ya no está disponible."}), 409
            if item["type"] != "product":
                return jsonify({"message": "Los servicios se coordinan por WhatsApp y no se agregan al pago del carrito."}), 400
            if item["stock"] < qty:
                return jsonify({"message": f"Stock insuficiente para {item['name']}."}), 409
            total += float(item["price"]) * qty
            verified.append({"id": item_id, "name": item["name"], "type": item["type"],
                             "category_id": item["mp_category_id"] or "electronics",
                             "price": float(item["price"]), "quantity": qty})
        conn.execute("UPDATE customers SET phone=? WHERE id=? AND (phone='' OR phone IS NULL)", (contact, customer_id))
        for line in verified:
            cur_stock = conn.execute(
                "UPDATE items SET stock=stock-? WHERE id=? AND stock>=?",
                (line["quantity"], line["id"], line["quantity"]),
            )
            if cur_stock.rowcount != 1:
                conn.rollback()
                return jsonify({"message": f"Stock insuficiente para {line['name']}."}), 409
        cur = conn.execute(
            """INSERT INTO orders(customer_name,customer_contact,address,notes,items_json,total,payment_method,
               customer_id,customer_email,payment_status)
               VALUES(?,?,?,?,?,?,?,?,?,'pending')""",
            (customer["name"], contact, address, notes, json_dumps(verified), total,
             methods[method_key], customer_id, customer["email"]),
        )
        order_id = cur.lastrowid

    try:
        name_parts = customer["name"].strip().split(maxsplit=1)
        payer = {"email": customer["email"], "first_name": name_parts[0] if name_parts else "Cliente"}
        if len(name_parts) > 1:
            payer["last_name"] = name_parts[1]
        identification = data.get("identification")
        if isinstance(identification, dict) and identification.get("type") and identification.get("number"):
            payer["identification"] = {
                "type": str(identification["type"])[:20],
                "number": str(identification["number"])[:30],
            }
        digits = "".join(ch for ch in contact if ch.isdigit())
        if digits.startswith("51") and len(digits) > 9:
            digits = digits[2:]
        phone_number = digits[-9:] if len(digits) >= 9 else digits
        yape_digits = "".join(ch for ch in str(data.get("payer_phone", "")) if ch.isdigit())
        if yape_digits.startswith("51") and len(yape_digits) > 9:
            yape_digits = yape_digits[2:]
        yape_phone_number = yape_digits[-9:] if len(yape_digits) >= 9 else yape_digits
        payment_method = {}

        if method_key == "card":
            token = str(data.get("token", "")).strip()
            payment_method_id = str(data.get("payment_method_id", "")).strip().lower()
            payment_type = str(data.get("payment_type", "")).strip().lower()
            try:
                installments = int(data.get("installments", 1))
            except (TypeError, ValueError):
                installments = 1
            if not token or not payment_method_id or payment_type not in {"credit_card", "debit_card", "prepaid_card"}:
                raise RuntimeError("Completa los datos de la tarjeta en el formulario seguro de Mercado Pago.")
            if installments < 1 or installments > 36:
                raise RuntimeError("El número de cuotas no es válido.")
            payment_method = {
                "id": payment_method_id, "type": payment_type, "token": token,
                "installments": installments,
            }
        elif method_key == "yape":
            token = str(data.get("token", "")).strip()
            if not token or len(yape_phone_number) != 9:
                raise RuntimeError("Ingresa el celular asociado a Yape y el código OTP vigente.")
            payer["entity_type"] = "individual"
            payer["phone"] = {"area_code": "51", "number": yape_phone_number}
            payment_method = {"id": "yape", "type": "debit_card", "token": token}
        else:
            payment_method = {"id": "pagoefectivo_atm", "type": "atm"}

        payment = {"amount": f"{total:.2f}", "payment_method": payment_method}
        if method_key == "pagoefectivo_atm":
            payment["expiration_time"] = "P1D"
        payload = {
            "type": "online",
            "processing_mode": "automatic",
            "capture_mode": "automatic_async",
            "total_amount": f"{total:.2f}",
            "external_reference": str(order_id),
            "description": ", ".join(f"{line['name']} x{line['quantity']}" for line in verified)[:150],
            "items": [
                {
                    "external_code": str(line["id"]),
                    "title": line["name"],
                    "description": line["name"],
                    **({"category_id": line["category_id"]} if line.get("category_id") else {}),
                    "quantity": int(line["quantity"]),
                    "unit_price": f"{float(line['price']):.2f}",
                }
                for line in verified
            ],
            "payer": payer,
            "transactions": {"payments": [payment]},
        }
        if method_key == "pagoefectivo_atm":
            payload["payer"]["entity_type"] = "individual"
        remote = mercado_pago_request(
            "/v1/orders", method="POST", idempotency_key=str(uuid.uuid4()), payload=payload,
        )
        mp_order_id = str(remote.get("id") or "")
        if not mp_order_id:
            raise RuntimeError("Mercado Pago no devolvió el número de la orden.")
        if method_key == "pagoefectivo_atm" and not payment_details_from_order(remote).get("reference"):
            for _ in range(3):
                time.sleep(0.5)
                remote = mercado_pago_request("/v1/orders/" + quote(mp_order_id, safe=""))
                if payment_details_from_order(remote).get("reference"):
                    break

        details = payment_details_from_order(remote)
        mp_status = str(remote.get("status") or "").lower()
        transaction = payment_transaction_from_order(remote)
        transaction_status = str(transaction.get("status") or "").lower()
        paid_amount = Decimal(str(transaction.get("paid_amount") or remote.get("total_paid_amount") or "0"))
        paid = mp_status == "processed" and transaction_status == "processed" and paid_amount >= Decimal(str(total))
        failed = mp_status in {"failed", "canceled", "cancelled", "expired"}
        local_payment_status = "paid" if paid else "failed" if failed else "pending"
        local_status = "Confirmado" if paid else "Cancelado" if failed else "Pendiente"
        with db() as conn:
            conn.execute(
                "UPDATE orders SET mp_order_id=?,mp_payment_details=?,payment_status=?,status=? WHERE id=?",
                (mp_order_id, json_dumps(details), local_payment_status, local_status, order_id),
            )
            if failed:
                for line in verified:
                    conn.execute("UPDATE items SET stock=stock+? WHERE id=?", (line["quantity"], line["id"]))
        if paid:
            send_order_email(
                customer["email"], f"Pago confirmado — pedido #{order_id}",
                "Tu pago fue aprobado. Ya estamos preparando tu compra.",
                order_id, customer["name"], verified, total, methods[method_key], "Confirmado", notes,
            )
        return jsonify({
            "ok": True, "order_id": order_id, "total": round(total, 2),
            "payment_status": local_payment_status, "status": local_status,
            "payment_method": methods[method_key], "payment_details": details,
            "result_url": f"/pago/resultado?order_id={order_id}",
        }), 201
    except Exception as exc:
        app.logger.exception("No se pudo crear el pago del pedido %s", order_id)
        with db() as conn:
            for line in verified:
                conn.execute("UPDATE items SET stock=stock+? WHERE id=?", (line["quantity"], line["id"]))
            conn.execute("DELETE FROM orders WHERE id=? AND payment_status='pending'", (order_id,))
        message = str(exc) if isinstance(exc, RuntimeError) else "No se pudo iniciar el pago. Inténtalo de nuevo."
        if isinstance(exc, MercadoPagoRateLimitError):
            return jsonify({"message": message, "retry_after": exc.retry_after}), 429
        return jsonify({"message": message}), 502

@app.get("/api/payments/mercadopago/methods")
def mercado_pago_payment_methods():
    if not mercado_pago_is_configured():
        return jsonify({"configured": False, "methods": []})
    try:
        remote = mercado_pago_request("/v1/payment_methods")
    except Exception:
        app.logger.exception("No se pudieron consultar los medios de pago de Mercado Pago")
        return jsonify({"configured": True, "methods": [],
                        "message": "No se pudieron cargar los medios de pago ahora."}), 502
    methods = remote if isinstance(remote, list) else []
    safe_methods = [{"id": str(method.get("id", "")), "name": str(method.get("name") or method.get("id") or ""),
                     "type": str(method.get("payment_type_id") or "")}
                    for method in methods if isinstance(method, dict) and method.get("status") == "active"]
    return jsonify({"configured": True, "methods": safe_methods})


@app.get("/pago/resultado")
def payment_result():
    try:
        order_id = int(request.args.get("order_id", "0"))
    except ValueError:
        order_id = 0
    customer_id = session.get("customer_id")
    if not customer_id:
        return redirect("/cuenta?next=%2Fcheckout")
    with db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=? AND customer_id=?",
                             (order_id, customer_id)).fetchone() if order_id else None
    if not order:
        return redirect("/cuenta")
    if order["payment_status"] == "pending" and order["mp_order_id"]:
        try:
            reconcile_mp_order(order_id)
        except Exception:
            app.logger.exception("No se pudo consultar el resultado del pago %s", order_id)
        with db() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id=? AND customer_id=?",
                                 (order_id, customer_id)).fetchone()
    result = dict(order)
    try:
        result["items"] = json.loads(result.pop("items_json") or "[]")
    except Exception:
        result["items"] = []
    try:
        result["payment_details"] = json.loads(result.pop("mp_payment_details", "{}") or "{}")
    except Exception:
        result["payment_details"] = {}
    return render_template("order_result.html", order=result)

@app.post("/api/payments/mercadopago/webhook")
def mercado_pago_webhook():
    if not valid_mp_webhook_signature():
        return "", 401
    mp_order_id = str(request.args.get("data.id", ""))
    with db() as conn:
        row = conn.execute("SELECT id FROM orders WHERE mp_order_id=?", (mp_order_id,)).fetchone()
    if not row:
        return "", 503
    try:
        reconcile_mp_order(row["id"], mp_order_id)
    except Exception:
        app.logger.exception("No se pudo reconciliar el webhook de Mercado Pago")
        return "", 503
    return "", 200


def json_dumps(value):
    import json
    return json.dumps(value, ensure_ascii=False)


@app.get("/api/admin/orders")
def get_orders():
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    with db() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200").fetchall()
    import json
    output = []
    for row in rows:
        d = dict(row)
        try:
            d["items"] = json.loads(d.pop("items_json"))
        except Exception:
            d["items"] = []
        try:
            d["payment_details"] = json.loads(d.pop("mp_payment_details", "{}") or "{}")
        except Exception:
            d["payment_details"] = {}
        output.append(d)
    return jsonify(output)


@app.post("/api/admin/orders/<int:order_id>/cancel-payment")
def cancel_mercado_pago_order(order_id):
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return jsonify({"message": "Pedido no encontrado."}), 404
    if row["payment_status"] != "pending" or not row["mp_order_id"]:
        return jsonify({"message": "Este pedido no tiene un pago pendiente cancelable en Mercado Pago."}), 409

    mp_order_id = row["mp_order_id"]
    try:
        current = mercado_pago_request("/v1/orders/" + quote(mp_order_id, safe=""))
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502
    if current.get("external_reference") != str(order_id):
        app.logger.error("Referencia externa no coincide al cancelar el pedido %s", order_id)
        return jsonify({"message": "No se pudo validar que la orden de Mercado Pago corresponda a este pedido."}), 409

    if current.get("status") not in {"created", "action_required"}:
        try:
            reconcile_mp_order(order_id, mp_order_id, current)
        except Exception:
            app.logger.exception("No se pudo sincronizar el pedido %s antes de cancelar", order_id)
        return jsonify({"message": "Mercado Pago solo permite cancelar órdenes creadas o que requieren una acción. Se actualizó el estado del pedido."}), 409

    try:
        canceled = mercado_pago_request(
            "/v1/orders/" + quote(mp_order_id, safe="") + "/cancel",
            method="POST", idempotency_key=str(uuid.uuid4()))
    except RuntimeError as exc:
        # A buyer may have completed payment between the status check and cancellation.
        try:
            fresh = mercado_pago_request("/v1/orders/" + quote(mp_order_id, safe=""))
            if fresh.get("external_reference") == str(order_id):
                reconcile_mp_order(order_id, mp_order_id, fresh)
        except Exception:
            app.logger.exception("No se pudo sincronizar el pedido %s tras fallar la cancelación", order_id)
        return jsonify({"message": str(exc)}), 409

    if (canceled.get("id") != mp_order_id or canceled.get("external_reference") != str(order_id)
            or canceled.get("status") not in {"canceled", "cancelled"}):
        app.logger.error("Respuesta inesperada al cancelar la orden Mercado Pago del pedido %s", order_id)
        return jsonify({"message": "Mercado Pago no confirmó la cancelación de la orden."}), 502
    try:
        reconcile_mp_order(order_id, mp_order_id, canceled)
    except Exception:
        app.logger.exception("No se pudo sincronizar el pedido FLOVA %s después de cancelar", order_id)
        return jsonify({"message": "Mercado Pago canceló la orden, pero FLOVA no pudo actualizar el pedido. Actualiza los pedidos para sincronizarlo."}), 502
    return jsonify({"ok": True, "message": "Pago cancelado en Mercado Pago y pedido actualizado."})


@app.post("/api/admin/orders/<int:order_id>/refund-payment")
def refund_mercado_pago_order(order_id):
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    data = request.get_json(silent=True) or {}
    amount_value = data.get("amount")
    try:
        amount = None if amount_value in (None, "") else Decimal(str(amount_value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return jsonify({"message": "Ingresa un monto válido para el reembolso."}), 400
    if amount is not None and amount <= Decimal("0"):
        return jsonify({"message": "El monto del reembolso debe ser mayor que cero."}), 400

    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return jsonify({"message": "Pedido no encontrado."}), 404
    if row["payment_status"] not in {"paid", "partially_refunded"} or not row["mp_order_id"]:
        return jsonify({"message": "Solo se pueden reembolsar pagos confirmados de Mercado Pago."}), 409

    mp_order_id = row["mp_order_id"]
    try:
        remote = mercado_pago_request("/v1/orders/" + quote(mp_order_id, safe=""))
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502
    if remote.get("external_reference") != str(order_id):
        app.logger.error("Referencia externa no coincide al reembolsar el pedido %s", order_id)
        return jsonify({"message": "No se pudo validar que la orden de Mercado Pago corresponda a este pedido."}), 409
    if remote.get("status") == "refunded":
        with db() as conn:
            conn.execute("UPDATE orders SET payment_status='refunded' WHERE id=? AND mp_order_id=?",
                         (order_id, mp_order_id))
        return jsonify({"ok": True, "payment_status": "refunded",
                        "message": "La orden ya estaba reembolsada; FLOVA actualizó el estado."})
    if remote.get("status") != "processed":
        return jsonify({"message": "Mercado Pago solo permite reembolsar órdenes procesadas."}), 409

    transactions = remote.get("transactions") or {}
    payments = transactions.get("payments", []) if isinstance(transactions, dict) else []
    refunds = transactions.get("refunds", []) if isinstance(transactions, dict) else []
    if not isinstance(payments, list):
        payments = []
    if not isinstance(refunds, list):
        refunds = []
    paid_total = Decimal(str(remote.get("total_paid_amount") or "0"))
    if paid_total <= 0:
        paid_total = sum((Decimal(str(p.get("paid_amount") or p.get("amount") or "0")) for p in payments), Decimal("0"))
    refunded_total = sum((Decimal(str(r.get("amount") or "0")) for r in refunds if r.get("status") == "processed"), Decimal("0"))
    remaining = (paid_total - refunded_total).quantize(Decimal("0.01"))
    if remaining <= Decimal("0"):
        return jsonify({"message": "Esta orden ya fue reembolsada por completo."}), 409
    requested_amount = remaining if amount is None else amount
    if requested_amount > remaining:
        return jsonify({"message": f"El monto supera el saldo reembolsable de S/ {remaining:.2f}."}), 400

    # A missing body requests a full refund. When prior partial refunds exist,
    # refund only the remaining balance against its payment transaction.
    refund_body = None
    if amount is not None or refunded_total > 0:
        processed_payments = [p for p in payments if p.get("status") == "processed" and p.get("id")]
        if len(processed_payments) != 1:
            return jsonify({"message": "Esta orden tiene varias transacciones; el reembolso parcial debe gestionarse desde Mercado Pago."}), 409
        payment = processed_payments[0]
        if not payment:
            return jsonify({"message": "Mercado Pago no devolvió una transacción pagada para procesar el reembolso."}), 409
        payment_paid = Decimal(str(payment.get("paid_amount") or payment.get("amount") or "0"))
        payment_refunded = sum((Decimal(str(r.get("amount") or "0")) for r in refunds
                                if r.get("status") == "processed" and r.get("transaction_id") == payment["id"]), Decimal("0"))
        payment_remaining = (payment_paid - payment_refunded).quantize(Decimal("0.01"))
        if requested_amount > payment_remaining:
            return jsonify({"message": f"El monto supera el saldo reembolsable de esta transacción: S/ {payment_remaining:.2f}."}), 400
        refund_body = {"transactions": [{"id": payment["id"], "amount": f"{requested_amount:.2f}"}]}
    try:
        result = mercado_pago_request(
            "/v1/orders/" + quote(mp_order_id, safe="") + "/refund",
            method="POST", idempotency_key=str(uuid.uuid4()), payload=refund_body)
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502

    result_transactions = result.get("transactions") or {}
    result_refunds = result_transactions.get("refunds", []) if isinstance(result_transactions, dict) else []
    if not isinstance(result_refunds, list):
        result_refunds = []
    response_refunded = sum((Decimal(str(r.get("amount") or "0")) for r in result_refunds if r.get("status") == "processed"), Decimal("0"))
    all_refunded = (result.get("status") == "refunded" or requested_amount >= remaining
                    or refunded_total + response_refunded >= paid_total)
    new_payment_status = "refunded" if all_refunded else "partially_refunded"
    if result.get("id") != mp_order_id or result.get("status") not in {"processed", "refunded"}:
        app.logger.error("Respuesta inesperada al reembolsar la orden Mercado Pago del pedido %s", order_id)
        return jsonify({"message": "Mercado Pago no confirmó el reembolso. Actualiza el estado antes de volver a intentarlo."}), 502
    with db() as conn:
        conn.execute("UPDATE orders SET payment_status=? WHERE id=? AND mp_order_id=?",
                     (new_payment_status, order_id, mp_order_id))
    refund_amount = paid_total - refunded_total if amount is None else requested_amount
    send_order_email(row["customer_email"], f"Reembolso procesado — pedido #{order_id}",
                     f"Mercado Pago procesó un reembolso de S/ {refund_amount:.2f}.", order_id,
                     row["customer_name"], json.loads(row["items_json"] or "[]"), row["total"],
                     "Mercado Pago", "Reembolsado" if all_refunded else "Reembolso parcial", row["notes"])
    return jsonify({"ok": True, "payment_status": new_payment_status,
                    "message": "Reembolso total procesado." if all_refunded else "Reembolso parcial procesado."})


@app.get("/api/admin/mercadopago/orders")
def search_mercado_pago_orders():
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401

    begin_date = request.args.get("begin_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    try:
        begin = datetime.fromisoformat(begin_date.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if begin.tzinfo is None or end.tzinfo is None or begin > end:
            raise ValueError
    except ValueError:
        return jsonify({"message": "Indica un rango de fechas válido con zona horaria."}), 400

    params = {"begin_date": begin_date, "end_date": end_date}
    reference = request.args.get("external_reference", "").strip()[:64]
    status = request.args.get("status", "").strip().lower()
    allowed_statuses = {"created", "processing", "processed", "action_required", "failed", "canceled", "refunded"}
    if status and status not in allowed_statuses:
        return jsonify({"message": "Estado de Mercado Pago no válido."}), 400
    if reference:
        params["external_reference"] = reference
    if status:
        params["status"] = status
    try:
        limit = max(1, min(50, int(request.args.get("limit", "10"))))
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        return jsonify({"message": "Paginación no válida."}), 400
    params.update({"limit": str(limit), "offset": str(offset)})

    try:
        remote = mercado_pago_request("/v1/orders?" + urlencode(params))
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502
    except Exception:
        app.logger.exception("No se pudo consultar la búsqueda de órdenes de Mercado Pago")
        return jsonify({"message": "No se pudo consultar Mercado Pago. Inténtalo nuevamente."}), 502

    # Return only operational fields; omit client tokens and 3DS/security URLs.
    safe_orders = []
    remote_orders = remote.get("data", [])
    if not isinstance(remote_orders, list):
        remote_orders = []
    for order in remote_orders:
        transactions = order.get("transactions") or {}
        payments = []
        remote_payments = transactions.get("payments", []) if isinstance(transactions, dict) else []
        if not isinstance(remote_payments, list):
            remote_payments = []
        for payment in remote_payments:
            method = payment.get("payment_method") or {}
            payments.append({
                "id": payment.get("id"), "amount": payment.get("amount"),
                "paid_amount": payment.get("paid_amount"), "status": payment.get("status"),
                "status_detail": payment.get("status_detail"),
                "payment_method": {"id": method.get("id"), "type": method.get("type"),
                                   "installments": method.get("installments")}
            })
        remote_chargebacks = transactions.get("chargebacks", []) if isinstance(transactions, dict) else []
        remote_refunds = transactions.get("refunds", []) if isinstance(transactions, dict) else []
        if not isinstance(remote_chargebacks, list):
            remote_chargebacks = []
        if not isinstance(remote_refunds, list):
            remote_refunds = []
        chargebacks = [{key: entry.get(key) for key in ("id", "transaction_id", "status")}
                       for entry in remote_chargebacks]
        refunds = [{key: entry.get(key) for key in ("id", "transaction_id", "amount", "status")}
                   for entry in remote_refunds]
        safe_orders.append({key: order.get(key) for key in (
            "id", "type", "processing_mode", "external_reference", "total_amount",
            "total_paid_amount", "status", "status_detail", "capture_mode", "currency",
            "created_date", "last_updated_date"
        )})
        safe_orders[-1]["transactions"] = {"payments": payments, "chargebacks": chargebacks, "refunds": refunds}
    return jsonify({"data": safe_orders, "paging": remote.get("paging", {})})


@app.get("/api/admin/mercadopago/chargebacks/<chargeback_id>")
def get_mercado_pago_chargeback(chargeback_id):
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    chargeback_id = chargeback_id.strip()
    if not (6 <= len(chargeback_id) <= 32 and chargeback_id.isdigit()):
        return jsonify({"message": "Indica un ID numérico de contracargo válido."}), 400
    caller_id = os.environ.get("FLOVA_MP_CALLER_ID", "").strip()
    if not caller_id.isdigit():
        return jsonify({"message": "Configura el ID de vendedor FLOVA_MP_CALLER_ID en el servidor."}), 503
    try:
        remote = mercado_pago_request("/v1/chargebacks/" + quote(chargeback_id, safe=""), caller_id=caller_id)
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502
    except Exception:
        app.logger.exception("No se pudo consultar el contracargo %s en Mercado Pago", chargeback_id)
        return jsonify({"message": "No se pudo consultar el contracargo. Inténtalo nuevamente."}), 502

    if not isinstance(remote, dict) or str(remote.get("id", "")) != chargeback_id:
        return jsonify({"message": "Mercado Pago no devolvió el contracargo solicitado."}), 502
    documentation = []
    remote_docs = remote.get("documentation", [])
    for entry in remote_docs if isinstance(remote_docs, list) else []:
        if not isinstance(entry, dict):
            continue
        doc_type, doc_uuid = entry.get("type"), entry.get("uuid")
        try:
            valid_uuid = str(uuid.UUID(str(doc_uuid))) == str(doc_uuid).lower()
        except (ValueError, AttributeError, TypeError):
            valid_uuid = False
        safe_url = (f"/api/admin/mercadopago/chargebacks/documentation/{quote(str(doc_type), safe='')}/{quote(str(doc_uuid), safe='')}"
                    if doc_type == "collector" and valid_uuid else None)
        safe_document = {key: entry.get(key) for key in ("type", "description", "uuid")}
        safe_document["url"] = safe_url
        documentation.append(safe_document)
    payments = remote.get("payments", [])
    if not isinstance(payments, list):
        payments = []
    safe_chargeback = {key: remote.get(key) for key in (
        "id", "currency", "amount", "reason", "reason_id", "coverage_applied",
        "coverage_eligible", "documentation_required", "documentation_status",
        "date_documentation_deadline", "date_created", "date_last_updated", "live_mode"
    )}
    safe_chargeback["payments"] = [str(payment.get("id")) for payment in payments
                                   if isinstance(payment, dict) and payment.get("id")]
    safe_chargeback["documentation"] = documentation
    return jsonify(safe_chargeback)


@app.get("/api/admin/mercadopago/chargebacks/documentation/<doc_type>/<doc_uuid>")
def view_mercado_pago_chargeback_document(doc_type, doc_uuid):
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    if doc_type != "collector":
        return jsonify({"message": "Tipo de documento no válido."}), 400
    try:
        if str(uuid.UUID(doc_uuid)) != doc_uuid.lower():
            raise ValueError
    except (ValueError, AttributeError):
        return jsonify({"message": "Identificador de documento no válido."}), 400
    caller_id = os.environ.get("FLOVA_MP_CALLER_ID", "").strip()
    if not caller_id.isdigit():
        return jsonify({"message": "Configura el ID de vendedor FLOVA_MP_CALLER_ID en el servidor."}), 503
    try:
        content, content_type = mercado_pago_get_chargeback_document(doc_type, doc_uuid, caller_id)
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502
    except Exception:
        app.logger.exception("No se pudo descargar documentación de contracargo %s", doc_uuid)
        return jsonify({"message": "No se pudo obtener el documento. Inténtalo nuevamente."}), 502
    extension = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}[content_type]
    return Response(content, mimetype=content_type, headers={
        "Content-Disposition": f'inline; filename="{doc_uuid}.{extension}"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"
    })


@app.post("/api/admin/mercadopago/chargebacks/<chargeback_id>/documentation")
def upload_mercado_pago_chargeback_documentation(chargeback_id):
    if not is_admin():
        return jsonify({"message": "No autorizado"}), 401
    chargeback_id = chargeback_id.strip()
    if not (6 <= len(chargeback_id) <= 32 and chargeback_id.isdigit()):
        return jsonify({"message": "Indica un ID numérico de contracargo válido."}), 400
    caller_id = os.environ.get("FLOVA_MP_CALLER_ID", "").strip()
    if not caller_id.isdigit():
        return jsonify({"message": "Configura el ID de vendedor FLOVA_MP_CALLER_ID en el servidor."}), 503
    uploads = request.files.getlist("file")
    if not uploads or len(uploads) > 5:
        return jsonify({"message": "Selecciona entre 1 y 5 archivos de evidencia."}), 400

    allowed_signatures = {
        "application/pdf": b"%PDF-",
        "image/jpeg": b"\xff\xd8\xff",
        "image/png": b"\x89PNG\r\n\x1a\n"
    }
    prepared, total_size = [], 0
    for upload in uploads:
        if not upload.filename:
            return jsonify({"message": "Todos los archivos seleccionados deben tener nombre."}), 400
        content_type = (upload.mimetype or "").lower()
        signature = allowed_signatures.get(content_type)
        content = upload.read(5 * 1024 * 1024 + 1)
        if not signature or not content.startswith(signature):
            return jsonify({"message": "Solo se aceptan documentos PDF, JPG o PNG válidos."}), 400
        if len(content) > 5 * 1024 * 1024:
            return jsonify({"message": "Cada archivo debe pesar como máximo 5 MB."}), 413
        total_size += len(content)
        prepared.append((upload.filename, content, content_type))
    if total_size > 5 * 1024 * 1024:
        return jsonify({"message": "El tamaño total de los archivos no debe superar 5 MB."}), 413

    try:
        chargeback = mercado_pago_request("/v1/chargebacks/" + quote(chargeback_id, safe=""), caller_id=caller_id)
        if not isinstance(chargeback, dict) or str(chargeback.get("id", "")) != chargeback_id:
            return jsonify({"message": "Mercado Pago no devolvió el contracargo solicitado."}), 502
        if chargeback.get("documentation_status") != "pending":
            return jsonify({"message": "Este caso no está pendiente de documentación y Mercado Pago no permite volver a enviarla."}), 409
        mercado_pago_upload_chargeback_documents(chargeback_id, prepared, caller_id)
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502
    except Exception:
        app.logger.exception("No se pudo subir documentación al contracargo %s", chargeback_id)
        return jsonify({"message": "No se pudo enviar la documentación. Revisa el estado del caso antes de reintentar."}), 502
    return jsonify({"ok": True, "message": "Evidencia enviada a Mercado Pago. Actualiza la consulta para ver el nuevo estado."})


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
        if status == "Cancelado" and old["payment_status"] == "pending" and old["mp_order_id"]:
            return jsonify({"message": "Cancela primero el pago pendiente desde el botón Cancelar pago para anularlo también en Mercado Pago."}), 409
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
