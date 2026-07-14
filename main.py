"""
Syndris Backend v3.0 — FastAPI + IMAP + Claude AI + Supabase
Pełna klasyfikacja emaili: faktury, zapytania, zamówienia, płatności
"""
import imaplib, email, base64, os, json, re, time, asyncio, html
from urllib.parse import quote
from email.header import decode_header
from datetime import datetime, timedelta
from typing import Optional

import anthropic, httpx, resend
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding

app = FastAPI(title="Syndris API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_SECRET = os.environ.get("SUPABASE_SECRET_KEY", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY", "")
resend.api_key  = RESEND_API_KEY

# Klucz "anon"/publiczny z Supabase (Dashboard → Settings → API) — w odroznieniu od
# SUPABASE_SECRET_KEY jest bezpieczny do ujawnienia w przegladarce klienta. Uzywany
# tylko do przekierowania na hostowany OAuth Supabase (logowanie przez Microsoft/Azure).
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

# Klucz do szyfrowania tokenow KSeF klientow zanim trafia do Supabase (Fernet, symetryczny).
# Wygeneruj lokalnie: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# i ustaw jako zmienna srodowiskowa KSEF_ENCRYPTION_KEY na Render.
KSEF_ENCRYPTION_KEY = os.environ.get("KSEF_ENCRYPTION_KEY", "")
_ksef_fernet = Fernet(KSEF_ENCRYPTION_KEY.encode()) if KSEF_ENCRYPTION_KEY else None

# Potwierdzone w oficjalnej dokumentacji CIRFMF/ksef-api (github.com/CIRFMF/ksef-api).
# Adres produkcyjny wywnioskowany przez analogie do testowego (ten sam wzorzec domeny) —
# zweryfikuj przed pierwszym uzyciem env="prod".
KSEF_BASE_URLS = {
    "test": "https://api-test.ksef.mf.gov.pl/v2",
    "prod": "https://api.ksef.mf.gov.pl/v2",
}

# ── SUPABASE ──
def sb_headers():
    return {
        "apikey": SUPABASE_SECRET,
        "Authorization": f"Bearer {SUPABASE_SECRET}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

async def sb_insert(table: str, data: dict):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=sb_headers(), json=data, timeout=15)
    print(f"[DB] {table} → {r.status_code}: {r.text[:200]}")
    return r

async def sb_select(table: str, filters: str = ""):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}&order=created_at.desc&limit=1000"
    headers = {**sb_headers(), "Range-Unit": "items", "Range": "0-999"}
    async with httpx.AsyncClient() as c:
        r = await c.get(url, headers=headers, timeout=15)
    return r.json() if r.status_code in (200, 206) else []

async def sb_exists(table: str, client_email: str, message_id: str) -> bool:
    """Sprawdza czy rekord już istnieje w bazie — z poprawnym kodowaniem URL."""
    params = {
        "client_email": f"eq.{client_email}",
        "message_id": f"eq.{message_id}",
        "limit": "1",
    }
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**sb_headers(), "Prefer": "count=exact"}
    async with httpx.AsyncClient() as c:
        r = await c.get(url, headers=headers, params=params, timeout=10)
    try:
        count = int(r.headers.get("content-range", "0/0").split("/")[-1])
        return count > 0
    except:
        data = r.json() if r.status_code == 200 else []
        return isinstance(data, list) and len(data) > 0

async def sb_patch(table: str, filters: str, data: dict):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    async with httpx.AsyncClient() as c:
        r = await c.patch(url, headers=sb_headers(), json=data, timeout=15)
    return r

async def sb_delete(table: str, filters: str):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    async with httpx.AsyncClient() as c:
        r = await c.delete(url, headers=sb_headers(), timeout=15)
    return r

async def sb_upsert(table: str, data: dict, on_conflict: str):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**sb_headers(), "Prefer": f"resolution=merge-duplicates,return=representation"}
    async with httpx.AsyncClient() as c:
        r = await c.post(url + f"?on_conflict={on_conflict}",
                         headers=headers, json=data, timeout=15)
    return r

# ── MODELE ──
class ImapConfig(BaseModel):
    host: str
    port: int = 993
    use_ssl: bool = True
    username: str
    password: str
    folder: str = "INBOX"
    days_back: float = 1

class ScanRequest(BaseModel):
    imap: ImapConfig
    auto_save_documents: bool = False

class ChatRequest(BaseModel):
    question: str
    client_email: Optional[str] = ""
    invoices: list = []

class ReplyRequest(BaseModel):
    imap: Optional[ImapConfig] = None
    to: str
    subject: str
    body: str
    in_reply_to: Optional[str] = ""
    from_email: Optional[str] = ""
    reply_to: Optional[str] = ""

class FollowUpRequest(BaseModel):
    imap: ImapConfig
    days_without_reply: int = 3

class KsefConnectRequest(BaseModel):
    client_email: str
    nip: str
    ksef_token: str
    env: str = "test"

class RegisterRequest(BaseModel):
    email: str
    password: str
    plan: str = "starter"

class LoginRequest(BaseModel):
    email: str
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str

class SyncUserRequest(BaseModel):
    plan: str = "starter"

# Maksymalny rozmiar pojedynczego zalacznika zapisywanego w bazie jako dokument
DOCUMENT_MAX_SIZE = 15 * 1024 * 1024  # 15 MB

# ── KATEGORIE EMAILI ──
EMAIL_CATEGORIES = {
    "faktura":    {"label": "Faktura",    "color": "#c9a84c", "icon": "🧾"},
    "reklamacja": {"label": "Reklamacja", "color": "#e05252", "icon": "⚠️"},
    "zapytanie":  {"label": "Zapytanie",  "color": "#4a8fe8", "icon": "❓"},
    "zamowienie": {"label": "Zamówienie", "color": "#2eb87a", "icon": "📦"},
    "spam":       {"label": "Spam",       "color": "#5a5752", "icon": "🗑️"},
    "inne":       {"label": "Inne",       "color": "#5a5752", "icon": "📧"},
}

# ── POZIOMY PILNOŚCI (niezależne od kategorii) ──
PRIORITY_LEVELS = {
    "pilne":          {"label": "Pilne",          "color": "#e05252"},
    "wazne":          {"label": "Ważne",          "color": "#e8c74a"},
    "moze_poczekac":  {"label": "Może poczekać",  "color": "#2eb87a"},
}

# ── ENDPOINTS ──
@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "version": "3.0.0",
            "supabase": "ok" if SUPABASE_URL else "NOT SET"}

