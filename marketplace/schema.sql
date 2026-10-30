PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('seller', 'admin')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'rejected')) DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    slug TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES users(id),
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    description TEXT NOT NULL,
    cover_path TEXT,
    pdf_path TEXT NOT NULL,
    preview_text TEXT,
    price INTEGER NOT NULL DEFAULT 0 CHECK (price = 0),
    published INTEGER NOT NULL DEFAULT 1,
    featured INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL COLLATE NOCASE,
    access_token_hash TEXT NOT NULL UNIQUE,
    token_expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'revoked')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id),
    price INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (order_id, book_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id),
    order_id INTEGER NOT NULL REFERENCES orders(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reading_progress (
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    last_page INTEGER NOT NULL DEFAULT 1 CHECK (last_page > 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (order_id, book_id)
);

CREATE INDEX IF NOT EXISTS idx_books_published_created ON books(published, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_books_category ON books(category_id);
CREATE INDEX IF NOT EXISTS idx_order_email ON orders(email);
CREATE INDEX IF NOT EXISTS idx_downloads_book ON downloads(book_id);
