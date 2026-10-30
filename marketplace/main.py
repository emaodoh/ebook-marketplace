from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import smtplib
import sqlite3
import ssl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


ROOT = Path(__file__).resolve().parent
INSTANCE = Path(os.getenv("MARKETPLACE_INSTANCE", ROOT / "instance"))
UPLOADS = INSTANCE / "books"
DB_PATH = Path(os.getenv("MARKETPLACE_DB", INSTANCE / "marketplace.sqlite3"))
SCHEMA = ROOT / "schema.sql"
SECRET_KEY = os.getenv("MARKETPLACE_SECRET_KEY", "development-only-change-me")
BASE_URL = os.getenv("MARKETPLACE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
MAX_UPLOAD = int(os.getenv("MARKETPLACE_MAX_UPLOAD_BYTES", 30 * 1024 * 1024))
TOKEN_DAYS = int(os.getenv("MARKETPLACE_ACCESS_DAYS", "7"))

INSTANCE.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="DISmart Books", description="Independent school ebook marketplace")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=os.getenv("COOKIE_HTTPS_ONLY", "0") == "1", same_site="lax")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_database() -> None:
    with db() as connection:
        connection.executescript(SCHEMA.read_text())
        for label, slug in (("Teaching", "teaching"), ("Learning", "learning"), ("Fiction", "fiction"), ("Technology", "technology"), ("Reference", "reference")):
            connection.execute("INSERT OR IGNORE INTO categories(name,slug) VALUES(?,?)", (label, slug))
        admin_email = os.getenv("MARKETPLACE_ADMIN_EMAIL", "").strip().lower()
        admin_password = os.getenv("MARKETPLACE_ADMIN_PASSWORD", "")
        if admin_email and admin_password:
            exists = connection.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()
            if not exists:
                connection.execute(
                    "INSERT INTO users(email,password_hash,role,status) VALUES(?,?, 'admin','active')",
                    (admin_email, hash_password(admin_password)),
                )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 310_000).hex()
        return hmac.compare_digest(actual, digest_hex)
    except (ValueError, TypeError):
        return False


def current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with db() as connection:
        return connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def require_seller(request: Request):
    user = current_user(request)
    if not user or user["role"] != "seller" or user["status"] != "active":
        raise HTTPException(status_code=403, detail="An active seller account is required")
    return user


def require_admin(request: Request):
    user = current_user(request)
    if not user or user["role"] != "admin" or user["status"] != "active":
        raise HTTPException(status_code=403, detail="Administrator access is required")
    return user


def render(request: Request, template: str, **context):
    context.setdefault("user", current_user(request))
    return templates.TemplateResponse(request=request, name=template, context=context)


def send_access_email(recipient: str, token: str) -> bool:
    host = os.getenv("SMTP_HOST")
    sender = os.getenv("SMTP_FROM")
    if not host or not sender:
        return False
    message = EmailMessage()
    message["Subject"] = "Your DISmart Books library link"
    message["From"] = sender
    message["To"] = recipient
    link = f"{BASE_URL}/library/{quote(token)}"
    message.set_content(f"Your free ebook order is ready. Open this secure link to access your books (valid for {TOKEN_DAYS} days):\n\n{link}\n")
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if os.getenv("SMTP_STARTTLS", "1") == "1":
            smtp.starttls(context=ssl.create_default_context())
        username = os.getenv("SMTP_USER")
        if username:
            smtp.login(username, os.getenv("SMTP_PASSWORD", ""))
        smtp.send_message(message)
    return True


def access_order(token: str):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as connection:
        order = connection.execute(
            "SELECT * FROM orders WHERE access_token_hash=? AND status='completed'", (token_hash,)
        ).fetchone()
    if not order or datetime.fromisoformat(order["token_expires_at"]) < datetime.now(timezone.utc):
        return None
    return order


def save_upload(upload: UploadFile, allowed: set[str]) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    data = upload.file.read(MAX_UPLOAD + 1)
    if not data or len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=400, detail="File is empty or exceeds the upload limit")
    if suffix == ".pdf" and not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF")
    filename = f"{secrets.token_hex(24)}{suffix}"
    (UPLOADS / filename).write_bytes(data)
    return filename


@app.on_event("startup")
def startup():
    initialize_database()


