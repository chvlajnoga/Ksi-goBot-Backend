"""
KsięgoBot Backend v2.1 — FastAPI + IMAP + Claude AI + Supabase
"""
import imaplib, email, base64, os, json, re
from email.header import decode_header
from datetime import datetime, timedelta
from typing import Optional

import anthropic, httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="KsięgoBot API", version="2.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

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
    print(f"[DB] POST {url}")
    print(f"[DB] Data: {json.dumps(data, ensure_ascii=False)[:400]}")
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=sb_headers(), json=data, timeout=15)
    print(f"[DB] Status: {r.status_code} | Response: {r.text[:300]}")
    return r

async def sb_select(table: str, filters: str = ""):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}&order=created_at.desc"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=sb_headers(), timeout=10)
    return r.json()

# ── MODELE ──
class ImapConfig(BaseModel):
    host: str
    port: int = 993
    use_ssl: bool = True
    username: str
    password: str
    folder: str = "INBOX"
    keywords: Optional[str] = ""
    days_back: int = 30

class ScanRequest(BaseModel):
    imap: ImapConfig

class ChatRequest(BaseModel):
    question: str
    client_email: Optional[str] = ""
    invoices: list = []

# ── ENDPOINTS ──
@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "supabase_url": SUPABASE_URL[:40] + "..." if SUPABASE_URL else "NOT SET"}

@app.post("/api/imap/test")
def test_imap(config: ImapConfig):
    try:
        mail = imaplib.IMAP4_SSL(config.host, config.port) if config.use_ssl else imaplib.IMAP4(config.host, config.port)
        mail.login(config.username, config.password)
        mail.logout()
        return {"success": True, "message": f"Polaczenie z {config.host} udane.", "email": config.username}
    except imaplib.IMAP4.error as e:
        raise HTTPException(status_code=401, detail=f"Blad logowania: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Blad polaczenia: {str(e)}")

@app.post("/api/scan")
async def scan_mailbox(req: ScanRequest):
    config = req.imap
    invoices, errors = [], []

    print(f"[SCAN] Start dla: {config.username}")
    print(f"[SCAN] SUPABASE_URL = {SUPABASE_URL[:50] if SUPABASE_URL else 'BRAK!'}")

    try:
        mail = imaplib.IMAP4_SSL(config.host, config.port) if config.use_ssl else imaplib.IMAP4(config.host, config.port)
        mail.login(config.username, config.password)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Blad IMAP: {str(e)}")

    try:
        mail.select(config.folder)
        since = (datetime.now() - timedelta(days=config.days_back)).strftime("%d-%b-%Y")
        _, ids_raw = mail.search(None, f'(SINCE "{since}")')
        ids = ids_raw[0].split()[-50:]
        print(f"[SCAN] Znaleziono emaili: {len(ids)}")

        claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        for msg_id in ids:
            try:
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                subject = _decode_hdr(msg.get("Subject", ""))
                sender  = msg.get("From", "")
                atts = _get_attachments(msg)
                if not atts:
                    continue
                print(f"[SCAN] Email z {len(atts)} zalacznikami od: {sender}")
                for att in atts:
                    inv = _analyze(claude, att, sender, subject)
                    if not inv:
                        continue
                    # Przygotuj dane do zapisu
                    db = {
                        "client_email":       config.username,
                        "vendor":             str(inv.get("vendor") or "")[:255],
                        "invoice_number":     str(inv.get("invoice_number") or "")[:100] or None,
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
                        "filename":           str(att["filename"])[:255],
                        "status":             "ok",
                        "date":               _to_date(inv.get("date")),
                        "due_date":           _to_date(inv.get("due_date")),
                    }
                    # Usuń None z dat (Supabase wymaga null nie "None")
                    for k in ("date", "due_date", "invoice_number"):
                        if db[k] is None:
                            db.pop(k)

                    print(f"[SCAN] Zapisuję: {db['vendor']} {db['amount_gross']} zl")
                    result = await sb_insert("invoices", db)
                    if result.status_code in (200, 201):
                        print(f"[SCAN] Zapisano OK")
                        inv["client_email"] = config.username
                        invoices.append(inv)
                    else:
                        err = f"DB blad {result.status_code}: {result.text[:200]}"
                        print(f"[SCAN] {err}")
                        errors.append(err)
            except Exception as e:
                print(f"[SCAN] Blad emaila: {e}")
                errors.append(str(e))

        mail.logout()

        # Log skanowania
        try:
            await sb_insert("scan_logs", {
                "client_email": config.username,
                "scanned_emails": len(ids),
                "invoices_found": len(invoices),
                "status": "ok",
                "message": f"Emaili: {len(ids)}, Faktur: {len(invoices)}"
            })
        except Exception as e:
            print(f"[SCAN] Blad logu: {e}")

    except Exception as e:
        try: mail.logout()
        except: pass
        raise HTTPException(status_code=500, detail=str(e))

    print(f"[SCAN] Gotowe. Faktur zapisanych: {len(invoices)}, Bledow: {len(errors)}")
    return {"success": True, "scanned_emails": len(ids), "invoices_found": len(invoices), "invoices": invoices, "errors": errors}

