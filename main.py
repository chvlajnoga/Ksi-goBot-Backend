"""
KsięgoBot Backend — FastAPI + IMAP + Claude AI + Supabase
"""

import imaplib
import email
import base64
import os
import json
from email.header import decode_header
from datetime import datetime, timedelta
from typing import Optional

import anthropic
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── APP ──
app = FastAPI(title="KsięgoBot API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── CONFIG ──
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "https://elfibvwjskmíaugxjckf.supabase.co")
SUPABASE_SECRET  = os.environ.get("SUPABASE_SECRET_KEY", "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")

# ── SUPABASE HELPER ──
def sb_headers():
    return {
        "apikey": SUPABASE_SECRET,
        "Authorization": f"Bearer {SUPABASE_SECRET}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

async def sb_insert(table: str, data: dict):
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=sb_headers(),
            json=data,
            timeout=10,
        )
        return r.json()

async def sb_select(table: str, filters: str = ""):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}?{filters}&order=created_at.desc",
            headers=sb_headers(),
            timeout=10,
        )
        return r.json()

async def sb_delete(table: str, filters: str):
    async with httpx.AsyncClient() as client:
        r = await client.delete(
            f"{SUPABASE_URL}/rest/v1/{table}?{filters}",
            headers=sb_headers(),
            timeout=10,
        )
        return r.status_code

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
    client_email: Optional[str] = ""

class ChatRequest(BaseModel):
    question: str
    client_email: Optional[str] = ""
    invoices: list = []

