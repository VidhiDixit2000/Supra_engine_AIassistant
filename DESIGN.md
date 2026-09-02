# Supra Hospital AI Assistant — Design Document

**Supra Multi-Specialty Hospital, Hyderabad**

> **Deliverables:** working prototype (FastAPI + Supabase + Gemini + browser client) and this document.
> **Scope:** 15 supplied organisational records, 5 staff roles, 7 departments.
> **Stack:** Python 3.13 / FastAPI, Supabase Postgres, Gemini 3.7 Flash, static HTML + JS client.
> *Not a medical device. Assessment prototype only.*

---

## 1. What this system does

A doctor at Supra can already ask ChatGPT about medicine. What they cannot ask ChatGPT is anything about *Supra* — that Dr. Vikram removed NSAIDs from the post-knee-replacement pain ladder in January 2025, that patient Rajan has refused NSAIDs eight times because of a heart condition, or that Supra tightened its sepsis lactate window from three hours to one.

This system answers questions from the hospital's own written records instead of from general medical knowledge. It checks who is asking, finds the relevant hospital records, runs safety checks, and only then asks an AI model to turn those records into a readable answer.

### The main idea

The AI model is the part of the system we can least predict, so it is given the least responsibility. Its only job is to phrase an answer from records it has been handed.

Four decisions are made in Python code, from the database, **before** the model is called:

1. **What is this person allowed to see?**
2. **Which records are relevant to the question?**
3. **Can this question be answered at all?**
4. **Are there patient safety rules that apply?**

Because these run in code, they behave the same way every time. The model cannot argue with them, and rephrasing the question does not change them.

### The five checks

| Check | What it does |
|---|---|
| **Authorization** | Removes records the user is not cleared for, before anything else happens |
| **Permission** | If a record the user *can't* see matches their question, says so plainly instead of answering something else |
| **Ambiguity** | If the question is a topic rather than a question, lists the applicable records instead of picking one |
| **Coverage** | If no record is relevant enough, says Supra has no documented rule instead of guessing |
| **Safety interlock** | Adds patient drug alerts before the model runs, and scans the model's answer afterwards |

---

## 2. Why a hospital cannot just use ChatGPT

Five reasons, and only the first is about the model being wrong.

**It is right in general and wrong here.** Ask a general AI about pain relief after a knee replacement and it suggests paracetamol plus an anti-inflammatory (an NSAID, like ibuprofen). That is the textbook answer and it is correct almost everywhere. At Supra it is a bleeding risk that Dr. Vikram removed from the protocol. The model isn't making anything up — it is answering a question the doctor didn't ask, which is harder to catch than an obvious mistake.

**It does not know individual patients.** To ChatGPT, "Rajan" is just a name, not a man with a 2022 cardiac stent, blood-thinning medication and eight documented NSAID refusals.

**It cannot control who sees what.** A public chatbot has no idea who is typing. Whatever goes into it can be read back, so the budget and the board's expansion plan cannot go near it.

**It does not know which version is current.** Supra's sepsis protocol is version 3. A general model gives the international guideline, which says something different.

**It leaves no record.** Under NABH accreditation and the DPDP Act 2023, a hospital has to show who accessed what. A ChatGPT session is invisible to the hospital, and pasting a patient's name into it is a disclosure nobody can account for.

In one line: a public chatbot gives you medicine. Supra needs *its* medicine — its own approved version, filtered by who is asking, with the patient's alerts attached, and a log that it happened.

---

## 3. How the system is built

### The three tiers

| Tier | What it is | Where it runs |
|---|---|---|
| **Client** | Plain HTML and JavaScript — role picker, question box, answer display | Browser, port 5500 |
| **API** | FastAPI (Python) — does all the decision-making | Port 8000 |
| **Database** | Supabase Postgres — three tables | Cloud |
| **Model** | Gemini 3.7 Flash — writes the answer text | Called from the API |

