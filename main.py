"""
KsięgoBot Backend — FastAPI + IMAP + Claude AI
Uruchom lokalnie: uvicorn main:app --reload
Na Render.com: automatyczne uruchomienie przez Procfile
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
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── APP ──
app = FastAPI(title="KsięgoBot API", version="1.0.0")

# Pozwól na połączenia z dowolnej domeny (w produkcji ogranicz do swojej domeny)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── MODELE DANYCH ──
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

# ── KLIENT ANTHROPIC ──
def get_claude():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Brak klucza ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=api_key)


# ─────────────────────────────────────────────
# ENDPOINT: Test połączenia IMAP
# ─────────────────────────────────────────────
@app.post("/api/imap/test")
def test_imap(config: ImapConfig):
    """Sprawdza czy dane IMAP są poprawne i skrzynka dostępna."""
    try:
        if config.use_ssl:
            mail = imaplib.IMAP4_SSL(config.host, config.port)
        else:
            mail = imaplib.IMAP4(config.host, config.port)

        mail.login(config.username, config.password)
        status, folders = mail.list()
        mail.logout()

        return {
            "success": True,
            "message": f"Połączenie z {config.host} udane. Skrzynka dostępna.",
            "email": config.username
        }
    except imaplib.IMAP4.error as e:
        raise HTTPException(status_code=401, detail=f"Błąd logowania IMAP: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Błąd połączenia: {str(e)}")


# ─────────────────────────────────────────────
# ENDPOINT: Skanuj skrzynkę i analizuj faktury
# ─────────────────────────────────────────────
@app.post("/api/scan")
def scan_mailbox(req: ScanRequest):
    """
    Łączy się ze skrzynką IMAP, szuka emaili z załącznikami PDF/JPG/PNG,
    analizuje je przez Claude AI i zwraca listę faktur.
    """
    config = req.imap
    invoices = []
    errors = []

    # 1. Połącz z IMAP
    try:
        if config.use_ssl:
            mail = imaplib.IMAP4_SSL(config.host, config.port)
        else:
            mail = imaplib.IMAP4(config.host, config.port)
        mail.login(config.username, config.password)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Błąd połączenia IMAP: {str(e)}")

    try:
        # 2. Wybierz folder
        mail.select(config.folder)

        # 3. Zbuduj zapytanie IMAP
        since_date = (datetime.now() - timedelta(days=config.days_back)).strftime("%d-%b-%Y")
        search_criteria = f'(SINCE "{since_date}")'

        # Dodaj filtr słów kluczowych jeśli podano
        if config.keywords:
            keywords = [k.strip() for k in config.keywords.split(",") if k.strip()]
            if keywords:
                # Szukaj emaili zawierających którekolwiek słowo kluczowe w temacie
                keyword_criteria = " ".join([f'(SUBJECT "{kw}")' for kw in keywords[:3]])
                search_criteria = f'(SINCE "{since_date}" OR {keyword_criteria} {keyword_criteria})'

        # 4. Wyszukaj maile
        status, message_ids = mail.search(None, search_criteria)
        if status != "OK":
            raise Exception("Błąd wyszukiwania emaili")

        ids = message_ids[0].split()
        # Ogranicz do ostatnich 50 emaili żeby nie przeciążyć
        ids = ids[-50:]

        claude = get_claude()

        # 5. Przetwórz każdy email
        for msg_id in ids:
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Dekoduj temat
                subject = _decode_header(msg.get("Subject", ""))
                sender = msg.get("From", "")
                date_str = msg.get("Date", "")

                # 6. Znajdź załączniki PDF/JPG/PNG
                attachments = _get_attachments(msg)
                if not attachments:
                    continue

                # 7. Analizuj każdy załącznik przez Claude
                for att in attachments:
                    invoice = _analyze_attachment_with_claude(
                        claude=claude,
                        filename=att["filename"],
                        content=att["content"],
                        content_type=att["content_type"],
                        email_from=sender,
                        email_subject=subject,
                        email_date=date_str,
                    )
                    if invoice:
                        invoices.append(invoice)

            except Exception as e:
                errors.append(f"Błąd przetwarzania emaila {msg_id}: {str(e)}")
                continue

        mail.logout()

    except Exception as e:
        try:
            mail.logout()
        except:
            pass
        raise HTTPException(status_code=500, detail=f"Błąd skanowania: {str(e)}")

    return {
        "success": True,
        "scanned_emails": len(ids),
        "invoices_found": len(invoices),
        "invoices": invoices,
        "errors": errors,
        "scanned_at": datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────
# ENDPOINT: Analiza pojedynczego pliku (upload)
# ─────────────────────────────────────────────
@app.post("/api/analyze")
async def analyze_file(payload: dict):
    """
    Przyjmuje plik jako base64 i analizuje go przez Claude.
    payload: { filename, content_base64, content_type }
    """
    try:
        claude = get_claude()
        content = base64.b64decode(payload["content_base64"])
        invoice = _analyze_attachment_with_claude(
            claude=claude,
            filename=payload.get("filename", "faktura"),
            content=content,
            content_type=payload.get("content_type", "application/pdf"),
            email_from="",
            email_subject="",
            email_date="",
        )
        return {"success": True, "invoice": invoice}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# ENDPOINT: Zapytaj AI o faktury
# ─────────────────────────────────────────────
class ChatRequest(BaseModel):
    question: str
    invoices: list = []

@app.post("/api/chat")
def chat(req: ChatRequest):
    """Odpowiada na pytania użytkownika o jego faktury."""
    claude = get_claude()

    context = ""
    if req.invoices:
        total = sum(i.get("amount_gross", 0) for i in req.invoices)
        vat_total = sum(i.get("vat", 0) for i in req.invoices)
        context = f"""
