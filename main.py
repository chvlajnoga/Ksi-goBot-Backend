"""
KsięgoBot Backend v3.0 — FastAPI + IMAP + Claude AI + Supabase
Pełna klasyfikacja emaili: faktury, zapytania, zamówienia, płatności
"""
import imaplib, email, base64, os, json, re
from email.header import decode_header
from datetime import datetime, timedelta
from typing import Optional

import anthropic, httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="KsięgoBot API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_SECRET = os.environ.get("SUPABASE_SECRET_KEY", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

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
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}&order=created_at.desc"
    async with httpx.AsyncClient() as c:
        r = await c.get(url, headers=sb_headers(), timeout=10)
    return r.json() if r.status_code == 200 else []

async def sb_exists(table: str, filters: str) -> bool:
    """Sprawdza czy rekord już istnieje w bazie."""
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}&limit=1"
    headers = {**sb_headers(), "Prefer": "count=exact"}
    async with httpx.AsyncClient() as c:
        r = await c.get(url, headers=headers, timeout=10)
    try:
        count = int(r.headers.get("content-range", "0/0").split("/")[-1])
        return count > 0
    except:
        data = r.json()
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

class ChatRequest(BaseModel):
    question: str
    client_email: Optional[str] = ""
    invoices: list = []

class ReplyRequest(BaseModel):
    imap: ImapConfig
    to: str
    subject: str
    body: str
    in_reply_to: Optional[str] = ""

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
def test_imap(config: ImapConfig):
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
async def scan_mailbox(req: ScanRequest):
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
        ids = ids_raw[0].split()[-100:]  # max 100 emaili
        print(f"[SCAN] Emaili do analizy: {len(ids)}")

        claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        skipped_duplicates = 0

        for msg_id in ids:
            try:
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = _decode_hdr(msg.get("Subject", ""))
                sender  = msg.get("From", "")
                date    = msg.get("Date", "")
                body    = _get_body(msg)
                atts    = _get_attachments(msg)

                # Pobierz Message-ID — unikalny identyfikator emaila
                message_id = _clean_message_id(msg.get("Message-ID", "") or msg.get("Message-Id", ""))
                if not message_id:
                    # Fallback: hash z tematu + daty + nadawcy
                    import hashlib
                    raw = f"{sender}{subject}{date}"
                    message_id = "hash-" + hashlib.md5(raw.encode()).hexdigest()

                print(f"[SCAN] Klasyfikuję: {subject[:60]} [{message_id[:30]}]")

                # SPRAWDŹ DUPLIKAT po Message-ID
                already_exists = await sb_exists(
                    "emails",
                    f"client_email=eq.{config.username}&message_id=eq.{message_id}"
                )
                if already_exists:
                    print(f"[SCAN] Pominięto duplikat (Message-ID): {subject[:50]}")
                    skipped_duplicates += 1
                    continue
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
                    "body":           str(body)[:3000],
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
async def get_emails(client_email: str, category: str = ""):
    filters = f"client_email=eq.{client_email}"
    if category:
        filters += f"&category=eq.{category}"
    data = await sb_select("emails", filters)
    return {"success": True, "emails": data, "count": len(data)}

@app.post("/api/emails/reclassify/{client_email:path}")
async def reclassify_emails(client_email: str):
    """Ponownie klasyfikuje juz zapisane maile (kategoria + priorytet) wg aktualnych zasad AI.
    Potrzebne bo /api/scan pomija juz zapisane maile jako duplikaty i nigdy ich nie przeklasyfikuje."""
    claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    emails = await sb_select("emails", f"client_email=eq.{client_email}")
    updated, errors = 0, []
    for em in emails:
        try:
            atts = [{"content_type": "application/pdf"}] if em.get("has_attachment") else []
            classification = _classify_email(
                claude, em.get("subject") or "", em.get("sender") or "",
                em.get("body") or "", atts, em.get("date")
            )
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
    return {"success": True, "updated": updated, "total": len(emails), "errors": errors}

@app.delete("/api/emails/delete/{email_id}")
async def delete_email(email_id: str):
    r = await sb_delete("emails", f"id=eq.{email_id}")
    return {"success": r.status_code in (200, 204)}

@app.get("/api/invoices/{client_email:path}")
async def get_invoices(client_email: str):
    data = await sb_select("invoices", f"client_email=eq.{client_email}")
    return {"success": True, "invoices": data, "count": len(data)}

@app.delete("/api/invoices/{invoice_id}")
async def delete_invoice(invoice_id: str):
    r = await sb_delete("invoices", f"id=eq.{invoice_id}")
    return {"success": r.status_code in (200, 204)}

@app.get("/api/inquiries/{client_email:path}")
async def get_inquiries(client_email: str):
    data = await sb_select("inquiries", f"client_email=eq.{client_email}")
    return {"success": True, "inquiries": data, "count": len(data)}

@app.post("/api/chat")
async def chat(req: ChatRequest):
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