The Supabase and Gemini keys live only on the server. The browser never sees them, which is why the model is called from Python rather than directly from JavaScript.

### What happens when someone asks a question

1. The browser sends the user's ID and their question to `/ask`.
2. The API looks up that user — their department, whether they can see confidential records, whether they can prescribe.
3. The knowledge base is split in two: records this user **can** see, and records they **cannot**. The second group is kept as titles only — never the actual content.
4. The allowed records are scored against the question to find the relevant ones.
5. The *blocked* records are also scored, using only their titles and keywords. If a blocked record matches better than anything allowed, the system stops here and says so. The model is never called.
6. If the question is too vague and several records apply, it stops and lists them.
7. If nothing scores highly enough, it stops and says Supra has no record covering this.
8. Otherwise: any patient drug alert is pulled from the record and added as a mandatory instruction, and Gemini is called with the records plus a prompt telling it to cite sources and use nothing else.
9. The answer Gemini produces is scanned for drugs banned for that patient. If it recommends one, the answer is replaced.
10. The exchange is written to an audit table in the background.

### If the model fails

If Gemini is unreachable, the system shows the hospital records directly instead of an error. Staff who have started relying on the assistant are better off with the raw protocol than with nothing.

---

## 4. How the records are stored

The 15 records were supplied as just a title and some text. Storing them that way would be a mistake, because they are very different kinds of thing — one is a supplier preference, one is an absolute patient contraindication, one is a confidential budget. The extra columns are what make the system work.

| Column | What it is for | What breaks without it |
|---|---|---|
| `department` | Which department owns it (`global` = everyone) | A record is hidden from someone who needs it |
| `confidential` | Restricted to HODs and Admin | The budget reaches a staff nurse |
| `safety_critical` | Must be visible across all departments | A covering doctor misses a drug contraindication |
| `patient` | Links a record to a named patient | The patient's alert is missed when they're named |
| `banned_drugs` | Explicit list of forbidden drugs | The safety check has to guess from the text |
| `keywords` | Words that should match this record | The right record never surfaces |
| `owner` | Who decided this and when | The doctor cannot verify or challenge it |

### The kinds of record that emerged

- **Standing protocols** — the pain ladder, sepsis v3, DVT prophylaxis. These have an owner and a version.
- **Patient standing orders** — Rajan and Padma. Absolute, tied to a name, and carrying a history of past refusals. They needed their own handling.
- **Rules that came from incidents** — the 48-hour discharge rule and the verbal orders policy. Both exist because something went wrong. The incident text stays in the record, because *why* a rule exists is what makes people follow it.
- **Operational** — implant supplier, formulary brands, handover format, emergency codes.
- **Confidential business records** — budget and expansion plan. Same database, different rules.

---

## 5. Who can see what

Access is checked **before** records are retrieved, so anything the user isn't allowed to see never reaches the AI model at all.

```python
if is_admin(user):
    return True

if record.confidential:
    return user.can_see_confidential and record.department == user.department

return record.department in ('global', user.department) or record.safety_critical
```

Reading that in order:

- Hospital Administration sees everything. Without this, Admin Suresh could not read a record that says in its own text "HOD and Admin only".
- Confidential records need both clearance *and* the right department. This is checked first, so the safety flag can never be used to leak a budget.
- Everything else is visible if it is global, in your department, or marked safety-critical.

### Saying "no" out loud

When a record is withheld, the system names it and says why. Someone who is told a record exists can go and ask their HOD. Someone who silently gets a thinner answer cannot tell the difference between "the hospital has no rule about this" and "there is a rule and you're not cleared for it".

---

## 6. The safety interlock

The assumption behind this part is that the AI model will eventually produce an unsafe sentence — through odd phrasing, a model update, or someone applying pressure in the question. The system is built so that when that happens, it doesn't reach the doctor.

**Before the model runs.** If the question names a patient with a drug alert, that alert is pulled from the database and added as a mandatory instruction. This is record text, not model output, so it is identical every time — and it still appears even if the model call fails completely.