@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = "", category: str = "", sort: str = "new"):
    safe_sort = "download_count DESC" if sort == "popular" else "b.created_at DESC"
    sql = f"""SELECT b.*, c.name AS category_name, u.email AS seller_email,
        (SELECT COUNT(*) FROM downloads d WHERE d.book_id=b.id) AS download_count,
        (SELECT ROUND(AVG(r.rating),1) FROM reviews r WHERE r.book_id=b.id) AS rating
        FROM books b LEFT JOIN categories c ON c.id=b.category_id JOIN users u ON u.id=b.seller_id
        WHERE b.published=1 AND (?='' OR b.title LIKE ? OR b.author LIKE ? OR b.description LIKE ?)
        AND (?='' OR c.slug=?) ORDER BY b.featured DESC, {safe_sort} LIMIT 100"""
    term = f"%{q.strip()}%"
    with db() as connection:
        books = connection.execute(sql, (q.strip(), term, term, term, category, category)).fetchall()
        categories = connection.execute("SELECT * FROM categories ORDER BY name").fetchall()
    return render(request, "home.html", books=books, categories=categories, q=q, selected_category=category, sort=sort)


@app.get("/books/{book_id}", response_class=HTMLResponse)
def book_detail(request: Request, book_id: int):
    with db() as connection:
        book = connection.execute("""SELECT b.*, c.name AS category_name, u.email AS seller_email,
            (SELECT ROUND(AVG(r.rating),1) FROM reviews r WHERE r.book_id=b.id) AS rating
            FROM books b LEFT JOIN categories c ON c.id=b.category_id JOIN users u ON u.id=b.seller_id
            WHERE b.id=? AND b.published=1""", (book_id,)).fetchone()
        if not book:
            raise HTTPException(404)
        reviews = connection.execute("SELECT * FROM reviews WHERE book_id=? ORDER BY created_at DESC", (book_id,)).fetchall()
        related = connection.execute("SELECT * FROM books WHERE published=1 AND category_id=? AND id<>? ORDER BY created_at DESC LIMIT 4", (book["category_id"], book_id)).fetchall()
    return render(request, "book.html", book=book, reviews=reviews, related=related)


@app.get("/covers/{filename}")
def cover(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(404)
    path = UPLOADS / filename
    if not path.is_file() or path.suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(404)
    return FileResponse(path)


@app.post("/books/{book_id}/purchase")
def purchase(book_id: int, email: Annotated[str, Form()]):
    email = email.strip().lower()
    if len(email) > 254 or "@" not in email:
        raise HTTPException(400, "Enter a valid email address")
    token = secrets.token_urlsafe(32)
    expiry = datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS)
    with db() as connection:
        book = connection.execute("SELECT id FROM books WHERE id=? AND published=1", (book_id,)).fetchone()
        if not book:
            raise HTTPException(404)
        cursor = connection.execute("INSERT INTO orders(email,access_token_hash,token_expires_at) VALUES(?,?,?)", (email, hashlib.sha256(token.encode()).hexdigest(), expiry.isoformat()))
        connection.execute("INSERT INTO order_items(order_id,book_id,price) VALUES(?,?,0)", (cursor.lastrowid, book_id))
    sent = False
    try:
        sent = send_access_email(email, token)
    except (OSError, smtplib.SMTPException):
        sent = False
    return RedirectResponse("/purchase/confirmation?sent=" + ("1" if sent else "0"), status_code=303)


@app.post("/cart/add/{book_id}")
def add_to_cart(request: Request, book_id: int):
    with db() as connection:
        exists = connection.execute("SELECT id FROM books WHERE id=? AND published=1", (book_id,)).fetchone()
    if not exists:
        raise HTTPException(404)
    cart = request.session.get("cart", [])
    if book_id not in cart:
        cart.append(book_id)
    request.session["cart"] = cart[:50]
    return RedirectResponse("/cart", status_code=303)


@app.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request):
    ids = request.session.get("cart", [])
    books = []
    if ids:
        marks = ",".join("?" for _ in ids)
        with db() as connection:
            books = connection.execute(f"SELECT id,title,author,cover_path FROM books WHERE published=1 AND id IN ({marks})", ids).fetchall()
    return render(request, "cart.html", books=books)


