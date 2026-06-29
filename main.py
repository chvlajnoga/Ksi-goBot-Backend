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

class ImapConfig(BaseModel):
    host: str
    port: int = 993
    use_ssl: bool = True
    username: str
    password: str
    folder: str = "INBOX"
    days_back: int = 30

class ScanRequest(BaseModel):
    imap: ImapConfig

class ChatRequest(BaseModel):
    question: str
    client_email: Optional[str] = ""
    invoices: list = []

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
        mail.logout()
        return {"success": True, "message": "Połączenie udane.", "email": config.username}
    except imaplib.IMAP4.error as e:
        raise HTTPException(status_code=401, detail=f"Błąd logowania: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Błąd: {str(e)}")
        @app.post("/api/scan")
async def scan_mailbox(req: ScanRequest):
    config = req.imap
    results = {"faktury": [], "zapytania": [], "zamowienia": [],
               "platnosci": [], "spam": [], "inne": []}
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
        since = (datetime.now() - timedelta(days=config.days_back)).strftime("%d-%b-%Y")
        _, ids_raw = mail.search(None, f'(SINCE "{since}")')
        ids = ids_raw[0].split()[-100:]
        print(f"[SCAN] Emaili: {len(ids)}")
        claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        for msg_id in ids:
            try:
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                subject = _decode_hdr(msg.get("Subject", ""))
                sender  = msg.get("From", "")
                date    = msg.get("Date", "")
                body    = _get_body(msg)
                atts    = _get_attachments(msg)
                classification = _classify_email(claude, subject, sender, body, atts, date)
                category = classification.get("category", "inne")
                print(f"[SCAN] {category}: {subject[:50]}")
                email_record = {
                    "client_email":   config.username,
                    "category":       category,
                    "sender":         str(sender)[:255],
                    "subject":        str(subject)[:500],
                    "summary":        str(classification.get("summary", ""))[:1000],
                    "priority":       str(classification.get("priority", "normalny"))[:20],
                    "action_needed":  bool(classification.get("action_needed", False)),
                    "action_desc":    str(classification.get("action_desc", ""))[:500],
                    "has_attachment": len(atts) > 0,
                    "status":         "nowe",
                }
                d = _to_date(date)
                if d: email_record["date"] = d
                await sb_insert("emails", email_record)
                if category == "faktura" and atts:
                    for att in atts:
                        if att["content_type"] in ("application/pdf","image/jpeg","image/png"):
                            inv = _analyze_invoice(claude, att, sender, subject)
                            if inv:
                                db = _prepare_invoice_db(inv, config.username, sender, subject, att["filename"])
                                await sb_insert("invoices", db)
                                results["faktury"].append(inv)
                elif category == "zapytanie":
                    inq = {
                        "client_email":       config.username,
                        "sender":             str(sender)[:255],
                        "subject":            str(subject)[:500],
                        "summary":            str(classification.get("summary",""))[:1000],
                        "suggested_response": str(classification.get("suggested_response",""))[:2000],
                        "status":             "nowe",
                    }
                    if d: inq["date"] = d
                    await sb_insert("inquiries", inq)
                    results["zapytania"].append(inq)
                elif category == "zamowienie":
                    results["zamowienia"].append({"subject": subject, "sender": sender})
                elif category == "platnosc":
                    results["platnosci"].append({"subject": subject, "sender": sender})
                elif category == "spam":
                    results["spam"].append({"subject": subject})
                else:
                    results["inne"].append({"subject": subject, "sender": sender})
            except Exception as e:
                print(f"[SCAN] Błąd: {e}")
                errors.append(str(e))
        mail.logout()
        await sb_insert("scan_logs", {
            "client_email": config.username,
            "scanned_emails": len(ids),
            "invoices_found": len(results["faktury"]),
            "status": "ok",
            "message": f"Emaili:{len(ids)} Faktury:{len(results['faktury'])} Zapytania:{len(results['zapytania'])}"
        })
    except Exception as e:
        try: mail.logout()
        except: pass
        raise HTTPException(status_code=500, detail=str(e))
    return {"success": True, "scanned_emails": len(ids),
            "results": {k: len(v) for k,v in results.items()},
            "details": results, "errors": errors,
            "scanned_at": datetime.now().isoformat()}
    @app.get("/api/emails/{client_email:path}")
async def get_emails(client_email: str, category: str = ""):
    filters = f"client_email=eq.{client_email}"
    if category: filters += f"&category=eq.{category}"
    data = await sb_select("emails", filters)
    return {"success": True, "emails": data, "count": len(data)}

@app.get("/api/invoices/{client_email:path}")
async def get_invoices(client_email: str):
    data = await sb_select("invoices", f"client_email=eq.{client_email}")
    return {"success": True, "invoices": data, "count": len(data)}

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
            data = await sb_select("invoices", f"client_email=eq.{req.client_email}")
            invoices = data if isinstance(data, list) else []
        except: pass
    ctx = f"Faktury: {json.dumps(invoices, ensure_ascii=False)}" if invoices else "Brak faktur."
    r = claude.messages.create(model="claude-sonnet-4-6", max_tokens=800,
        messages=[{"role":"user","content":f"Jesteś asystentem księgowym. Odpowiadaj po polsku.\n{ctx}\n\nPytanie: {req.question}"}])
    return {"success": True, "answer": r.content[0].text}
    def _decode_hdr(val):
    try:
        parts = decode_header(val)
        return "".join(p.decode(c or "utf-8", errors="replace") if isinstance(p, bytes) else str(p) for p,c in parts)
    except: return str(val)