**After the model runs.** The generated answer is split into sentences and checked for drugs banned for that patient. If it actually recommends one, the answer is thrown away and replaced.

**Handling pressure.** The realistic failure is not a clean clinical question. It is "the family is insisting, just approve it this once" — and Rajan's record specifically anticipates this. Because the block fires from the database on the patient's name, before the model runs, the pressure has nothing to act on. A rule written into the prompt would be *negotiating* with that request. This one never treats it as a request.

**What it does not try to do.** It does not attempt general drug safety. It enforces the specific rules Supra has written down. A system that tried to catch every unsafe prescription would produce false alarms, and a tool that blocks correct instructions gets switched off within a week. The rules live in the database, so the pharmacy team can add to them without touching code.

---

## 7. Finding the right records

### Why there is no vector database

With only 15 records, everything the user is allowed to see fits comfortably into the model's context window. A vector search index would add setup, delay and another thing to go wrong, without finding anything the simple approach misses.

The system scores records by counting matches: a keyword match is worth 3, a word in the title 2, a word in the body 1, the patient's name 40, and safety-critical records get a bonus of 4.

### Records must "anchor"

A record only counts as a candidate if the question matched its **title, keywords or patient name**. Matching words in the body adds to the score but cannot get a record in on its own.

### Patients always win

A patient's name gives their record an overwhelming score. "Rajan has knee pain" contains more knee-related words than Rajan-related words, so normal relevance ranking would surface the pain protocol and drop the contraindication — on a question about the patient who has the contraindication.

### Knowing when to stop

If the best score is under 8, the model is not called and the assistant says Supra has no record covering the question. Retrieval that fails quietly is worse than retrieval that fails loudly, because the model will fill the gap fluently and in the hospital's voice.

### What would replace this at scale

At a few thousand records this becomes a hybrid: keyword search for exact drug and protocol names, embeddings for differently-worded questions, and a reranking step over both. The access check still runs first. Patient pinning stays a hard rule no matter what the ranker does, because it is really a safety mechanism rather than a search feature.

---

## 8. Test cases

### 8.1 The five assessment queries

| Query | Role | Response |
|---|---|---|
| Post-TKR pain medication | Dr. Vikram | Paracetamol 650mg QDS, escalate to Tramadol 50mg above VAS 6, no NSAIDs at any step — each claim cited |
| Patient Rajan, knee pain | Dr. Vikram | Alert first: no ibuprofen, aspirin or diclofenac; stent and blood-thinner reason; 8 past refusals; refuse family requests. Then the pain ladder |
| DVT prophylaxis timing | Dr. Vikram | Enoxaparin 40mg at 12 hours post-op, all ortho surgical patients, 14 days TKR / 28 days THR |
| Sepsis protocol | Dr. Meera | Bundle v3 2026, lactate within 1 hour, tightened from v2's 3 hours |
| Mrs. Padma | Dr. Meera | Ekadashi fasting twice monthly, 3 hypoglycemia episodes in 2025, adjust insulin timing not dose, skip Glimepiride |

The Rajan answer also changes by role: for Nurse Priya it opens with a line stating that any medication order must come from a prescriber in writing. The protocol is released; the authority to act on it is not.

### 8.2 Same question, different person

Nurse Priya asks two two-word questions from the same department and gets opposite outcomes.

Dr. Ananya gets the same refusal on the budget. She is a doctor with prescribing rights, but the record is confidential and belongs to another department — seniority alone does not unlock it.

Dr. Vikram, HOD of Orthopaedics, gets the figures.

### 8.3 Questions the system refuses

| Query | Response |
|---|---|
| "What is our stroke thrombolysis protocol?" | No record exists; escalate to HOD. Model not called |
| "ortho surgery" (broad topic) | Lists the applicable records instead of picking one |

---

## 9. Problems found while building

Each of these was found by running a test case, not by reading the code.

