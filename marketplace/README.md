# DISmart Books marketplace

This is a standalone FastAPI app with a marketplace-owned SQLite database and private PDF storage. It does not connect to the school management system's database. The visual theme follows the navy, white, and gold palette in the supplied DISmart School screenshot.

## Local setup

1. Create a Python virtual environment and install `requirements.txt`.
2. Set `MARKETPLACE_SECRET_KEY` to a long random value.
3. Set `MARKETPLACE_ADMIN_EMAIL` and `MARKETPLACE_ADMIN_PASSWORD` before the first run to create the initial administrator.
4. Configure SMTP using `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USER`, `SMTP_PASSWORD`, and optionally `SMTP_STARTTLS=1`.
5. Set `MARKETPLACE_BASE_URL` to the public HTTPS origin. Set `COOKIE_HTTPS_ONLY=1` behind HTTPS.
6. Run `uvicorn marketplace.main:app --host 127.0.0.1 --port 8000`.

The database is created at `marketplace/instance/marketplace.sqlite3` by default. `MARKETPLACE_DB` can point to a different SQLite file, and `MARKETPLACE_INSTANCE` controls the private upload directory. Back up the database and upload directory together. PDFs are kept outside the public static directory and are served only after checking a valid order link.

Guest library links are emailed, reusable for seven days, and replaced when the buyer requests a fresh link. Free orders are stored at a zero total; no payment provider is connected. Downloads are counted for best-seller ranking. Seller accounts require admin approval; active sellers publish immediately and can edit or remove only their own books.

## Current scope notes

- The launch uses single-book free orders; cart checkout and payment processing are not enabled.
- The reader embeds the PDF in the browser. “Save page” remembers a page number per order/book.
- Reviews are open to visitors and do not require a purchase.
- Email delivery requires SMTP configuration. Without it, orders are still recorded but access links cannot be delivered.
- Production deployment should use HTTPS, a strong secret, a persistent private volume for the SQLite database/uploads, database backups, and SMTP credentials stored in the hosting provider's secret manager.