@app.post("/cart/checkout")
def cart_checkout(request: Request, email: Annotated[str, Form()]):
    email = email.strip().lower()
    ids = request.session.get("cart", [])
    if "@" not in email or len(email) > 254 or not ids:
        raise HTTPException(400, "Enter a valid email and add at least one book")
    token = secrets.token_urlsafe(32)
    expiry = datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS)
    with db() as connection:
        marks = ",".join("?" for _ in ids)
        books = connection.execute(f"SELECT id FROM books WHERE published=1 AND id IN ({marks})", ids).fetchall()
        if not books:
            raise HTTPException(400, "Your cart is empty")
        order = connection.execute("INSERT INTO orders(email,access_token_hash,token_expires_at) VALUES(?,?,?)", (email, hashlib.sha256(token.encode()).hexdigest(), expiry.isoformat()))
        connection.executemany("INSERT INTO order_items(order_id,book_id,price) VALUES(?,?,0)", [(order.lastrowid, book["id"]) for book in books])
    request.session["cart"] = []
    try:
        sent = send_access_email(email, token)
    except (OSError, smtplib.SMTPException):
        sent = False
    return RedirectResponse("/purchase/confirmation?sent=" + ("1" if sent else "0"), status_code=303)


@app.get("/purchase/confirmation", response_class=HTMLResponse)
def confirmation(request: Request, sent: bool = Query(False)):
    return render(request, "confirmation.html", sent=sent)


