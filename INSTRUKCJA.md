# KsięgoBot Backend — Instrukcja wdrożenia

## Pliki w tym folderze
- `main.py` — serwer API (FastAPI + IMAP + Claude)
- `requirements.txt` — biblioteki Python
- `Procfile` — instrukcja uruchomienia dla Render.com

---

## Krok 1 — Załóż konto GitHub (jeśli nie masz)
Wejdź na https://github.com i zarejestruj się za darmo.

## Krok 2 — Wgraj pliki na GitHub
1. Kliknij "New repository" → nazwa: `ksiegobot-backend` → Public → Create
2. Kliknij "uploading an existing file"
3. Przeciągnij wszystkie 3 pliki (main.py, requirements.txt, Procfile)
4. Kliknij "Commit changes"

## Krok 3 — Załóż konto Render.com
Wejdź na https://render.com → "Get Started for Free" → zaloguj przez GitHub

## Krok 4 — Wdróż backend
1. Kliknij "New +" → "Web Service"
2. Wybierz repozytorium `ksiegobot-backend`
3. Wypełnij:
   - Name: `ksiegobot-api`
   - Environment: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Kliknij "Advanced" → "Add Environment Variable":
   - Key: `ANTHROPIC_API_KEY`
   - Value: (Twój klucz z console.anthropic.com)
5. Kliknij "Create Web Service"

## Krok 5 — Skopiuj URL
Po 2-3 minutach Render wyświetli URL w stylu:
`https://ksiegobot-api.onrender.com`

Ten URL wklejasz w panelu agenta (pole "Adres serwera API").

---

## Jak zdobyć klucz ANTHROPIC_API_KEY
1. Wejdź na https://console.anthropic.com
2. Zaloguj się lub zarejestruj
3. Kliknij "API Keys" → "Create Key"
4. Skopiuj klucz (zaczyna się od `sk-ant-...`)

---

## Testowanie
Po wdrożeniu wejdź na:
`https://TWOJ-URL.onrender.com/health`

Powinieneś zobaczyć: `{"status":"ok"}`

---

## Koszt
- Render.com Free Plan: **0 zł/mies.** (usypia po 15 min bezczynności, budzi się przy żądaniu)
- Render.com Starter: **~28 zł/mies.** (działa 24/7 — polecane gdy masz klientów)
- Anthropic API: płacisz za użycie (~0.01-0.05 zł za analizę jednej faktury)