SMTP_PRESETS = {
    "imap.gmail.com":             ("smtp.gmail.com", 587),
    "outlook.office365.com":      ("smtp.office365.com", 587),
    "imap.wp.pl":                 ("smtp.wp.pl", 587),
    "imap.poczta.onet.pl":        ("smtp.poczta.onet.pl", 465),
    "poczta.interia.pl":          ("poczta.interia.pl", 587),
}

@app.post("/api/emails/reply")
async def send_reply(req: ReplyRequest):
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    imap_host = req.imap.host.lower()
    smtp_host, smtp_port = SMTP_PRESETS.get(imap_host, (imap_host.replace("imap.", "smtp."), 587))

    msg = MIMEMultipart("alternative")
    msg["From"]    = req.imap.username
    msg["To"]      = req.to
    msg["Subject"] = req.subject if req.subject.startswith("Re:") else f"Re: {req.subject}"
    if req.in_reply_to:
        msg["In-Reply-To"] = req.in_reply_to
        msg["References"]  = req.in_reply_to
    msg.attach(MIMEText(req.body, "plain", "utf-8"))

    try:
        if smtp_port == 465:
            import ssl
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=15) as s:
                s.login(req.imap.username, req.imap.password)
                s.sendmail(req.imap.username, req.to, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
                s.starttls()
                s.login(req.imap.username, req.imap.password)
                s.sendmail(req.imap.username, req.to, msg.as_string())
        print(f"[SMTP] Wysłano odpowiedź do {req.to}")
        return {"success": True, "message": f"Odpowiedź wysłana do {req.to}"}
    except Exception as e:
        print(f"[SMTP] Błąd: {e}")
        raise HTTPException(status_code=500, detail=f"Błąd wysyłki: {str(e)}")

# ── HELPERS ──
def _decode_hdr(val):
    try:
        parts = decode_header(val)
        return "".join(
            p.decode(c or "utf-8", errors="replace")
            if isinstance(p, bytes) else str(p)
            for p, c in parts)
    except:
        return str(val)

def _get_body(msg) -> str:
    """Wyciąga tekst z emaila (plain text lub HTML)."""
    body = ""
    try:
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body += payload.decode("utf-8", errors="replace")[:2000]
                    break
        if not body:
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        text = payload.decode("utf-8", errors="replace")
                        # Usuń tagi HTML
                        text = re.sub(r"<[^>]+>", " ", text)
                        body = text[:2000]
                        break
    except: pass
    return body[:1500]

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
        '"reply_reject":"pelna formalna odpowiedz NEGATYWNA z powitaniem i pozegnaniem (null dla spam/inne)"}\n'
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
        '"spam"=reklama/newsletter, "inne"=reszta.\n'
        'W razie wątpliwości między zapytanie a zamowienie: jeśli w treści jest jakiekolwiek pytanie dotyczące transakcji '
        '— wybierz "zapytanie". Wybierz "zamowienie" TYLKO gdy nie ma już nic do ustalenia, klient po prostu zamawia/potwierdza.\n'
        '\n'
        'USTALANIE PRIORYTETU (priority) — oceń NIEZALEŻNIE od kategorii, na podstawie realnej pilności sprawy '
        'dla właściciela skrzynki. Przeczytaj CAŁĄ treść ze zrozumieniem, nie zgaduj po pojedynczych słowach:\n'
        '- "pilne" (czerwony) — wymaga reakcji NATYCHMIAST: faktura z terminem płatności dziś/jutro lub już przeterminowana, '
        'wezwanie do zapłaty/windykacja, poważna reklamacja lub wyraźnie zdenerwowany/niezadowolony klient, '
        'klient grożący rezygnacją lub krokami prawnymi, wiadomość z dopiskiem "pilne"/"ASAP"/"natychmiast", '
        'każda sytuacja grożąca realną stratą pieniędzy, klienta lub reputacji.\n'
        '- "wazne" (żółty) — wymaga reakcji, ale nie od razu: standardowe zapytania ofertowe/cenowe, '
        'nowe zamówienia oczekujące na potwierdzenie, faktura z odległym (kilkudniowym+) terminem płatności, '
        'zwykła reklamacja bez oznak eskalacji, sprawy z rozsądnym terminem na odpowiedź.\n'
        '- "moze_poczekac" (zielony) — czysto informacyjne, BEZ wymaganej pilnej akcji: potwierdzenia otrzymania, '
        'powiadomienia o wysyłce/statusie bez problemu, spam/newslettery, luźna korespondencja, "inne".\n'
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
        f'Od: {sender[:80]} | Temat: {subject[:120]} | PDF: {has_pdf} | Tresc: {body[:600]}'
    )
    try:
        r = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}])
        raw = r.content[0].text.replace("```json","").replace("```","").strip()
        result = json.loads(raw)
    except Exception as e:
        print(f"[AI] Blad klasyfikacji: {e}")
        return _classify_by_keywords(subject, sender, has_pdf, body)

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
        f'Od: {sender[:80]} | Temat: {subject[:120]} | Treść: {body[:600]}'
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