@app.post("/library/send-link")
def resend_library_link(email: Annotated[str, Form()]):
    email = email.strip().lower()
    if "@" in email and len(email) <= 254:
        with db() as connection:
            orders = connection.execute("SELECT id FROM orders WHERE email=? AND status='completed'", (email,)).fetchall()
            for order in orders:
                token = secrets.token_urlsafe(32)
                expiry = datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS)
                connection.execute("UPDATE orders SET access_token_hash=?,token_expires_at=? WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(), expiry.isoformat(), order["id"]))
                try:
                    send_access_email(email, token)
                except (OSError, smtplib.SMTPException):
                    pass
    return RedirectResponse("/purchase/confirmation?sent=1", status_code=303)


@app.get("/library/{token}", response_class=HTMLResponse)
def library(request: Request, token: str):
    order = access_order(token)
    if not order:
        raise HTTPException(403, "This library link is invalid or has expired")
    with db() as connection:
        books = connection.execute("""SELECT b.*, oi.order_id, COALESCE(p.last_page,1) AS last_page FROM books b
            JOIN order_items oi ON oi.book_id=b.id LEFT JOIN reading_progress p
            ON p.order_id=oi.order_id AND p.book_id=b.id
            WHERE oi.order_id=?""", (order["id"],)).fetchall()
    return render(request, "library.html", books=books, token=token, email=order["email"], expiry=order["token_expires_at"])


@app.get("/library/{token}/read/{book_id}", response_class=HTMLResponse)
def read_book(request: Request, token: str, book_id: int, page: int = 1):
    order = access_order(token)
    if not order:
        raise HTTPException(403)
    with db() as connection:
        owned = connection.execute("SELECT b.title,b.pdf_path FROM books b JOIN order_items i ON i.book_id=b.id WHERE i.order_id=? AND b.id=?", (order["id"], book_id)).fetchone()
    if not owned:
        raise HTTPException(403)
    page = max(1, min(page, 10000))
    with db() as connection:
        connection.execute("INSERT INTO reading_progress(order_id,book_id,last_page,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(order_id,book_id) DO UPDATE SET last_page=excluded.last_page,updated_at=CURRENT_TIMESTAMP", (order["id"], book_id, page))
    return render(request, "reader.html", title=owned["title"], token=token, book_id=book_id, page=page)


@app.get("/library/{token}/read/{book_id}/file")
def read_pdf(token: str, book_id: int):
    order = access_order(token)
    if not order:
        raise HTTPException(403)
    with db() as connection:
        owned = connection.execute("SELECT b.title,b.pdf_path FROM books b JOIN order_items i ON i.book_id=b.id WHERE i.order_id=? AND b.id=?", (order["id"], book_id)).fetchone()
    if not owned:
        raise HTTPException(403)
    path = UPLOADS / owned["pdf_path"]
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf", headers={"Content-Disposition": "inline"})


@app.get("/library/{token}/download/{book_id}")
def download(token: str, book_id: int):
    order = access_order(token)
    if not order:
        raise HTTPException(403)
    with db() as connection:
        owned = connection.execute("SELECT b.title,b.pdf_path FROM books b JOIN order_items i ON i.book_id=b.id WHERE i.order_id=? AND b.id=?", (order["id"], book_id)).fetchone()
        if not owned:
            raise HTTPException(403)
        connection.execute("INSERT INTO downloads(book_id,order_id) VALUES(?,?)", (book_id, order["id"]))
    path = UPLOADS / owned["pdf_path"]
    if not path.is_file():
        raise HTTPException(404)
    safe_name = "".join(c for c in owned["title"] if c.isalnum() or c in " -_").strip() or "ebook"
    return FileResponse(path, media_type="application/pdf", filename=f"{safe_name}.pdf")


@app.post("/books/{book_id}/reviews")
def add_review(book_id: int, name: Annotated[str, Form()], rating: Annotated[int, Form()], body: Annotated[str, Form()]):
    if not 1 <= rating <= 5 or not name.strip() or not body.strip():
        raise HTTPException(400, "Name, review, and a rating from 1 to 5 are required")
    with db() as connection:
        exists = connection.execute("SELECT id FROM books WHERE id=? AND published=1", (book_id,)).fetchone()
        if not exists:
            raise HTTPException(404)
        connection.execute("INSERT INTO reviews(book_id,name,rating,body) VALUES(?,?,?,?)", (book_id, name.strip()[:100], rating, body.strip()[:4000]))
    return RedirectResponse(f"/books/{book_id}", status_code=303)


@app.get("/seller/register", response_class=HTMLResponse)
def seller_register_page(request: Request):
    return render(request, "seller_register.html", error="")


@app.post("/seller/register")
def seller_register(email: Annotated[str, Form()], password: Annotated[str, Form()]):
    email = email.strip().lower()
    if "@" not in email or len(password) < 10:
        return RedirectResponse("/seller/register?error=Use+a+valid+email+and+password+with+at+least+10+characters", status_code=303)
    try:
        with db() as connection:
            connection.execute("INSERT INTO users(email,password_hash,role,status) VALUES(?,?,'seller','pending')", (email, hash_password(password)))
    except sqlite3.IntegrityError:
        return RedirectResponse("/seller/register?error=That+email+is+already+registered", status_code=303)
    return RedirectResponse("/seller/login?pending=1", status_code=303)


@app.get("/seller/login", response_class=HTMLResponse)
def seller_login_page(request: Request, error: str = "", pending: bool = False):
    return render(request, "login.html", error=error, pending=pending)


@app.post("/seller/login")
def seller_login(request: Request, email: Annotated[str, Form()], password: Annotated[str, Form()]):
    with db() as connection:
        user = connection.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        return RedirectResponse("/seller/login?error=Invalid+email+or+password", status_code=303)
    if user["role"] == "seller" and user["status"] != "active":
        return RedirectResponse("/seller/login?error=Your+seller+account+is+awaiting+admin+approval", status_code=303)
    request.session.clear()
    request.session["user_id"] = user["id"]
    return RedirectResponse("/admin" if user["role"] == "admin" else "/seller", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/seller", response_class=HTMLResponse)
def seller_dashboard(request: Request):
    seller = require_seller(request)
    with db() as connection:
        books = connection.execute("""SELECT b.*,(SELECT COUNT(*) FROM downloads d WHERE d.book_id=b.id) downloads,
            (SELECT COUNT(*) FROM order_items i WHERE i.book_id=b.id) orders,
            (SELECT COALESCE(SUM(i.price),0) FROM order_items i WHERE i.book_id=b.id) revenue
            FROM books b WHERE b.seller_id=? ORDER BY b.created_at DESC""", (seller["id"],)).fetchall()
        categories = connection.execute("SELECT * FROM categories ORDER BY name").fetchall()
        reviews = connection.execute("SELECT r.*,b.title FROM reviews r JOIN books b ON b.id=r.book_id WHERE b.seller_id=? ORDER BY r.created_at DESC LIMIT 10", (seller["id"],)).fetchall()
    return render(request, "seller.html", books=books, categories=categories, reviews=reviews)


@app.post("/seller/books")
def create_book(request: Request, title: Annotated[str, Form()], author: Annotated[str, Form()], description: Annotated[str, Form()], category_id: Annotated[int, Form()], pdf: UploadFile = File(...), cover_file: UploadFile | None = File(None)):
    seller = require_seller(request)
    pdf_name = save_upload(pdf, {".pdf"})
    cover_name = save_upload(cover_file, {".png", ".jpg", ".jpeg", ".webp"}) if cover_file and cover_file.filename else None
    with db() as connection:
        category = connection.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone()
        if not category:
            (UPLOADS / pdf_name).unlink(missing_ok=True)
            raise HTTPException(400, "Choose a valid category")
        connection.execute("INSERT INTO books(seller_id,category_id,title,author,description,pdf_path,cover_path,published) VALUES(?,?,?,?,?,?,?,1)", (seller["id"], category_id, title.strip()[:200], author.strip()[:200], description.strip()[:12000], pdf_name, cover_name))
    return RedirectResponse("/seller", status_code=303)


@app.post("/seller/books/{book_id}/delete")
def delete_book(request: Request, book_id: int):
    seller = require_seller(request)
    with db() as connection:
        book = connection.execute("SELECT pdf_path,cover_path,EXISTS(SELECT 1 FROM order_items WHERE book_id=books.id) AS purchased FROM books WHERE id=? AND seller_id=?", (book_id, seller["id"])).fetchone()
        if not book:
            raise HTTPException(404)
        if book["purchased"]:
            connection.execute("UPDATE books SET published=0 WHERE id=? AND seller_id=?", (book_id, seller["id"]))
        else:
            connection.execute("DELETE FROM books WHERE id=? AND seller_id=?", (book_id, seller["id"]))
            for name in (book["pdf_path"], book["cover_path"]):
                if name:
                    (UPLOADS / name).unlink(missing_ok=True)
    return RedirectResponse("/seller", status_code=303)


@app.post("/seller/books/{book_id}/edit")
def edit_book(request: Request, book_id: int, title: Annotated[str, Form()], author: Annotated[str, Form()], description: Annotated[str, Form()], category_id: Annotated[int, Form()]):
    seller = require_seller(request)
    with db() as connection:
        changed = connection.execute("UPDATE books SET title=?,author=?,description=?,category_id=? WHERE id=? AND seller_id=? AND EXISTS(SELECT 1 FROM categories WHERE id=?)", (title.strip()[:200], author.strip()[:200], description.strip()[:12000], category_id, book_id, seller["id"], category_id)).rowcount
        if not changed:
            raise HTTPException(404, "Book or category not found")
    return RedirectResponse("/seller", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    require_admin(request)
    with db() as connection:
        pending = connection.execute("SELECT id,email,created_at FROM users WHERE role='seller' AND status='pending' ORDER BY created_at").fetchall()
        sellers = connection.execute("SELECT id,email,status,created_at FROM users WHERE role='seller' ORDER BY created_at DESC").fetchall()
        categories = connection.execute("SELECT * FROM categories ORDER BY name").fetchall()
        books = connection.execute("SELECT id,title,featured,published FROM books ORDER BY created_at DESC").fetchall()
        stats = connection.execute("SELECT (SELECT COUNT(*) FROM books) books,(SELECT COUNT(*) FROM orders) orders,(SELECT COUNT(*) FROM downloads) downloads,(SELECT COUNT(*) FROM users WHERE role='seller' AND status='active') sellers").fetchone()
    return render(request, "admin.html", pending=pending, sellers=sellers, categories=categories, books=books, stats=stats)


@app.post("/admin/sellers/{seller_id}/{action}")
def seller_action(request: Request, seller_id: int, action: str):
    require_admin(request)
    status = {"approve": "active", "reject": "rejected", "suspend": "pending"}.get(action)
    if not status:
        raise HTTPException(404)
    with db() as connection:
        connection.execute("UPDATE users SET status=? WHERE id=? AND role='seller'", (status, seller_id))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/categories")
def add_category(request: Request, name: Annotated[str, Form()]):
    require_admin(request)
    label = name.strip()[:100]
    if not label:
        raise HTTPException(400)
    slug = "".join(c.lower() if c.isalnum() else "-" for c in label).strip("-")
    with db() as connection:
        connection.execute("INSERT OR IGNORE INTO categories(name,slug) VALUES(?,?)", (label, slug))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/books/{book_id}/feature")
def feature_book(request: Request, book_id: int):
    require_admin(request)
    with db() as connection:
        connection.execute("UPDATE books SET featured=CASE featured WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (book_id,))
    return RedirectResponse("/admin", status_code=303)


initialize_database()