# ─────────────────────────────────────────────
# SETUP — Utwórz tabele w Supabase (wywołaj raz)
# ─────────────────────────────────────────────
@app.post("/api/setup-db")
async def setup_db():
    """
    Tworzy tabele w Supabase przez SQL Editor.
    Wywołaj raz po podpięciu bazy.
    """
    sql = """
    CREATE TABLE IF NOT EXISTS invoices (
        id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
        client_email TEXT NOT NULL,
        vendor TEXT,
        invoice_number TEXT,
        date DATE,
        due_date DATE,
        amount_net NUMERIC(12,2) DEFAULT 0,
        amount_gross NUMERIC(12,2) DEFAULT 0,
        vat NUMERIC(12,2) DEFAULT 0,
        vat_rate NUMERIC(5,2),
        category TEXT DEFAULT 'Inne',
        description TEXT,
        currency TEXT DEFAULT 'PLN',
        is_cost_deductible BOOLEAN DEFAULT false,
        confidence TEXT DEFAULT 'medium',
        source_email TEXT,
        source_subject TEXT,
        filename TEXT,
        status TEXT DEFAULT 'ok',
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS imap_configs (
        id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
        client_email TEXT UNIQUE NOT NULL,
        imap_host TEXT NOT NULL,
        imap_port INTEGER DEFAULT 993,
        use_ssl BOOLEAN DEFAULT true,
        folder TEXT DEFAULT 'INBOX',
        last_scan TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS scan_logs (
        id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
        client_email TEXT NOT NULL,
        scanned_emails INTEGER DEFAULT 0,
        invoices_found INTEGER DEFAULT 0,
        status TEXT DEFAULT 'ok',
        message TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
    return {
        "success": True,
        "message": "Uruchom poniższy SQL w Supabase SQL Editor (Database → SQL Editor)",
        "sql": sql
    }

# ─────────────────────────────────────────────
# IMAP TEST
# ─────────────────────────────────────────────
@app.post("/api/imap/test")
def test_imap(config: ImapConfig):
    try:
        if config.use_ssl:
            mail = imaplib.IMAP4_SSL(config.host, config.port)
        else:
            mail = imaplib.IMAP4(config.host, config.port)
        mail.login(config.username, config.password)
        mail.logout()
        return {"success": True, "message": f"Połączenie z {config.host} udane.", "email": config.username}
    except imaplib.IMAP4.error as e:
        raise HTTPException(status_code=401, detail=f"Błąd logowania: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Błąd połączenia: {str(e)}")

# ─────────────────────────────────────────────
# SCAN — Skanuj skrzynkę i zapisz do Supabase
# ─────────────────────────────────────────────
@app.post("/api/scan")
async def scan_mailbox(req: ScanRequest):
    config = req.imap
    invoices = []
    errors = []

    # Połącz z IMAP
    try:
        if config.use_ssl:
            mail = imaplib.IMAP4_SSL(config.host, config.port)
        else:
            mail = imaplib.IMAP4(config.host, config.port)
        mail.login(config.username, config.password)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Błąd IMAP: {str(e)}")

    try:
        mail.select(config.folder)
        since_date = (datetime.now() - timedelta(days=config.days_back)).strftime("%d-%b-%Y")
        status, message_ids = mail.search(None, f'(SINCE "{since_date}")')
        ids = message_ids[0].split()[-50:]

        claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        for msg_id in ids:
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                subject = _decode_header(msg.get("Subject", ""))
                sender  = msg.get("From", "")
                attachments = _get_attachments(msg)
                if not attachments:
                    continue
                for att in attachments:
                    inv = _analyze_with_claude(claude, att, sender, subject)
                    if inv:
                        inv["client_email"] = config.username
                        # Zapisz do Supabase
                        try:
                            await sb_insert("invoices", inv)
                        except Exception as db_err:
                            errors.append(f"DB błąd: {str(db_err)}")
                        invoices.append(inv)
            except Exception as e:
                errors.append(str(e))

        mail.logout()

        # Zapisz log skanowania
        try:
            await sb_insert("scan_logs", {
                "client_email": config.username,
                "scanned_emails": len(ids),
                "invoices_found": len(invoices),
                "status": "ok",
                "message": f"Skanowanie zakończone. Emaili: {len(ids)}, Faktur: {len(invoices)}"
            })
        except:
            pass

    except Exception as e:
        try: mail.logout()
        except: pass
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "success": True,
        "scanned_emails": len(ids),
        "invoices_found": len(invoices),
        "invoices": invoices,
        "errors": errors,
        "scanned_at": datetime.now().isoformat(),
    }

# ─────────────────────────────────────────────
# GET — Pobierz faktury z Supabase
# ─────────────────────────────────────────────
@app.get("/api/invoices/{client_email}")
async def get_invoices(client_email: str):
    try:
        data = await sb_select("invoices", f"client_email=eq.{client_email}")
        return {"success": True, "invoices": data, "count": len(data)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────
# DELETE — Usuń fakturę
# ─────────────────────────────────────────────
@app.delete("/api/invoices/{invoice_id}")
async def delete_invoice(invoice_id: str):
    try:
        await sb_delete("invoices", f"id=eq.{invoice_id}")
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────
# CHAT
# ─────────────────────────────────────────────
@app.post("/api/chat")
async def chat(req: ChatRequest):
    claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # Pobierz faktury z bazy jeśli nie przesłano
    invoices = req.invoices
    if not invoices and req.client_email:
        try:
            data = await sb_select("invoices", f"client_email=eq.{req.client_email}")
            invoices = data
        except:
            pass

    context = f"Dane faktur: {json.dumps(invoices, ensure_ascii=False)}" if invoices else "Brak faktur."

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": f"Jesteś asystentem księgowym. Odpowiadaj po polsku.\n{context}\n\nPytanie: {req.question}"}]
    )
    return {"success": True, "answer": response.content[0].text}

# ─────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "KsięgoBot API", "version": "2.0.0"}

@app.get("/health")
def health():
    return {"status": "ok"}

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _decode_header(val):
    try:
        parts = decode_header(val)
        return "".join(p.decode(c or "utf-8", errors="replace") if isinstance(p, bytes) else str(p) for p, c in parts)
    except:
        return str(val)

def _get_attachments(msg):
    result = []
    for part in msg.walk():
        ct = part.get_content_type()
        cd = str(part.get("Content-Disposition", ""))
        if ct in ("application/pdf", "image/jpeg", "image/png", "image/jpg") and "attachment" in cd:
            try:
                content = part.get_payload(decode=True)
                if content and len(content) > 100:
                    result.append({"filename": _decode_header(part.get_filename() or "file"), "content": content, "content_type": ct})
            except:
                pass
    return result

def _analyze_with_claude(claude, att, sender, subject):
    try:
        b64 = base64.standard_b64encode(att["content"]).decode()
        ct  = att["content_type"]
        media_type = ct if ct != "image/jpg" else "image/jpeg"
        doc_block = {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}} if ct == "application/pdf" \
               else {"type": "image",    "source": {"type": "base64", "media_type": media_type, "data": b64}}

        prompt = f"""Przeanalizuj ten dokument. Zwróć TYLKO JSON bez markdown:
{{"vendor":"nazwa","invoice_number":"numer lub null","date":"YYYY-MM-DD lub null","due_date":"YYYY-MM-DD lub null",
"amount_net":0,"amount_gross":0,"vat":0,"vat_rate":23,"category":"IT/Marketing/Biuro/Usługi/Inne",
"description":"opis","currency":"PLN","is_cost_deductible":true,"confidence":"high/medium/low"}}
Email od: {sender} | Temat: {subject} | Plik: {att['filename']}"""

        r = claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=500,
            messages=[{"role": "user", "content": [doc_block, {"type": "text", "text": prompt}]}]
        )
        data = json.loads(r.content[0].text.replace("```json","").replace("```","").strip())
        data["source_email"]   = sender
        data["source_subject"] = subject
        data["filename"]       = att["filename"]
        data["status"]         = "ok"
        return data
    except:
        return {"vendor": sender.split("@")[0], "filename": att["filename"],
                "source_email": sender, "amount_gross": 0, "vat": 0,
                "category": "Inne", "status": "error"}