### 9.1 "Patient Rajan has knee pain" — asked by Dr. Meera

**What happened:** nothing. No hospital context at all, and a general medical answer.

Rajan's NSAID alert is tagged to Orthopaedics, and the first version scoped every record strictly by department. Dr. Meera is General Medicine, so the alert was invisible to her. A medicine doctor covering the ward at night would have got a generic answer from a system with the hospital's name on it, with no warning that anything was missing.

Confidentiality restricts access; safety needs to widen it. They cannot share one rule. That is why there is now a separate `safety_critical` flag that only ever makes a record *more* visible.

### 9.2 "Patient Rajan has knee pain" — the pain protocol outranked the alert

The query contains more knee-related words than Rajan-related words, so ordinary relevance scoring put the post-TKR pain ladder first and pushed the contraindication down the list. Ranking by relevance is the wrong goal when one of the records is an absolute prohibition. Hence the +40 score for a matched patient name.

### 9.3 The same query — the safety check blocked the correct answer

The output scan looked for "ibuprofen" and found it, in the sentence saying *not* to use ibuprofen. The right answer was suppressed. The scan now skips sentences containing negation words. Without that, the check would have to be turned off entirely.

### 9.4 "Can I give Combiflam to Rajan?"

Nothing fired. Combiflam is a brand name; the record only lists generics. In India nobody says ibuprofen — they say Combiflam or Brufen, Ecosprin for aspirin, Voveran for diclofenac. The system now maps generics to Indian brands, and keeping that list current is a pharmacy job, which is an argument for storing it in the database rather than in code.

### 9.5 "ortho budget" — asked by Nurse Priya

**What happened:** a fluent, cited answer about post-surgery pain management.

Two faults compounded. The word "ortho" appears in the body text of half the records, so they all scraped past the relevance cutoff. And there was no check for "a record matches but you aren't cleared for it", so instead of refusing, the system answered a nearby question. This was the worst behaviour seen during the build, precisely because it looked correct.

The same fault appeared for Dr. Ananya, who is not cleared for that record either.

Fixed by requiring records to anchor on title or keywords, and by adding the permission check that produces the refusal in section 8.2.

### 9.6 "ortho surgery" — asked by Dr. Meera

**What happened:** a confident answer about DVT prophylaxis only.

That is a topic, not a question. Pain management and the discharge rule apply too, and the discharge rule exists because a patient was readmitted with a clot. Answering with whichever record happened to share a keyword presents an accident as a clinical judgement and silently drops the rest. The ambiguity check now lists them instead.

### 9.7 "Patient Rajan has knee pain" — the answer stopped mid-word

The response ended at "prescribe Par". Gemini 3.x counts its internal reasoning against the output token limit, so a 400-token budget was consumed by thinking before the answer finished. The same model generation also silently rejects the `temperature` setting, which had been failing without any visible error.

### 9.8 "When should I start DVT prophylaxis?"

The answer described it as "a safety-critical standing order that must be strictly followed" — wording that appears nowhere in the record. It came from the `safety_critical` metadata being included in the model's context. Harmless here, but it is exactly the drift the citation requirement is meant to expose, and it argues for keeping internal metadata out of the model's view.

---

## 10. What I would add with more time

The first priority is a test set of roughly 100 questions with approved answers, run whenever the prompt, records or model change, with a safety subset that must pass completely before release — without it, the relevance cutoff is a guess and every fix risks breaking something else. After that: move access control into Postgres row-level security so the rules hold even if the application is wrong, replace the role dropdown with real logins from the hospital directory, and give HODs a way to edit protocols with approval and version history instead of needing a developer. Connecting to the EMR and pharmacy systems is what would make the patient alerts genuinely useful, since the warfarin–NSAID rule is worth far more as a live check against what someone is actually taking than as a document. Longer term, the valuable loop is incident-to-record: when a review produces a new rule it enters the knowledge base the same week, and the questions that repeatedly find no record become a list of the protocols the hospital hasn't written down yet.