Dane finansowe firmy:
- Łączna wartość faktur: {total:.2f} zł
- Łączny VAT do odliczenia: {vat_total:.2f} zł
- Liczba faktur: {len(req.invoices)}

Lista faktur:
{json.dumps(req.invoices, ensure_ascii=False, indent=2)}
"""
    else:
        context = "Brak faktur w systemie. Poinformuj użytkownika że powinien połączyć skrzynkę i uruchomić skanowanie."

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""Jesteś profesjonalnym asystentem księgowym dla małej firmy. 
Odpowiadaj po polsku, konkretnie i rzeczowo.
Nie udzielasz porad prawnych ani podatkowych — informujesz o danych.

{context}

Pytanie użytkownika: {req.question}"""
        }]
    )

    return {
        "success": True,
        "answer": response.content[0].text,
        "tokens_used": response.usage.output_tokens
    }


# ─────────────────────────────────────────────
# ENDPOINT: Health check (Render.com tego wymaga)
# ─────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "KsięgoBot API", "version": "1.0.0"}

@app.get("/health")
def health_check():
    return {"status": "ok"}


# ─────────────────────────────────────────────
# FUNKCJE POMOCNICZE
# ─────────────────────────────────────────────

def _decode_header(header_value: str) -> str:
    """Dekoduje nagłówek emaila (obsługuje UTF-8, ISO-8859-2 itp.)"""
    if not header_value:
        return ""
    try:
        decoded_parts = decode_header(header_value)
        result = ""
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result += part.decode(charset or "utf-8", errors="replace")
            else:
                result += str(part)
        return result
    except:
        return str(header_value)


def _get_attachments(msg) -> list:
    """Wyciąga załączniki PDF/JPG/PNG z emaila."""
    attachments = []
    for part in msg.walk():
        content_type = part.get_content_type()
        content_disposition = str(part.get("Content-Disposition", ""))

        # Sprawdź czy to załącznik z obsługiwanym typem
        is_pdf = content_type == "application/pdf"
        is_image = content_type in ["image/jpeg", "image/png", "image/jpg"]
        is_attachment = "attachment" in content_disposition

        if (is_pdf or is_image) and is_attachment:
            try:
                filename = part.get_filename() or f"attachment.{content_type.split('/')[1]}"
                filename = _decode_header(filename)
                content = part.get_payload(decode=True)

                if content and len(content) > 100:  # Ignoruj puste pliki
                    attachments.append({
                        "filename": filename,
                        "content": content,
                        "content_type": content_type,
                    })
            except:
                continue

    return attachments


def _analyze_attachment_with_claude(
    claude, filename: str, content: bytes,
    content_type: str, email_from: str,
    email_subject: str, email_date: str
) -> Optional[dict]:
    """
    Wysyła załącznik do Claude API i parsuje odpowiedź jako dane faktury.
    Claude obsługuje zarówno PDF jak i obrazy natywnie.
    """
    try:
        # Zakoduj plik do base64
        content_b64 = base64.standard_b64encode(content).decode("utf-8")

        # Sprawdź typ dla API
        if content_type == "application/pdf":
            media_type = "application/pdf"
            doc_type = "document"
        elif content_type in ["image/jpeg", "image/jpg"]:
            media_type = "image/jpeg"
            doc_type = "image"
        elif content_type == "image/png":
            media_type = "image/png"
            doc_type = "image"
        else:
            return None

        # Zbuduj wiadomość do Claude
        if doc_type == "document":
            file_content = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": content_b64
                }
            }
        else:
            file_content = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": content_b64
                }
            }

        prompt = f"""Przeanalizuj ten dokument. To prawdopodobnie faktura lub rachunek.
Kontekst emaila:
- Od: {email_from}
- Temat: {email_subject}
- Data emaila: {email_date}
- Nazwa pliku: {filename}

Zwróć TYLKO JSON bez żadnego dodatkowego tekstu, bez markdown, bez backticks:
{{
  "vendor": "nazwa dostawcy/wystawcy faktury",
  "invoice_number": "numer faktury lub null",
  "date": "data faktury YYYY-MM-DD lub null",
  "due_date": "termin płatności YYYY-MM-DD lub null",
  "amount_net": wartość netto jako liczba lub 0,
  "amount_gross": wartość brutto jako liczba lub 0,
  "vat": kwota VAT jako liczba lub 0,
  "vat_rate": stawka VAT jako liczba np. 23 lub null,
  "category": "IT/Marketing/Biuro/Usługi/Inne",
  "description": "krótki opis 1 zdanie",
  "currency": "PLN/EUR/USD/GBP",
  "is_cost_deductible": true lub false,
  "confidence": "high/medium/low"
}}"""

        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [file_content, {"type": "text", "text": prompt}]
            }]
        )

        raw = response.content[0].text.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)

        # Dodaj metadane emaila
        data["source_email"] = email_from
        data["source_subject"] = email_subject
        data["filename"] = filename
        data["processed_at"] = datetime.now().isoformat()
        data["status"] = "ok"

        return data

    except json.JSONDecodeError:
        # Jeśli Claude nie zwrócił JSON, zwróć podstawowe dane
        return {
            "vendor": email_from.split("@")[0] if email_from else "Nieznany",
            "filename": filename,
            "source_email": email_from,
            "amount_gross": 0,
            "vat": 0,
            "category": "Inne",
            "description": f"Nie udało się automatycznie przetworzyć: {filename}",
            "status": "error",
            "processed_at": datetime.now().isoformat(),
        }
    except Exception as e:
        return None
