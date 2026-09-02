# Supra Hospital AI Assistant

A hospital knowledge assistant for Supra Multi-Specialty Hospital, Hyderabad. It answers clinical and operational questions from the hospital's own protocols, patient standing orders and incident-derived rules — scoped to who is asking, and with deterministic safety checks the language model cannot override.

The design position: **the language model is the least trustworthy component, so it is given the least authority.** It phrases answers. Clearance, relevance, answerability and drug safety are all decided in Python, from database records, before it is called.

---

## Stack

| Tier | Technology | Port |
|---|---|---|
| Client | Static HTML + JS (VS Code Live Server) | 5500 |
| API | FastAPI / Python 3.13 | 8000 |
| Data | Supabase Postgres | — |
| Model | Gemini 3.7 Flash (server-side) | — |

The Gemini and Supabase keys never reach the browser.

---

## Setup

### 1. Install

```bash
python -m venv .supai
.supai\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. Environment

Create `.env` in the project root:

```
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=<service role key>
GEMINI_API_KEY=<key from aistudio.google.com>
GEMINI_MODEL=gemini-3.7-flash
```

No quotes, no spaces around `=`.

### 3. Database

Run `migration.sql` in the Supabase SQL editor. It adds the governance columns (`safety_critical`, `patient`, `owner`, `banned_drugs`), the `can_prescribe` column on users, and the `query_audit` table. Safe to re-run.

Confirm the department strings match between the two tables — a mismatch fails silently and every departmental query returns only global records:

```sql
select distinct department from hospital_users;
select distinct department from hospital_knowledge;
```

### 4. Run

```bash
uvicorn main:app --reload          # from backend/
```

Then open `frontend/index.html` with Live Server on port 5500. Check `http://127.0.0.1:8000/` returns the status JSON, and `http://127.0.0.1:8000/docs` for the interactive API.

---

## How a request is handled

1. **Authorize** — the knowledge base is partitioned into released and withheld for this user. Withheld records are carried forward as metadata only, never content.
2. **Retrieve** — released records are scored lexically. A record must anchor on title, keywords or patient name to qualify; body-word matches add weight but cannot admit a record alone.
3. **Permission gate** — withheld records are scored on title and keywords. If the best withheld match beats the best released one, the request stops with a named refusal. The model is never called.
4. **Ambiguity gate** — a short query naming no patient, with several records qualifying, returns the list rather than picking one.
5. **Coverage gate** — below a top score of 8, the assistant states that Supra has no documented record and routes to the HOD. The model is never called.
6. **Safety interlock (pre)** — a named patient's standing order is injected from the record as a mandatory directive, so it reaches the user even if the model call fails.
7. **Generate** — Gemini receives only the released records, the directives, and a prompt requiring citation and forbidding general medical knowledge.
8. **Safety interlock (post)** — the output is sentence-split and scanned for drugs prohibited for that patient, skipping sentences containing negation cues. A genuine recommendation suppresses the answer.
9. **Audit** — written to `query_audit` as a background task, off the response path.

---

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Status and configured model |
| `/users` | GET | Staff roster for the role picker |
| `/ask` | POST | `{user_id, question}` → answer, sources, safety alerts, withheld records, guardrails fired |
| `/admin/reload-knowledge` | POST | Clears the in-process knowledge cache after editing records in Supabase |

The knowledge table is cached in memory. **After editing records in Supabase, hit `/admin/reload-knowledge` or restart uvicorn** — otherwise the change has no effect.

---

## Test queries

| Query | Role | Expected |
|---|---|---|
| What pain medication for a post-TKR patient? | Dr. Vikram | Paracetamol → Tramadol, no NSAIDs, cited |
| Patient Rajan has knee pain, what should I prescribe? | Dr. Vikram | Standing order first, then the ladder |
| Patient Rajan has knee pain, what should I prescribe? | Nurse Priya | Same, plus the prescriber note |
| When should I start DVT prophylaxis after surgery? | Dr. Vikram | Enoxaparin 40mg, 12h post-op, 14/28 days |
| What's our sepsis protocol? | Dr. Meera | v3, lactate within 1 hour, supersedes v2 |
| Tell me about Mrs. Padma's medication management | Dr. Meera | Ekadashi fasting, skip Glimepiride |
| ortho budget | Nurse Priya | Permission refusal, named |
| ortho budget | Dr. Vikram | Figures released |
| ortho budget | Dr. Ananya | Permission refusal — doctor, wrong department |
| ortho surgery | Dr. Meera | Ambiguity — records listed |
| What is our stroke thrombolysis protocol? | Dr. Vikram | No documented record |

---

## Known limitations

- **Auth is a dropdown.** No identity provider; every access-control claim is a demonstration, not an implementation.
- **The service key bypasses RLS.** Authorization is enforced in FastAPI, not the database. Production moves these rules into row-level security policies.
- **The coverage threshold of 8 is uncalibrated.** Lexical scores are not comparable across differently-worded questions. Chosen to fail toward refusing rather than answering, but it is a guess — there is no evaluation set to tune it against.
- **The ambiguity test is a token count**, a crude proxy for "the user has not said what they want". The right signal is the gap between the top two scores.
- **Keywords are hand-curated.** A doctor asking about "blood thinners" rather than anticoagulation may not surface the record they need. This is the most likely source of a false refusal.
- **No evaluation harness.** Answers were verified by reading them.
- **No EMR integration.** The Warfarin–NSAID rule is a document, not a live check against the patient's actual medication list.
- **The audit table is not tamper-evident** and has no retention policy.

---

## Files

```
backend/main.py      FastAPI application — authorization, retrieval, gates, interlocks
frontend/index.html  Client
migration.sql        Schema and seed metadata
.env                 Keys (not committed)
```

Design document: `Supra_Hospital_AI_Design_Document.docx` — architecture, safety model, problems found while building, roadmap.

---

Not a medical device. Assessment prototype. The assistant advises and never orders; no medication change leaves it without a prescriber's written authorisation.

---
Design document: [DESIGN.md](DESIGN.md) — architecture, safety model, problems found while building, roadmap.