@app.post("/api/imap/test")
async def test_imap(config: ImapConfig, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    try:
        mail = imaplib.IMAP4_SSL(config.host, config.port) if config.use_ssl \
               else imaplib.IMAP4(config.host, config.port)
        mail.login(config.username, config.password)
        # Pobierz listę folderów
        _, folders_raw = mail.list()
        folders = []
        for f in folders_raw:
            try:
                parts = f.decode().split('"/"')
                if parts:
                    folders.append(parts[-1].strip().strip('"'))
            except: pass
        mail.logout()
        return {"success": True, "message": f"Połączenie udane.",
                "email": config.username, "folders": folders[:10]}
    except imaplib.IMAP4.error as e:
        raise HTTPException(status_code=401, detail=f"Błąd logowania: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Błąd: {str(e)}")

@app.post("/api/scan")
async def scan_mailbox(req: ScanRequest, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    config = req.imap
    results = {"faktury": [], "reklamacje": [], "zapytania": [],
               "zamowienia": [], "spam": [], "inne": []}
    errors = []

    print(f"[SCAN] Start: {config.username}")

    try:
        mail = imaplib.IMAP4_SSL(config.host, config.port) if config.use_ssl \
               else imaplib.IMAP4(config.host, config.port)
        mail.login(config.username, config.password)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Błąd IMAP: {str(e)}")

    try:
        mail.select(config.folder)
        # Obsługa ułamkowych dni (np. 0.042 = 1 godzina, 1 = 24 godziny)
        delta = timedelta(hours=config.days_back * 24)
        since_dt = datetime.now() - delta
        since = since_dt.strftime("%d-%b-%Y")
        print(f"[SCAN] Skanowanie od: {since} (cofnięcie: {config.days_back} dni = {config.days_back*24:.1f}h)")
        _, ids_raw = mail.search(None, f'(SINCE "{since}")')
        ids = ids_raw[0].split()[-300:][::-1]  # max 300, od najnowszych
        print(f"[SCAN] Emaili do analizy: {len(ids)}")

        claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        skipped_duplicates = 0
        digest_emails = []

        for msg_id in ids:
            try:
                _, idate_data = mail.fetch(msg_id, "(INTERNALDATE)")
                idate_raw = idate_data[0].decode() if idate_data and idate_data[0] else ""
                m = re.search(r'INTERNALDATE "([^"]+)"', idate_raw)
                internal_date = _to_date(m.group(1)) if m else None

                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = _decode_hdr(msg.get("Subject", ""))
                sender  = msg.get("From", "")
                date    = internal_date or _to_date(msg.get("Date", ""))
                body    = _get_body(msg)
                atts    = _get_attachments(msg)

                # Pobierz Message-ID — unikalny identyfikator emaila
                message_id = _clean_message_id(msg.get("Message-ID", "") or msg.get("Message-Id", ""))
                if not message_id:
                    # Fallback: hash z tematu + daty + nadawcy
                    import hashlib
                    raw = f"{sender}{subject}{date}"
                    message_id = "hash-" + hashlib.md5(raw.encode()).hexdigest()

                # SPRAWDŹ DUPLIKAT przed wywołaniem AI (oszczędność kosztów)
                already_exists = await sb_exists("emails", config.username, message_id)
                if already_exists:
                    print(f"[SCAN] Pominięto duplikat: {subject[:50]}")
                    skipped_duplicates += 1
                    continue

                print(f"[SCAN] Klasyfikuję: {subject[:60]} [{message_id[:30]}]")
                sender_clean = sender[:200]

                # KLASYFIKACJA przez Claude
                classification = _classify_email(
                    claude, subject, sender, body, atts, date
                )
                category = classification.get("category", "inne")
                print(f"[SCAN] → {category}: {subject[:40]}")

                # Zbuduj rekord emaila
                email_record = {
                    "client_email":   config.username,
                    "message_id":     message_id[:500],
                    "category":       category,
                    "sender":         str(sender)[:255],
                    "subject":        str(subject)[:500],
                    "date":           _to_date(date),
                    "body":           str(body)[:10000],
                    "summary":        str(classification.get("summary", ""))[:1000],
                    "priority":       str(classification.get("priority", "moze_poczekac"))[:20],
                    "action_needed":  bool(classification.get("action_needed", False)),
                    "action_desc":    str(classification.get("action_desc", ""))[:500],
                    "reply_approve":  str(classification.get("reply_approve") or "")[:3000] or None,
                    "reply_reject":   str(classification.get("reply_reject") or "")[:3000] or None,
                    "has_attachment": len(atts) > 0,
                    "status":         "nowe",
                }

                # Zapisz email do bazy
                email_result = await sb_insert("emails", email_record)

                # KRYTYCZNE: jeśli email już istnieje (409 conflict z unique index),
                # to NIE przetwarzaj go dalej — to duplikat który prześlizgnął się
                # przez wcześniejsze sprawdzenie (np. race condition lub cache)
                if email_result.status_code == 409:
                    print(f"[SCAN] Email odrzucony jako duplikat przez baze (409): {subject[:50]}")
                    skipped_duplicates += 1
                    continue
                elif email_result.status_code not in (200, 201):
                    print(f"[SCAN] Nieoczekiwany blad zapisu emaila ({email_result.status_code}): {subject[:50]}")
                    errors.append(f"Email nie zapisany: {subject[:50]}")
                    continue

                digest_emails.append(email_record)

                # Zapisz remindery wykryte przez AI
                reminders_raw = classification.get("reminders") or []
                email_date_str = _to_date(date) or ""
                if isinstance(reminders_raw, list):
                    for rem in reminders_raw[:5]:
                        if not isinstance(rem, dict): continue
                        rem_date = _to_date(rem.get("date"))
                        if not rem_date: continue
                        # Zabezpieczenie przed halucynacja roku przez AI (np. 2024 zamiast 2026):
                        # termin/deadline nie moze byc wczesniejszy niz sam email, w ktorym go znaleziono
                        if email_date_str and rem_date[:10] < email_date_str[:10]:
                            print(f"[SCAN] Pominieto reminder z nieprawdopodobna data z przeszlosci: "
                                  f"{rem_date} (mail z {email_date_str[:10]}) — {subject[:50]}")
                            continue
                        await sb_insert("reminders", {
                            "client_email":  config.username,
                            "subject":       str(subject)[:500],
                            "reminder_date": rem_date,
                            "description":   str(rem.get("description", ""))[:500],
                            "type":          str(rem.get("type", "inne"))[:50],
                            "status":        "aktywny",
                        })

                # Zapisz automatycznie wszystkie zalaczniki (PDF/obrazy) jako dokumenty —
                # tylko gdy uzytkownik wlaczyl przelacznik "Automatyczne pobieranie zalacznikow"
                # w zakladce Harmonogram (auto_save_documents)
                if req.auto_save_documents:
                    for att in atts:
                        if len(att["content"]) > DOCUMENT_MAX_SIZE:
                            print(f"[SCAN] Pominięto dokument (za duży): {att['filename']}")
                            continue
                        doc_result = await sb_insert("documents", {
                            "client_email": config.username,
                            "message_id":   message_id[:500],
                            "filename":     str(att["filename"])[:255],
                            "content_type": att["content_type"],
                            "data":         base64.b64encode(att["content"]).decode("utf-8"),
                            "size":         len(att["content"]),
                            "subject":      str(subject)[:500],
                            "sender":       str(sender)[:255],
                            "date":         _to_date(date),
                            "category":     category,
                        })
                        if doc_result.status_code not in (200, 201):
                            print(f"[SCAN] Dokument nie zapisany ({doc_result.status_code}): {att['filename']}")

                # Jeśli to FAKTURA — analizuj głębiej
                if category == "faktura" and atts:
                    for att in atts:
                        if att["content_type"] == "application/pdf" or \
                           att["content_type"].startswith("image/"):
                            inv = _analyze_invoice(claude, att, sender, subject)
                            if inv:
                                db = _prepare_invoice_db(inv, config.username,
                                                          sender, subject,
                                                          att["filename"])
                                db["message_id"] = message_id[:500]
                                await sb_insert("invoices", db)
                                results["faktury"].append(inv)

                elif category == "reklamacja":
                    results["reklamacje"].append({"subject": subject, "sender": sender})
                elif category == "zapytanie":
                    results["zapytania"].append({"subject": subject, "sender": sender})
                elif category == "zamowienie":
                    results["zamowienia"].append({"subject": subject, "sender": sender})
                elif category == "spam":
                    results["spam"].append({"subject": subject})
                else:
                    results["inne"].append({"subject": subject, "sender": sender})

            except Exception as e:
                print(f"[SCAN] Błąd emaila: {e}")
                errors.append(str(e))

        mail.logout()

        # Log skanowania
        total = sum(len(v) for v in results.values())
        await sb_insert("scan_logs", {
            "client_email":   config.username,
            "scanned_emails": len(ids),
            "invoices_found": len(results["faktury"]),
            "status":         "ok",
            "message":        f"Emaili: {len(ids)} | "
                              f"Faktury: {len(results['faktury'])} | "
                              f"Zapytania: {len(results['zapytania'])} | "
                              f"Zamówienia: {len(results['zamowienia'])} | "
                              f"Inne: {len(results['inne'])}"
        })

        if digest_emails:
            _send_daily_digest(config.username, digest_emails, results)

    except Exception as e:
        try: mail.logout()
        except: pass
        raise HTTPException(status_code=500, detail=str(e))

    total_new = sum(len(v) for v in results.values())
    print(f"[SCAN] Gotowe: nowe={total_new}, duplikaty={skipped_duplicates}")
    notification = "Brak nowych wiadomości na skrzynce." if total_new == 0 else f"Znaleziono {total_new} nowych emaili."
    return {
        "success":            True,
        "scanned_emails":     len(ids),
        "new_emails":         total_new,
        "skipped_duplicates": skipped_duplicates,
        "results":            {k: len(v) for k, v in results.items()},
        "details":            results,
        "errors":             errors,
        "notification":       notification,
        "scanned_at":         datetime.now().isoformat(),
    }

@app.get("/api/emails/{client_email:path}")
async def get_emails(client_email: str, category: str = "", authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    filters = f"client_email=eq.{client_email}"
    if category:
        filters += f"&category=eq.{category}"
    data = await sb_select("emails", filters)
    return {"success": True, "emails": data, "count": len(data)}

@app.post("/api/emails/reclassify/{client_email:path}")
async def reclassify_emails(client_email: str, authorization: Optional[str] = Header(None)):
    """Ponownie klasyfikuje juz zapisane maile (kategoria + priorytet) wg aktualnych zasad AI.
    Potrzebne bo /api/scan pomija juz zapisane maile jako duplikaty i nigdy ich nie przeklasyfikuje."""
    await _verify_auth_token(authorization)
    claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    emails = await sb_select("emails", f"client_email=eq.{client_email}")
    updated, skipped, errors = 0, 0, []
    for i, em in enumerate(emails):
        try:
            atts = [{"content_type": "application/pdf"}] if em.get("has_attachment") else []
            classification = _classify_email(
                claude, em.get("subject") or "", em.get("sender") or "",
                em.get("body") or "", atts, em.get("date")
            )
            # KRYTYCZNE: jesli AI zawiodlo (np. limit zapytan) i mamy tylko fallback slownikowy,
            # NIE nadpisuj juz zapisanych, dobrych danych (kategoria/priorytet/odpowiedzi) pustymi wartosciami.
            if classification.get("_source") == "keywords":
                skipped += 1
                errors.append(f"{str(em.get('subject',''))[:40]}: AI niedostepne, pominieto (dane bez zmian)")
                continue
            patch = {
                "category":      classification.get("category", em.get("category", "inne")),
                "priority":      classification.get("priority", "moze_poczekac"),
                "summary":       str(classification.get("summary", ""))[:1000],
                "action_needed": bool(classification.get("action_needed", False)),
                "action_desc":   str(classification.get("action_desc") or "")[:500],
                "reply_approve": str(classification.get("reply_approve") or "")[:3000] or None,
                "reply_reject":  str(classification.get("reply_reject") or "")[:3000] or None,
            }
            r = await sb_patch("emails", f"id=eq.{em['id']}", patch)
            if r.status_code in (200, 204):
                updated += 1
            else:
                errors.append(f"{str(em.get('subject',''))[:40]}: {r.status_code}")
        except Exception as e:
            errors.append(f"{str(em.get('subject',''))[:40]}: {e}")
        if i < len(emails) - 1:
            await asyncio.sleep(1.5)  # odstep miedzy mailami — nie odpalaj limitu zapytan na minute
    return {"success": True, "updated": updated, "skipped": skipped, "total": len(emails), "errors": errors}

@app.delete("/api/emails/delete/{email_id}")
async def delete_email(email_id: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    r = await sb_delete("emails", f"id=eq.{email_id}")
    return {"success": r.status_code in (200, 204)}

@app.patch("/api/emails/category/{email_id}")
async def update_email_category(email_id: str, data: dict, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    r = await sb_patch("emails", f"id=eq.{email_id}", {"category": data.get("category")})
    return {"success": r.status_code in (200, 204)}

@app.patch("/api/emails/{email_id}/status")
async def update_email_status(email_id: str, data: dict, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    status = data.get("status")
    if status not in ("nowe", "obsłużone"):
        raise HTTPException(status_code=400, detail="status musi być 'nowe' lub 'obsłużone'")
    r = await sb_patch("emails", f"id=eq.{email_id}", {"status": status})
    return {"success": r.status_code in (200, 204)}

@app.get("/api/invoices/{client_email:path}")
async def get_invoices(client_email: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    data = await sb_select("invoices", f"client_email=eq.{client_email}")
    return {"success": True, "invoices": data, "count": len(data)}

@app.delete("/api/invoices/{invoice_id}")
async def delete_invoice(invoice_id: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    r = await sb_delete("invoices", f"id=eq.{invoice_id}")
    return {"success": r.status_code in (200, 204)}

class SaveDocumentRequest(BaseModel):
    client_email: str
    filename: str
    content_type: str
    data: str
    size: int = 0
    subject: Optional[str] = ""
    sender: Optional[str] = ""

@app.post("/api/documents/save")
async def save_document(req: SaveDocumentRequest, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    r = await sb_insert("documents", {
        "client_email": req.client_email,
        "filename":     req.filename[:255],
        "content_type": req.content_type,
        "data":         req.data,
        "size":         req.size,
        "subject":      (req.subject or "")[:500],
        "sender":       (req.sender or "")[:255],
    })
    return {"success": r.status_code in (200, 201)}

@app.get("/api/documents/{client_email:path}")
async def get_documents(client_email: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    data = await sb_select("documents", f"client_email=eq.{client_email}")
    return {"success": True, "documents": data, "count": len(data)}

@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    r = await sb_delete("documents", f"id=eq.{document_id}")
    return {"success": r.status_code in (200, 204)}

@app.get("/api/inquiries/{client_email:path}")
async def get_inquiries(client_email: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    data = await sb_select("inquiries", f"client_email=eq.{client_email}")
    return {"success": True, "inquiries": data, "count": len(data)}

@app.post("/api/chat")
async def chat(req: ChatRequest, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    invoices = req.invoices
    if not invoices and req.client_email:
        try:
            data = await sb_select("invoices",
                                   f"client_email=eq.{req.client_email}")
            invoices = data if isinstance(data, list) else []
        except: pass
    ctx = f"Faktury: {json.dumps(invoices, ensure_ascii=False)}" \
          if invoices else "Brak faktur."
    r = claude.messages.create(
        model="claude-sonnet-4-6", max_tokens=800,
        messages=[{"role": "user", "content":
                   f"Jesteś asystentem księgowym. Odpowiadaj po polsku.\n"
                   f"{ctx}\n\nPytanie: {req.question}"}])
    return {"success": True, "answer": r.content[0].text}

class AttachmentRequest(BaseModel):
    imap: ImapConfig
    message_id: str

@app.post("/api/emails/attachments")
async def get_attachments(req: AttachmentRequest, authorization: Optional[str] = Header(None)):
    """Pobiera załączniki emaila z IMAP po message_id, zwraca jako base64."""
    await _verify_auth_token(authorization)
    config = req.imap
    try:
        mail = imaplib.IMAP4_SSL(config.host, config.port) if config.use_ssl \
               else imaplib.IMAP4(config.host, config.port)
        mail.login(config.username, config.password)
        mail.select("INBOX")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Błąd IMAP: {str(e)}")

    try:
        # Szukaj po Message-ID
        mid = req.message_id.strip()
        _, data = mail.search(None, f'(HEADER "Message-ID" "{mid}")')
        ids = data[0].split()
        # Fallback — szukaj w całej skrzynce
        if not ids:
            _, data = mail.search(None, "ALL")
            ids = data[0].split()[-500:]

        attachments = []
        for msg_id in ids:
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            found_mid = _clean_message_id(msg.get("Message-ID", "") or "")
            if found_mid and found_mid != mid:
                continue
            for part in msg.walk():
                ct = part.get_content_type()
                cd = part.get("Content-Disposition", "")
                filename = part.get_filename()
                if filename or "attachment" in cd:
                    filename = _decode_hdr(filename or "plik")
                    payload = part.get_payload(decode=True)
                    if payload:
                        attachments.append({
                            "filename": filename,
                            "content_type": ct,
                            "data": base64.b64encode(payload).decode("utf-8"),
                            "size": len(payload),
                        })
            if attachments:
                break
        mail.logout()
        return {"success": True, "attachments": attachments}
    except Exception as e:
        try: mail.logout()
        except: pass
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/emails/reply")
async def send_reply(req: ReplyRequest, authorization: Optional[str] = Header(None)):
    """Wysyła odpowiedź przez Resend API (HTTP, port 443) zamiast surowego SMTP —
    Render blokuje wychodzące polaczenia na portach SMTP (25/465/587) na darmowym planie."""
    await _verify_auth_token(authorization)
    if not RESEND_API_KEY:
        return JSONResponse(status_code=500, content={
            "success": False,
            "error": "Brak RESEND_API_KEY — ustaw tę zmienną środowiskową na Render (klucz z resend.com).",
        })

    # Prawdziwa skrzynka klienta — nie mozemy wysylac "z" niej bezposrednio (Resend wymaga
    # zweryfikowanej domeny, a domeny typu gmail.com/wp.pl nie da sie zweryfikowac dla cudzego konta),
    # wiec ustawiamy ja jako Reply-To: odpowiedz odbiorcy trafi tam, gdzie normalnie by trafila.
    client_mailbox = (req.reply_to or (req.imap.username if req.imap else "") or "").strip()

    sender = req.from_email.strip() if req.from_email else ""
    if not sender:
        label = f"{client_mailbox} przez Syndris" if client_mailbox else "Syndris"
        sender = f"{label} <onboarding@resend.dev>"

    subject = req.subject if req.subject.startswith("Re:") else f"Re: {req.subject}"

    params = {
        "from": sender,
        "to": [req.to],
        "subject": subject,
        "text": req.body,
    }
    if client_mailbox:
        params["reply_to"] = client_mailbox
    if req.in_reply_to:
        mid = req.in_reply_to if req.in_reply_to.startswith("<") else f"<{req.in_reply_to}>"
        params["headers"] = {"In-Reply-To": mid, "References": mid}

    try:
        result = resend.Emails.send(params)
        email_id = result.get("id") if isinstance(result, dict) else None
        print(f"[RESEND] Wysłano odpowiedź do {req.to} (id={email_id})")
        return {"success": True, "message": f"Odpowiedź wysłana do {req.to}", "id": email_id}
    except Exception as e:
        print(f"[RESEND] Błąd: {e}")
        return JSONResponse(status_code=502, content={
            "success": False,
            "error": str(e),
            "detail": str(e),
        })

@app.get("/api/follow-ups/{client_email:path}")
async def get_follow_ups(client_email: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    data = await sb_select("follow_ups", f"client_email=eq.{client_email}")
    return {"success": True, "follow_ups": data, "count": len(data)}

@app.post("/api/imap/folders")
async def list_imap_folders(req: FollowUpRequest, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    config = req.imap
    try:
        mail = imaplib.IMAP4_SSL(config.host, config.port) if config.use_ssl \
               else imaplib.IMAP4(config.host, config.port)
        mail.login(config.username, config.password)
        _, folder_list = mail.list()
        mail.logout()
        folders = []
        for item in folder_list or []:
            decoded = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
            folders.append(decoded)
        return {"folders": folders}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/follow-ups/scan")
async def scan_follow_ups(req: FollowUpRequest, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    config = req.imap
    found, skipped, errors = [], 0, []
    SENT_FOLDERS = ["[Gmail]/Wys&AUI-ane", "Sent", "Sent Items", "Sent Messages",
                    "[Gmail]/Sent Mail", "INBOX.Sent", "Wysłane"]
    try:
        mail = imaplib.IMAP4_SSL(config.host, config.port) if config.use_ssl \
               else imaplib.IMAP4(config.host, config.port)
        mail.login(config.username, config.password)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Błąd IMAP: {str(e)}")

    sent_folder = None
    # Najpierw próbuj znane nazwy
    for folder in SENT_FOLDERS:
        try:
            status, _ = mail.select(f'"{folder}"')
            if status == "OK":
                sent_folder = folder
                break
        except: pass

    # Fallback: przeszukaj wszystkie foldery i dopasuj po słowie kluczowym
    if not sent_folder:
        try:
            _, folder_list = mail.list()
            SENT_KEYWORDS = ["sent", "wysłane", "wyslan", "gesendet", "envoy", "inviati"]
            for item in folder_list or []:
                decoded = item.decode() if isinstance(item, bytes) else str(item)
                name_part = decoded.split('"/"')[-1].strip().strip('"') if '"/"' in decoded else decoded.split()[-1].strip('"')
                if any(k in name_part.lower() for k in SENT_KEYWORDS):
                    try:
                        status, _ = mail.select(f'"{name_part}"')
                        if status == "OK":
                            sent_folder = name_part
                            break
                    except: pass
        except: pass

    if not sent_folder:
        try: mail.logout()
        except: pass
        raise HTTPException(status_code=404, detail="Nie znaleziono folderu Wysłane — sprawdź czy skrzynka ma folder Sent/Wysłane")

    try:
        cutoff = (datetime.now() - timedelta(days=req.days_without_reply)).strftime("%d-%b-%Y")
        before  = (datetime.now() - timedelta(days=req.days_without_reply)).strftime("%d-%b-%Y")
        _, ids_raw = mail.search(None, f'(BEFORE "{datetime.now().strftime("%d-%b-%Y")}" SINCE "{cutoff}")')
        ids = ids_raw[0].split()[-50:]
        print(f"[FOLLOWUP] Wysłanych do analizy: {len(ids)}")

        for msg_id in ids:
            try:
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                subject    = _decode_hdr(msg.get("Subject", ""))
                to_field   = msg.get("To", "")
                date_str   = msg.get("Date", "")
                message_id = _clean_message_id(msg.get("Message-ID", "") or "")
                if not message_id:
                    import hashlib
                    message_id = "hash-" + hashlib.md5(f"{to_field}{subject}{date_str}".encode()).hexdigest()

                # Sprawdź duplikat w follow_ups
                exists = await sb_exists("follow_ups", config.username, message_id)
                if exists:
                    skipped += 1
                    continue

                # Sprawdź czy w INBOX jest odpowiedź (In-Reply-To lub References)
                mail.select("INBOX")
                _, reply_ids = mail.search(None, f'(OR HEADER "In-Reply-To" "{message_id}" HEADER "References" "{message_id}")')
                has_reply = bool(reply_ids[0].split())
                mail.select(f'"{sent_folder}"')

                if not has_reply:
                    sent_date = _to_date(date_str)
                    record = {
                        "client_email": config.username,
                        "message_id":   message_id[:500],
                        "subject":      str(subject)[:500],
                        "sent_to":      str(to_field)[:255],
                        "sent_at":      sent_date,
                        "days_waiting": req.days_without_reply,
                        "status":       "oczekuje",
                    }
                    r = await sb_insert("follow_ups", record)
                    if r.status_code in (200, 201):
                        found.append(record)
                        print(f"[FOLLOWUP] Brak odpowiedzi: {subject[:50]}")
            except Exception as e:
                errors.append(str(e))

        mail.logout()
    except Exception as e:
        try: mail.logout()
        except: pass
        raise HTTPException(status_code=500, detail=str(e))

    return {"success": True, "found": len(found), "skipped": skipped,
            "errors": errors, "follow_ups": found}

@app.patch("/api/follow-ups/{follow_up_id}")
async def update_follow_up(follow_up_id: str, data: dict, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    r = await sb_patch("follow_ups", f"id=eq.{follow_up_id}", data)
    return {"success": r.status_code in (200, 204)}

@app.delete("/api/follow-ups/{follow_up_id}")
async def delete_follow_up(follow_up_id: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    r = await sb_delete("follow_ups", f"id=eq.{follow_up_id}")
    return {"success": r.status_code in (200, 204)}

@app.get("/api/reminders/{client_email:path}")
async def get_reminders(client_email: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    data = await sb_select("reminders", f"client_email=eq.{client_email}&order=reminder_date.asc")
    return {"success": True, "reminders": data, "count": len(data)}

@app.delete("/api/reminders/{reminder_id}")
async def delete_reminder(reminder_id: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    r = await sb_delete("reminders", f"id=eq.{reminder_id}")
    return {"success": r.status_code in (200, 204)}

# ── AUTH (Supabase Auth) ──

async def _verify_auth_token(authorization: Optional[str]) -> dict:
    """Wspolna weryfikacja tokenu Syndris (Supabase Auth) — uzywana na endpointach
    ktore koszutuja (Claude/Resend) albo dotykaja danych logowania klienta (IMAP/KSeF)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Wymagane logowanie — brak tokenu autoryzacji")
    token = authorization.split(" ", 1)[1]
    url = f"{SUPABASE_URL}/auth/v1/user"
    headers = {"apikey": SUPABASE_SECRET, "Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as c:
        r = await c.get(url, headers=headers, timeout=15)
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Nieprawidłowy lub wygasły token — zaloguj się ponownie")
    return r.json()

@app.post("/api/auth/register")
async def auth_register(req: RegisterRequest):
    url = f"{SUPABASE_URL}/auth/v1/signup"
    headers = {"apikey": SUPABASE_SECRET, "Content-Type": "application/json"}
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=headers,
                          json={"email": req.email, "password": req.password}, timeout=15)
    if r.status_code not in (200, 201):
        try:
            detail = r.json().get("msg") or r.json().get("error_description") or r.text[:300]
        except Exception:
            detail = r.text[:300]
        raise HTTPException(status_code=400, detail=f"Rejestracja nieudana: {detail}")

    data = r.json()
    auth_user_id = data.get("id") or (data.get("user") or {}).get("id")
    await sb_insert("users", {
        "email":         req.email,
        "auth_user_id":  auth_user_id,
        "plan":          req.plan,
        "status":        "trial",
    })
    needs_confirmation = not data.get("access_token")
    return {
        "success": True,
        "message": "Sprawdź email, żeby potwierdzić konto." if needs_confirmation else "Konto utworzone.",
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "user_id": auth_user_id,
    }

@app.post("/api/auth/login")
async def auth_login(req: LoginRequest):
    url = f"{SUPABASE_URL}/auth/v1/token?grant_type=password"
    headers = {"apikey": SUPABASE_SECRET, "Content-Type": "application/json"}
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=headers,
                          json={"email": req.email, "password": req.password}, timeout=15)
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Nieprawidłowy email lub hasło")
    data = r.json()
    return {
        "success": True,
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "user": data.get("user"),
    }

@app.post("/api/auth/refresh")
async def auth_refresh(req: RefreshRequest):
    """Wymienia refresh_token na nowy access_token — pozwala sesji przetrwac dluzej
    niz czas zycia access_token (zwykle ~1h) bez ponownego logowania hasłem."""
    url = f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token"
    headers = {"apikey": SUPABASE_SECRET, "Content-Type": "application/json"}
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=headers,
                          json={"refresh_token": req.refresh_token}, timeout=15)
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Nie udało się odświeżyć sesji — zaloguj się ponownie")
    data = r.json()
    return {
        "success": True,
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
    }

@app.get("/api/auth/me")
async def auth_me(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Brak tokenu autoryzacji")
    token = authorization.split(" ", 1)[1]
    url = f"{SUPABASE_URL}/auth/v1/user"
    headers = {"apikey": SUPABASE_SECRET, "Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as c:
        r = await c.get(url, headers=headers, timeout=15)
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Nieprawidłowy lub wygasły token")
    return {"success": True, "user": r.json()}

@app.get("/api/auth/oauth/{provider}")
async def auth_oauth_redirect(provider: str, redirect_to: str = ""):
    """Przekierowuje przegladarke klienta na hostowany OAuth Supabase (np. Azure/Microsoft).
    Wymaga wlaczonego providera w Supabase Dashboard → Authentication → Providers,
    oraz dodania adresu strony logowania do dozwolonych Redirect URLs."""
    if provider not in ("azure", "google", "github"):
        raise HTTPException(status_code=400, detail=f"Nieobsługiwany provider OAuth: {provider}")
    if not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=500,
            detail="Brak SUPABASE_ANON_KEY na serwerze — dodaj tę zmienną środowiskową na Render "
                   "(wartość z Supabase → Settings → API → 'anon public' key).")
    url = f"{SUPABASE_URL}/auth/v1/authorize?provider={provider}&apikey={SUPABASE_ANON_KEY}"
    if redirect_to:
        url += f"&redirect_to={quote(redirect_to, safe='')}"
    return RedirectResponse(url)

@app.post("/api/auth/sync")
async def auth_sync(req: SyncUserRequest, authorization: Optional[str] = Header(None)):
    """Wolane po kazdym logowaniu (haslo lub OAuth) — upewnia sie, ze istnieje rekord
    w naszej tabeli users, nawet jesli konto powstalo przez OAuth (bez /api/auth/register)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Brak tokenu autoryzacji")
    token = authorization.split(" ", 1)[1]
    url = f"{SUPABASE_URL}/auth/v1/user"
    headers = {"apikey": SUPABASE_SECRET, "Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as c:
        r = await c.get(url, headers=headers, timeout=15)
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Nieprawidłowy lub wygasły token")
    user = r.json()
    email = user.get("email")
    existing = await sb_select("users", f"email=eq.{email}")
    if not existing:
        await sb_insert("users", {
            "email":        email,
            "auth_user_id": user.get("id"),
            "plan":         req.plan,
            "status":       "trial",
        })
    return {"success": True, "user": user}

# ── KSeF 2.0 (Etap 1: autoryzacja) ──
# Zrodlo prawdy: github.com/CIRFMF/ksef-api (repo Ministerstwa Finansow).
# Pelen flow autoryzacji tokenem KSeF ma 6 krokow — authenticationToken z /auth/ksef-token
# NIE jest tokenem dostepowym, trzeba go "wymienic" (redeem) na accessToken.

@app.post("/api/ksef/connect")
async def ksef_connect(req: KsefConnectRequest, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    env = req.env if req.env in KSEF_BASE_URLS else "test"
    nip = re.sub(r"\D", "", req.nip)
    if len(nip) != 10:
        raise HTTPException(status_code=400, detail="NIP musi mieć dokładnie 10 cyfr")
    if not req.ksef_token.strip():
        raise HTTPException(status_code=400, detail="Brak tokenu KSeF")

    try:
        # Krok 1: klucz publiczny MF do szyfrowania tokenu
        public_key = await _ksef_fetch_public_key(env)

        # Krok 2: challenge + timestamp
        challenge_data = await _ksef_get_challenge(env)
        challenge = challenge_data.get("challenge")
        timestamp_ms = challenge_data.get("timestamp", challenge_data.get("timestampMs"))
        if not challenge or timestamp_ms is None:
            raise HTTPException(status_code=502,
                detail=f"KSeF: nieoczekiwana odpowiedź /auth/challenge: {challenge_data}")

        # Krok 3: szyfrowanie "{token}|{timestamp}" RSA-OAEP/SHA-256
        encrypted_token = _ksef_encrypt_token(public_key, req.ksef_token.strip(), timestamp_ms)

        # Krok 4: wymiana challenge + zaszyfrowany token na authenticationToken (tymczasowy)
        submit = await _ksef_submit_ksef_token(env, challenge, nip, encrypted_token)
        auth_token = submit.get("authenticationToken")
        reference_number = submit.get("referenceNumber")
        if not auth_token or not reference_number:
            raise HTTPException(status_code=502,
                detail=f"KSeF: nieoczekiwana odpowiedź /auth/ksef-token: {submit}")

        # Krok 5: czekaj aż autoryzacja się zakończy
        await _ksef_wait_for_auth(env, reference_number, auth_token)

        # Krok 6: wymień authenticationToken na docelowy accessToken + refreshToken
        redeemed = await _ksef_redeem_access_token(env, auth_token)
        access_token = _extract_jwt_value(redeemed.get("accessToken"))
        refresh_token = _extract_jwt_value(redeemed.get("refreshToken"))
        if not access_token:
            raise HTTPException(status_code=502,
                detail=f"KSeF: brak accessToken w odpowiedzi /auth/token/redeem: {redeemed}")

        expires_at = _jwt_exp_iso(access_token)
        encrypted_stored_token = _ksef_encrypt_for_storage(req.ksef_token.strip())

        record = {
            "client_email":         req.client_email,
            "nip":                  nip,
            "ksef_token_encrypted": encrypted_stored_token,
            "auth_token":           access_token,
            "refresh_token":        refresh_token,
            "expires_at":           expires_at,
            "env":                  env,
            "status":               "active",
        }
        # Jedno aktywne polaczenie na klienta+srodowisko — usun poprzednie i zapisz nowe.
        await sb_delete("ksef_connections", f"client_email=eq.{req.client_email}&env=eq.{env}")
        r = await sb_insert("ksef_connections", record)
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=502,
                detail=f"Autoryzacja KSeF ok, ale zapis do bazy nie powiódł się ({r.status_code})")

        return {"success": True, "message": f"Połączono z KSeF ({env})",
                "env": env, "nip": nip, "expires_at": expires_at}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[KSEF] Błąd połączenia: {e}")
        raise HTTPException(status_code=502, detail=f"Błąd integracji KSeF: {e}")

@app.get("/api/ksef/status/{client_email:path}")
async def ksef_status(client_email: str, authorization: Optional[str] = Header(None)):
    await _verify_auth_token(authorization)
    data = await sb_select("ksef_connections", f"client_email=eq.{client_email}")
    if not data:
        return {"success": True, "connected": False}
    conn = data[0]  # sb_select sortuje po created_at desc — najnowsze polaczenie
    return {
        "success":   True,
        "connected": conn.get("status") == "active",
        "nip":       conn.get("nip"),
        "env":       conn.get("env"),
        "last_sync": conn.get("last_sync"),
        "expires_at": conn.get("expires_at"),
    }

def _ksef_base_url(env: str) -> str:
    return KSEF_BASE_URLS.get(env, KSEF_BASE_URLS["test"])

async def _ksef_fetch_public_key(env: str):
    """GET /security/public-key-certificates — zwraca klucz publiczny MF do szyfrowania tokenu KSeF."""
    url = f"{_ksef_base_url(env)}/security/public-key-certificates"
    async with httpx.AsyncClient() as c:
        r = await c.get(url, timeout=15)
    if r.status_code != 200:
        raise HTTPException(status_code=502,
            detail=f"KSeF: nie udało się pobrać klucza publicznego ({r.status_code}): {r.text[:300]}")
    data = r.json()
    entries = data if isinstance(data, list) else (data.get("certificates") or data.get("items") or [data])
    cert_b64 = None
    for entry in entries:
        usage = entry.get("usage") or []
        if isinstance(usage, str): usage = [usage]
        if not cert_b64:
            cert_b64 = entry.get("certificate")
        if "KsefTokenEncryption" in usage:
            cert_b64 = entry.get("certificate")
            break
    if not cert_b64:
        raise HTTPException(status_code=502, detail="KSeF: brak certyfikatu w odpowiedzi /security/public-key-certificates")
    cert = x509.load_der_x509_certificate(base64.b64decode(cert_b64))
    return cert.public_key()

async def _ksef_get_challenge(env: str) -> dict:
    """POST /auth/challenge — challenge wazny 10 minut."""
    url = f"{_ksef_base_url(env)}/auth/challenge"
    async with httpx.AsyncClient() as c:
        r = await c.post(url, timeout=15)
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502,
            detail=f"KSeF: błąd pobierania challenge ({r.status_code}): {r.text[:300]}")
    return r.json()

def _ksef_encrypt_token(public_key, ksef_token: str, timestamp_ms) -> str:
    """RSA-OAEP/SHA-256 nad '{token}|{timestampMs}', wynik base64 — zgodnie z dokumentacja CIRFMF."""
    plaintext = f"{ksef_token}|{timestamp_ms}".encode("utf-8")
    ciphertext = public_key.encrypt(
        plaintext,
        rsa_padding.OAEP(
            mgf=rsa_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode("utf-8")

async def _ksef_submit_ksef_token(env: str, challenge: str, nip: str, encrypted_token: str) -> dict:
    """POST /auth/ksef-token — zwraca authenticationToken (tymczasowy) + referenceNumber."""
    url = f"{_ksef_base_url(env)}/auth/ksef-token"
    body = {
        "challenge": challenge,
        "contextIdentifier": {"type": "nip", "value": nip},
        "encryptedToken": encrypted_token,
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(url, json=body, timeout=15)
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=401,
            detail=f"KSeF: odrzucono token/autoryzację ({r.status_code}): {r.text[:300]}")
    return r.json()

async def _ksef_wait_for_auth(env: str, reference_number: str, auth_token: str):
    """GET /auth/{referenceNumber} — polling do zakonczenia autoryzacji.
    UWAGA: dokladna semantyka kodow statusu nie jest w pelni udokumentowana publicznie.
    Zakladamy: code==200 -> sukces, code>=400 -> blad, inaczej -> w toku. Zweryfikuj przy
    pierwszym realnym tescie (panel Swagger: api-test.ksef.mf.gov.pl/docs/v2) i dostosuj w razie potrzeby."""
    url = f"{_ksef_base_url(env)}/auth/{reference_number}"
    headers = {"Authorization": f"Bearer {auth_token}"}
    for attempt in range(10):
        async with httpx.AsyncClient() as c:
            r = await c.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            status_obj = data.get("status", data) if isinstance(data, dict) else {}
            code = status_obj.get("code") if isinstance(status_obj, dict) else None
            if code is None:
                print(f"[KSEF] Nieznany kształt odpowiedzi statusu autoryzacji: {data}")
                return data
            if code == 200:
                return data
            if isinstance(code, int) and code >= 400:
                raise HTTPException(status_code=401,
                    detail=f"KSeF: autoryzacja odrzucona (status {code}): {status_obj.get('description','')}")
            # w przeciwnym razie autoryzacja wciaz w toku — czekaj i sprobuj ponownie
        await asyncio.sleep(1.5)
    raise HTTPException(status_code=504, detail="KSeF: przekroczono czas oczekiwania na potwierdzenie autoryzacji")

async def _ksef_redeem_access_token(env: str, auth_token: str) -> dict:
    """POST /auth/token/redeem — wymienia authenticationToken na wlasciwy accessToken + refreshToken."""
    url = f"{_ksef_base_url(env)}/auth/token/redeem"
    headers = {"Authorization": f"Bearer {auth_token}"}
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=headers, timeout=15)
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=401,
            detail=f"KSeF: nie udało się odebrać access tokena ({r.status_code}): {r.text[:300]}")
    return r.json()

def _extract_jwt_value(value) -> Optional[str]:
    """accessToken/refreshToken bywaja zwracane jako plain string albo obiekt {token:...} — obsluz oba."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("token") or value.get("value") or value.get("accessToken")
    return None

def _jwt_exp_iso(token: str) -> Optional[str]:
    """Odczytuje pole exp z JWT (bez weryfikacji podpisu — tylko do wyswietlenia daty wygasniecia)."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        if exp:
            return datetime.utcfromtimestamp(exp).isoformat()
    except Exception:
        pass
    return None

def _ksef_encrypt_for_storage(value: str) -> str:
    if not _ksef_fernet:
        raise HTTPException(status_code=500,
            detail="Brak KSEF_ENCRYPTION_KEY na serwerze — ustaw tę zmienną środowiskową na Render przed podłączeniem KSeF.")
    return _ksef_fernet.encrypt(value.encode("utf-8")).decode("utf-8")

# ── HELPERS ──
CATEGORY_DIGEST_LABELS = {
    "faktury": "faktur", "reklamacje": "reklamacji", "zapytania": "zapytań",
    "zamowienia": "zamówień", "spam": "spam", "inne": "innych",
}

def _send_daily_digest(client_email: str, digest_emails: list, results: dict):
    """Wysyła krótkie podsumowanie HTML po skanowaniu przez Resend — tylko gdy są nowe emaile."""
    if not RESEND_API_KEY:
        print("[DIGEST] Brak RESEND_API_KEY — pomijam wysyłkę digestu")
        return
    total = len(digest_emails)
    cat_rows = "".join(
        f"<li>{len(v)} {CATEGORY_DIGEST_LABELS.get(k, k)}</li>"
        for k, v in results.items() if len(v) > 0
    )
    urgent = [e for e in digest_emails if e.get("action_needed")]
    urgent_rows = "".join(
        f"<li><b>{html.escape(str(e.get('subject') or ''))[:120]}</b>"
        f"{' — ' + html.escape(str(e.get('action_desc') or ''))[:200] if e.get('action_desc') else ''}</li>"
        for e in urgent[:20]
    )
    body_html = (
        '<div style="font-family:Arial,sans-serif;max-width:600px">'
        '<h2>Syndris — podsumowanie skanowania</h2>'
        f"<p>Znaleziono <b>{total}</b> nowych emaili:</p>"
        f"<ul>{cat_rows}</ul>"
        + (f"<p><b>Wymagają pilnej odpowiedzi:</b></p><ul>{urgent_rows}</ul>" if urgent_rows else "")
        + "</div>"
    )
    try:
        resend.Emails.send({
            "from": "SyndrisAI <onboarding@resend.dev>",
            "to": [client_email],
            "subject": f"Syndris: {total} nowych emaili na skrzynce",
            "html": body_html,
        })
        print(f"[DIGEST] Wysłano digest do {client_email} ({total} nowych)")
    except Exception as e:
        print(f"[DIGEST] Błąd wysyłki digestu: {e}")

def _decode_hdr(val):
    try:
        parts = decode_header(val)
        return "".join(
            p.decode(c or "utf-8", errors="replace")
            if isinstance(p, bytes) else str(p)
            for p, c in parts)
    except:
        return str(val)

_HTML_STUB_HINTS = [
    "nie obsluguje wiadomosci html", "nie obsluguje html", "przelacz klienta pocztowego",
    "przelacz program pocztowy", "does not support html", "html mode",
    "view this email in a web browser", "wyswietlic wiadomosc",
]

def _get_body(msg) -> str:
    """Wyciąga tekst z emaila (plain text lub HTML). Jeśli część text/plain to tylko
    placeholder ('Twój program pocztowy nie obsługuje HTML...'), parsuje zamiast tego część HTML."""
    plain, html_text = "", ""
    try:
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not plain:
                payload = part.get_payload(decode=True)
                if payload:
                    plain = payload.decode("utf-8", errors="replace").strip()
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not html_text:
                payload = part.get_payload(decode=True)
                if payload:
                    raw = payload.decode("utf-8", errors="replace")
                    # Usuń całe bloki <style>/<script> (razem z zawartością, np. regułami CSS)
                    raw = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", raw)
                    stripped = re.sub(r"<[^>]+>", " ", raw)
                    stripped = html.unescape(stripped)
                    html_text = re.sub(r"\s+", " ", stripped).strip()
    except: pass

    plain_norm = _strip_diacritics(plain.lower())
    is_stub = html_text and (len(plain) < 60 or any(h in plain_norm for h in _HTML_STUB_HINTS))
    body = html_text if is_stub else (plain or html_text)
    return body[:10000]

def _get_attachments(msg):
    result = []
    for part in msg.walk():
        ct = part.get_content_type()
        cd = str(part.get("Content-Disposition", ""))
        if ct in ("application/pdf", "image/jpeg", "image/png", "image/jpg") \
           and "attachment" in cd:
            try:
                content = part.get_payload(decode=True)
                if content and len(content) > 100:
                    result.append({
                        "filename":     _decode_hdr(part.get_filename() or "file"),
                        "content":      content,
                        "content_type": ct,
                    })
            except: pass
    return result

REPLY_CATEGORIES = {"faktura", "reklamacja", "zapytanie", "zamowienie"}

def _classify_email(claude, subject, sender, body, atts, date) -> dict:
    """Klasyfikuje email i generuje propozycje odpowiedzi — Haiku (tani)."""
    has_pdf = any(a["content_type"] == "application/pdf" for a in atts)
    prompt = (
        'Jesteś doświadczonym specjalistą obsługi klienta. Przeanalizuj poniższy email '
        'z NAJWYŻSZĄ UWAGĄ i przydziel go do DOKŁADNIE JEDNEJ kategorii. '
        'Odpowiedz WYŁĄCZNIE czystym JSON, bez markdown:\n'
        '{"category":"faktura|reklamacja|zapytanie|zamowienie|spam|inne",'
        '"priority":"pilne|wazne|moze_poczekac","summary":"max 1 zdanie po polsku",'
        '"action_needed":true/false,"action_desc":"co zrobic lub null",'
        '"reply_approve":"pelna formalna odpowiedz POZYTYWNA z powitaniem i pozegnaniem (null dla spam/inne)",'
        '"reply_reject":"pelna formalna odpowiedz NEGATYWNA z powitaniem i pozegnaniem (null dla spam/inne)",'
        '"reminders":[{"date":"YYYY-MM-DD","description":"co zrobic","type":"platnosc|spotkanie|termin|inne"}]}\n'
        'reminders: lista terminow/dat wykrytych w tresci emaila (np. termin platnosci, spotkanie, deadline, koniec przetargu). '
        'Pusta lista [] jesli brak. KRYTYCZNE dla pola "date": przepisz rok DOKŁADNIE tak jak jest napisany w tresci maila — '
        'nigdy nie zgaduj ani nie koryguj roku na podstawie własnych założeń o aktualnej dacie. '
        'Sprawdz dwukrotnie cyfry roku (np. "19.09.2026" to rok 2026, nie 2024) zanim zwrocisz JSON.\n'
        '\n'
        'ZASADY ROZRÓŻNIANIA (najczęstsza pomyłka — czytaj uważnie):\n'
        '1) "zapytanie" — klient PYTA o cokolwiek związanego z transakcją, ZANIM doszło do złożenia zamówienia: '
        'pyta o cenę, dostępność, ofertę, wycenę, warunki, termin, możliwość zakupu, szczegóły produktu/usługi. '
        'Rozpoznaj to po: znaku zapytania, słowach "czy", "ile kosztuje", "jaka jest dostępność", "proszę o wycenę/ofertę/informację", '
        '"chciałbym zapytać", "czy mogę zamówić/kupić" (to pytanie, więc mimo słowa "zamówić" to WCIĄŻ zapytanie!). '
        'Klient jeszcze NIE zadeklarował ostatecznie zakupu — dopytuje.\n'
        '2) "zamowienie" — klient JEDNOZNACZNIE SKŁADA lub POTWIERDZA zamówienie, bez pytania: '
        'np. "Zamawiam 5 sztuk, proszę o realizację", "Składam zamówienie nr X", "Potwierdzam zamówienie", '
        'podaje dane do wysyłki/faktury jako część już zdecydowanego zakupu, pyta o STATUS już złożonego zamówienia, '
        'informuje o wysyłce/paczce/przesyłce dotyczącej istniejącego zamówienia. '
        'Kluczowe: to STWIERDZENIE/DECYZJA, nie pytanie rozpoznawcze.\n'
        '3) "faktura"=FV/rachunek/paragon/invoice (także gdy jest PDF w załączniku), '
        '"reklamacja"=zwrot/skarga/complaint/problem/niezgodność, '
        '"spam"=WYŁĄCZNIE masowe reklamy i newslettery promocyjne od sklepów/serwisów bez żadnego związku z finansami lub operacjami firmy (Uber Eats, TikTok, Vinted, gry, loterie, "wygraj nagrodę" itp.).\n'
        'ABSOLUTNIE NIE klasyfikuj jako spam: powiadomień bankowych (nawet o braku środków!), alertów bezpieczeństwa konta, '
        'potwierdzeń transakcji/przelewów, wyciągów, przypomnień o płatnościach, faktur, powiadomień o zamówieniach, '
        'powiadomień z serwisów biznesowych (GitHub, Supabase, Render, hosting, domeny itp.).\n'
        '"inne"=powiadomienia bankowe, alerty konta, potwierdzenia transakcji, statusy zamówień, powiadomienia systemowe/biznesowe.\n'
        'W razie wątpliwości między zapytanie a zamowienie: jeśli w treści jest jakiekolwiek pytanie dotyczące transakcji '
        '— wybierz "zapytanie". Wybierz "zamowienie" TYLKO gdy nie ma już nic do ustalenia, klient po prostu zamawia/potwierdza.\n'
        '\n'
        'USTALANIE PRIORYTETU (priority) — NAJWAŻNIEJSZA ZASADA: myśl jak doświadczony asystent właściciela małej firmy. '
        'Zadaj sobie pytanie: "Czy właściciel firmy MUSI o tym wiedzieć?" Jeśli TAK — minimum "wazne".\n'
        '\n'
        '"pilne" — właściciel firmy musi zareagować DZIŚ:\n'
        '  * Finanse z problemem: brak/niewystarczające środki, zablokowane konto/karta, przekroczony limit, '
        '    wezwanie do zapłaty, windykacja, komornik, faktura przeterminowana lub płatna dziś/jutro\n'
        '  * Klient w kryzysie: poważna reklamacja, groźba rezygnacji, groźba prawna, bardzo niezadowolony klient\n'
        '  * Operacje: awaria usługi krytycznej, utrata dostępu do ważnego systemu\n'
        '\n'
        '"wazne" — właściciel firmy powinien to przejrzeć w ciągu 1-2 dni:\n'
        '  * KAŻDY mail od banku lub instytucji finansowej (nawet informacyjny — wyciąg, potwierdzenie przelewu)\n'
        '  * KAŻDE nowe zapytanie ofertowe, zamówienie, reklamacja\n'
        '  * KAŻDA faktura, umowa, dokument do podpisania\n'
        '  * Powiadomienia od dostawców usług biznesowych (hosting, domeny, oprogramowanie, ubezpieczenia)\n'
        '  * Powiadomienia o dostawie/paczce związanej z działalnością firmy\n'
        '  * Alerty bezpieczeństwa konta firmowego\n'
        '  * Każdy mail gdzie nadawca jest kontrahentem, dostawcą lub instytucją (urząd, ZUS, US, bank)\n'
        '\n'
        '"moze_poczekac" — TYLKO gdy mail nie ma ŻADNEGO związku z biznesem i nie wymaga żadnej akcji:\n'
        '  * Czyste reklamy i newslettery od sklepów (Uber Eats, Vinted, gry, loterie, "wygraj nagrodę")\n'
        '  * Powiadomienia społecznościowe (TikTok, social media) niezwiązane z działalnością firmy\n'
        '  * Potwierdzenia prywatnych zakupów bez znaczenia dla firmy\n'
        '\n'
        'ZASADA BEZPIECZEŃSTWA: w razie wątpliwości zawsze wybierz wyższy priorytet. '
        'Lepiej poinformować klienta o czymś mniej ważnym niż przeoczyć coś krytycznego dla firmy.\n'
        '\n'
        'ODPOWIEDZI (reply_approve/reply_reject) — WYMAGANE, PEŁNE i RÓŻNE od siebie dla: faktura, reklamacja, zapytanie, zamowienie. '
        'NIGDY nie zostawiaj reply_reject pustego, null ani identycznego z reply_approve dla tych czterech kategorii — '
        'nawet gdy scenariusz odmowny wydaje się mało prawdopodobny, i tak napisz pełną, konkretną wersję. '
        'Znaczenie "pozytywna"/"negatywna" zależy od kategorii:\n'
        '- "zapytanie": pozytywna = tak, mamy dostępność/ofertę, oto szczegóły/cena/termin; '
        'negatywna = niestety nie mamy tego w ofercie/brak dostępności/nie możemy zrealizować takiego zapytania.\n'
        '- "zamowienie": pozytywna = potwierdzamy przyjęcie zamówienia do realizacji; '
        'negatywna = niestety nie możemy zrealizować zamówienia (brak towaru/inny powód).\n'
        '- "reklamacja": pozytywna = uznajemy reklamację, oto dalsze kroki; '
        'negatywna = odrzucamy reklamację, z uzasadnieniem.\n'
        '- "faktura": pozytywna = potwierdzamy przyjęcie faktury do zapłaty; '
        'negatywna = kwestionujemy fakturę (niezgodność/błąd), z prośbą o korektę.\n'
        'Styl: formalny, profesjonalny, po polsku.\n'
        f'Od: {sender[:80]} | Temat: {subject[:120]} | PDF: {has_pdf} | Tresc: {body}'
    )
    result = None
    last_err = None
    for attempt in range(3):
        try:
            r = claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}])
            raw = r.content[0].text.replace("```json","").replace("```","").strip()
            result = json.loads(raw)
            break
        except Exception as e:
            last_err = e
            print(f"[AI] Proba {attempt+1}/3 nieudana: {e}")
            if attempt < 2:
                time.sleep(2 * (attempt + 1))  # odczekaj przed kolejna proba (np. rate limit)

    if result is None:
        print(f"[AI] Blad klasyfikacji po 3 probach: {last_err}")
        fallback = _classify_by_keywords(subject, sender, has_pdf, body)
        fallback["_source"] = "keywords"  # sygnalizuje ze to NIE jest wynik AI (uzyte np. przy reklasyfikacji, zeby nie nadpisywac dobrych danych)
        return fallback

    category = result.get("category")
    if category in REPLY_CATEGORIES:
        approve = (result.get("reply_approve") or "").strip()
        reject  = (result.get("reply_reject") or "").strip()
        if not approve:
            filled = _fill_missing_reply(claude, category, "approve", subject, sender, body)
            if filled: result["reply_approve"] = filled; approve = filled
        if not reject or reject == approve:
            filled = _fill_missing_reply(claude, category, "reject", subject, sender, body)
            if filled: result["reply_reject"] = filled
    result["_source"] = "ai"
    return result

_REPLY_HINTS = {
    "zapytanie":  {"approve": "TAK, mamy dostepnosc/oferte — podaj cene/termin/warunki",
                   "reject":  "NIESTETY nie mamy tego w ofercie / brak dostepnosci, nie mozemy zrealizowac tego zapytania"},
    "zamowienie": {"approve": "potwierdzamy przyjecie zamowienia do realizacji",
                   "reject":  "NIESTETY nie mozemy zrealizowac zamowienia (np. brak towaru)"},
    "reklamacja": {"approve": "uznajemy reklamacje — opisz dalsze kroki",
                   "reject":  "odrzucamy reklamacje, z uzasadnieniem"},
    "faktura":    {"approve": "potwierdzamy przyjecie faktury do zaplaty",
                   "reject":  "kwestionujemy fakture (niezgodnosc/blad) i prosimy o korekte"},
}

def _fill_missing_reply(claude, category: str, kind: str, subject, sender, body) -> Optional[str]:
    """Dogenerowuje brakujaca (approve lub reject) wersje odpowiedzi osobnym, ukierunkowanym zapytaniem —
    gwarantuje wynik niezaleznie od tego, czy glowna klasyfikacja sie do tego zastosowala."""
    hint = _REPLY_HINTS.get(category, {}).get(kind, "")
    prompt = (
        'Napisz WYŁĄCZNIE pełną, formalną odpowiedź email po polsku (z powitaniem i pożegnaniem), '
        'bez żadnych dodatkowych komentarzy, cudzysłowów ani JSON — sam tekst odpowiedzi.\n'
        f'Odpowiedź ma być w tonie: {hint}.\n'
        f'Od: {sender[:80]} | Temat: {subject[:120]} | Treść: {body}'
    )
    try:
        r = claude.messages.create(
            model="claude-haiku-4-5", max_tokens=800,
            messages=[{"role": "user", "content": prompt}])
        text = r.content[0].text.strip()
        return text or None
    except Exception as e:
        print(f"[AI] Blad dogenerowania odpowiedzi ({kind}): {e}")
        return None

def _strip_diacritics(text: str) -> str:
    """Zamienia polskie znaki na ich odpowiedniki ASCII, żeby dopasowanie słów kluczowych działało niezależnie od diakrytyków."""
    table = str.maketrans("ąćęłńóśźż", "acelnoszz")
    return text.translate(table)

def _classify_by_keywords(subject: str, sender: str, has_pdf: bool, body: str = "") -> dict:
    """Klasyfikacja bez AI — fallback gdy API niedostepne."""
    s = _strip_diacritics((subject + " " + body[:300]).lower())
    sndr = sender.lower()
    is_question = "?" in subject or "?" in body[:300] or \
        any(w in s for w in ["czy ","ile kosztuje","jaka jest dostepnosc","prosze o wycene",
                              "prosze o oferte","chcialbym zapytac","jaki jest termin"])
    is_urgent = any(w in s for w in ["pilne","asap","natychmiast","dzisiaj","jak najszybciej",
                                      "ostateczne wezwanie","zalegla","zaleglosc","windykacja",
                                      "wezwanie do zaplaty","przeterminowana"])
    is_upset = any(w in s for w in ["skandal","oburzony","niedopuszczalne","rezygnuje","rezygnacja",
                                     "prawnik","sad ","natychmiastowego"])

    if has_pdf or any(w in s for w in ["faktura","invoice","fv/","rachunek","paragon","receipt"]):
        prio = "pilne" if is_urgent else "wazne"
        return {"category":"faktura","priority":prio,"summary":subject,"action_needed":True,"action_desc":"Sprawdz fakture","reply_approve":None,"reply_reject":None}
    if any(w in s for w in ["reklamacja","zwrot","complaint","problem","niezgodnosc"]):
        prio = "pilne" if (is_urgent or is_upset) else "wazne"
        return {"category":"reklamacja","priority":prio,"summary":subject,"action_needed":True,"action_desc":"Rozpatrz reklamacje","reply_approve":None,"reply_reject":None}
    # pytanie o transakcje ma pierwszenstwo przed ogolnymi slowami zwiazanymi z zamowieniem
    if is_question or any(w in s for w in ["zapytanie","oferta","wycena","wspolpraca","pytanie","cena","dostepnosc"]):
        prio = "pilne" if is_urgent else "wazne"
        return {"category":"zapytanie","priority":prio,"summary":subject,"action_needed":True,"action_desc":"Odpowiedz na zapytanie","reply_approve":None,"reply_reject":None}
    if any(w in s for w in ["zamawiam","skladam zamowienie","potwierdzam zamowienie","numer zamowienia",
                            "status zamowienia","zamowienie nr","order","wysylka","paczka","przesylka"]):
        prio = "pilne" if is_urgent else "wazne"
        return {"category":"zamowienie","priority":prio,"summary":subject,"action_needed":False,"action_desc":None,"reply_approve":None,"reply_reject":None}
    if any(w in sndr for w in ["newsletter","noreply","no-reply","marketing","promo","info@"]):
        return {"category":"spam","priority":"moze_poczekac","summary":subject,"action_needed":False,"action_desc":None,"reply_approve":None,"reply_reject":None}
    return {"category":"inne","priority":"moze_poczekac","summary":subject,"action_needed":False,"action_desc":None,"reply_approve":None,"reply_reject":None}
def _analyze_invoice(claude, att, sender, subject) -> Optional[dict]:
    """Analizuje PDF faktury i wyciąga dane."""
    try:
        b64 = base64.standard_b64encode(att["content"]).decode()
        ct  = att["content_type"]
        mt  = "image/jpeg" if ct == "image/jpg" else ct
        blk = {"type": "document",
               "source": {"type": "base64", "media_type": mt, "data": b64}} \
              if ct == "application/pdf" \
              else {"type": "image",
                    "source": {"type": "base64", "media_type": mt, "data": b64}}
        prompt = f"""Przeanalizuj fakturę. Zwróć TYLKO JSON bez markdown:
{{"vendor":"nazwa","invoice_number":"numer","date":"YYYY-MM-DD","due_date":"YYYY-MM-DD",
"amount_net":0,"amount_gross":0,"vat":0,"vat_rate":23,
"category":"IT/Marketing/Biuro/Uslugi/Inne",
"description":"opis","currency":"PLN","is_cost_deductible":true,"confidence":"high/medium/low"}}
Od: {sender} | Temat: {subject} | Plik: {att['filename']}"""
        r = claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=500,
            messages=[{"role": "user",
                        "content": [blk, {"type": "text", "text": prompt}]}])
        data = json.loads(
            r.content[0].text.replace("```json", "").replace("```", "").strip())
        data["source_email"] = sender
        data["filename"]     = att["filename"]
        return data
    except Exception as e:
        print(f"[AI] Błąd analizy faktury: {e}")
        return None

def _prepare_invoice_db(inv, client_email, sender, subject, filename) -> dict:
    db = {
        "client_email":       client_email,
        "vendor":             str(inv.get("vendor") or "")[:255],
        "amount_net":         _to_float(inv.get("amount_net")),
        "amount_gross":       _to_float(inv.get("amount_gross")),
        "vat":                _to_float(inv.get("vat")),
        "vat_rate":           _to_float(inv.get("vat_rate")),
        "category":           str(inv.get("category") or "Inne")[:50],
        "description":        str(inv.get("description") or "")[:500],
        "currency":           str(inv.get("currency") or "PLN")[:10],
        "is_cost_deductible": bool(inv.get("is_cost_deductible", False)),
        "confidence":         str(inv.get("confidence") or "medium")[:20],
        "source_email":       str(sender)[:255],
        "source_subject":     str(subject)[:500],
        "filename":           str(filename)[:255],
        "status":             "ok",
    }
    inv_num = str(inv.get("invoice_number") or "")[:100]
    if inv_num: db["invoice_number"] = inv_num
    d = _to_date(inv.get("date"))
    if d: db["date"] = d
    dd = _to_date(inv.get("due_date"))
    if dd: db["due_date"] = dd
    return db

def _clean_message_id(val: str) -> str:
    """Czyści Message-ID emaila — usuwa nawiasy trójkątne i białe znaki."""
    return val.strip().strip("<>").strip()[:500] if val else ""

def _to_float(val):
    try: return float(val) if val is not None else 0.0
    except: return 0.0

def _to_date(val):
    if not val: return None
    s = str(val).strip()
    # Już ISO datetime
    if re.match(r"\d{4}-\d{2}-\d{2}T", s): return s[:19]
    # Już sama data
    if re.match(r"\d{4}-\d{2}-\d{2}$", s): return s
    # Format IMAP: "Mon, 30 Jun 2025 14:23:45 +0200"
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(s)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    return None