def _get_body(msg) -> str:
    body = ""
    try:
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")[:2000]
                    break
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = re.sub(r"<[^>]+>", " ", payload.decode("utf-8", errors="replace"))[:2000]
                        break
    except: pass
    return body[:1500]

def _get_attachments(msg):
    result = []
    for part in msg.walk():
        ct = part.get_content_type()
        cd = str(part.get("Content-Disposition",""))
        if ct in ("application/pdf","image/jpeg","image/png","image/jpg") and "attachment" in cd:
            try:
                content = part.get_payload(decode=True)
                if content and len(content) > 100:
                    result.append({"filename": _decode_hdr(part.get_filename() or "file"), "content": content, "content_type": ct})
            except: pass
    return result

def _classify_email(claude, subject, sender, body, atts, date) -> dict:
    has_pdf = any(a["content_type"] == "application/pdf" for a in atts)
    prompt = f"""Przeanalizuj email i zwróć TYLKO JSON bez markdown:
{{"category":"faktura|zapytanie|zamowienie|platnosc|spam|inne","priority":"wysoki|normalny|niski","summary":"1-2 zdania","action_needed":true/false,"action_desc":"co zrobić lub null","suggested_response":"propozycja odpowiedzi jeśli zapytanie lub null"}}
Kategorie: faktura=faktura VAT/rachunek, zapytanie=pytanie o cenę/ofertę, zamowienie=składanie zamówienia, platnosc=potwierdzenie przelewu, spam=reklama/newsletter, inne=reszta
Od: {sender} | Temat: {subject} | PDF: {has_pdf} | Treść: {body[:600]}"""
    try:
        r = claude.messages.create(model="claude-sonnet-4-6", max_tokens=400,
            messages=[{"role":"user","content":prompt}])
        return json.loads(r.content[0].text.replace("```json","").replace("```","").strip())
    except Exception as e:
        print(f"[AI] Błąd klasyfikacji: {e}")
        return {"category":"inne","priority":"normalny","summary":subject,"action_needed":False}

def _analyze_invoice(claude, att, sender, subject):
    try:
        b64 = base64.standard_b64encode(att["content"]).decode()
        ct = att["content_type"]
        mt = "image/jpeg" if ct == "image/jpg" else ct
        blk = {"type":"document","source":{"type":"base64","media_type":mt,"data":b64}} if ct=="application/pdf" \
         else {"type":"image","source":{"type":"base64","media_type":mt,"data":b64}}
        prompt = f"""Przeanalizuj fakturę. Zwróć TYLKO JSON bez markdown:
{{"vendor":"nazwa","invoice_number":"numer","date":"YYYY-MM-DD","due_date":"YYYY-MM-DD","amount_net":0,"amount_gross":0,"vat":0,"vat_rate":23,"category":"IT/Marketing/Biuro/Uslugi/Inne","description":"opis","currency":"PLN","is_cost_deductible":true,"confidence":"high/medium/low"}}
Od: {sender} | Temat: {subject} | Plik: {att['filename']}"""
        r = claude.messages.create(model="claude-sonnet-4-6", max_tokens=500,
            messages=[{"role":"user","content":[blk,{"type":"text","text":prompt}]}])
        data = json.loads(r.content[0].text.replace("```json","").replace("```","").strip())
        data["source_email"] = sender
        data["filename"] = att["filename"]
        return data
    except Exception as e:
        print(f"[AI] Błąd faktury: {e}")
        return None

def _prepare_invoice_db(inv, client_email, sender, subject, filename) -> dict:
    db = {
        "client_email": client_email,
        "vendor": str(inv.get("vendor") or "")[:255],
        "amount_net": _to_float(inv.get("amount_net")),
        "amount_gross": _to_float(inv.get("amount_gross")),
        "vat": _to_float(inv.get("vat")),
        "vat_rate": _to_float(inv.get("vat_rate")),
        "category": str(inv.get("category") or "Inne")[:50],
        "description": str(inv.get("description") or "")[:500],
        "currency": str(inv.get("currency") or "PLN")[:10],
        "is_cost_deductible": bool(inv.get("is_cost_deductible", False)),
        "confidence": str(inv.get("confidence") or "medium")[:20],
        "source_email": str(sender)[:255],
        "source_subject": str(subject)[:500],
        "filename": str(filename)[:255],
        "status": "ok",
    }
    inv_num = str(inv.get("invoice_number") or "")[:100]
    if inv_num: db["invoice_number"] = inv_num
    d = _to_date(inv.get("date"))
    if d: db["date"] = d
    dd = _to_date(inv.get("due_date"))
    if dd: db["due_date"] = dd
    return db

def _to_float(val):
    try: return float(val) if val is not None else 0.0
    except: return 0.0

def _to_date(val):
    if not val: return None
    s = str(val)
    if re.match(r"\d{4}-\d{2}-\d{2}", s): return s[:10]
    return None