@app.get("/api/invoices/{client_email:path}")
async def get_invoices(client_email: str):
    data = await sb_select("invoices", f"client_email=eq.{client_email}")
    return {"success": True, "invoices": data, "count": len(data) if isinstance(data, list) else 0}

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
        messages=[{"role":"user","content":f"Jestes asystentem ksiegowym. Odpowiadaj po polsku.\n{ctx}\n\nPytanie: {req.question}"}])
    return {"success": True, "answer": r.content[0].text}

# ── HELPERS ──
def _decode_hdr(val):
    try:
        parts = decode_header(val)
        return "".join(p.decode(c or "utf-8", errors="replace") if isinstance(p, bytes) else str(p) for p,c in parts)
    except: return str(val)

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

def _to_float(val):
    try: return float(val) if val is not None else 0.0
    except: return 0.0

def _to_date(val):
    if not val: return None
    s = str(val)
    if re.match(r"\d{4}-\d{2}-\d{2}", s): return s[:10]
    return None

def _analyze(claude, att, sender, subject):
    try:
        b64 = base64.standard_b64encode(att["content"]).decode()
        ct  = att["content_type"]
        mt  = "image/jpeg" if ct == "image/jpg" else ct
        blk = {"type":"document","source":{"type":"base64","media_type":mt,"data":b64}} if ct=="application/pdf" \
         else {"type":"image",   "source":{"type":"base64","media_type":mt,"data":b64}}
        prompt = f"""Przeanalizuj ten dokument. Zwroc TYLKO JSON bez markdown:
{{"vendor":"nazwa","invoice_number":"numer lub null","date":"YYYY-MM-DD lub null","due_date":"YYYY-MM-DD lub null","amount_net":0,"amount_gross":0,"vat":0,"vat_rate":23,"category":"IT/Marketing/Biuro/Uslugi/Inne","description":"opis","currency":"PLN","is_cost_deductible":true,"confidence":"high/medium/low"}}
Od: {sender} | Temat: {subject} | Plik: {att['filename']}"""
        r = claude.messages.create(model="claude-sonnet-4-6", max_tokens=500,
            messages=[{"role":"user","content":[blk,{"type":"text","text":prompt}]}])
        data = json.loads(r.content[0].text.replace("```json","").replace("```","").strip())
        data["source_email"] = sender
        data["filename"] = att["filename"]
        return data
    except Exception as e:
        print(f"[AI] Blad analizy: {e}")
        return {"vendor": sender.split("@")[0], "filename": att["filename"],
                "source_email": sender, "amount_gross": 0, "vat": 0, "category": "Inne", "status": "error"}
