-- SQLite schema for FLOVA by Cada. app.py also applies safe migrations automatically.
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL CHECK(type IN ('product','service')),
  name TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'General', price REAL NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '', stock INTEGER NOT NULL DEFAULT 0,
  payment_link_url TEXT NOT NULL DEFAULT '', payment_link_order_number TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', document_type TEXT NOT NULL DEFAULT 'DNI',
  document_number TEXT NOT NULL DEFAULT '', email_verified INTEGER NOT NULL DEFAULT 1, last_login_at REAL,
  last_verified_at REAL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS customer_addresses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
  label TEXT NOT NULL DEFAULT 'Casa', recipient TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL, district TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',
  region TEXT NOT NULL DEFAULT '', reference TEXT NOT NULL DEFAULT '', is_default INTEGER NOT NULL DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS auth_codes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL COLLATE NOCASE, purpose TEXT NOT NULL,
  code_hash TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', expires_at REAL NOT NULL,
  created_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(email,purpose)
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT NOT NULL, customer_contact TEXT NOT NULL,
  address TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', items_json TEXT NOT NULL, total REAL NOT NULL,
  payment_method TEXT NOT NULL DEFAULT 'Por coordinar', status TEXT NOT NULL DEFAULT 'Pendiente',
  customer_id INTEGER, customer_email TEXT NOT NULL DEFAULT '', payment_status TEXT NOT NULL DEFAULT 'unpaid',
  payment_provider TEXT NOT NULL DEFAULT '', provider_reference TEXT NOT NULL DEFAULT '',
  provider_details TEXT NOT NULL DEFAULT '{}', stock_deducted INTEGER NOT NULL DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
