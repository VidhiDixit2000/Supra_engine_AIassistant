# Supra Hospital AI Assistant — Design Document

**Supra Multi-Specialty Hospital, Hyderabad**
Architecture, safety model, findings and roadmap.

> Deliverables: working prototype (FastAPI + Supabase + Gemini + browser client) and this document.
> Scope: 15 supplied organisational records, 5 staff roles, 7 departments.
> Stack: Python 3.13 / FastAPI, Supabase Postgres, Gemini 3.7 Flash, static HTML + JS client.
> *Not a medical device. Assessment prototype only.*

---

## 1. Executive summary

A doctor at Supra does not need an assistant that knows medicine. Every junior doctor already has one on their phone. What Supra needs is an assistant that knows Supra: that Dr. Vikram removed NSAIDs from the post-TKR analgesia ladder in January 2025, that patient Rajan has refused NSAIDs eight times on a documented cardiac indication, that the sepsis lactate window was tightened from three hours to one, and that a staff nurse should not be able to read the departmental budget.

The system is a three-tier application. A browser client calls a FastAPI backend, which enforces authorization against Supabase, retrieves candidate records, runs deterministic safety checks, and only then asks Gemini to phrase an answer from the records it was given.

The design position is that the language model is the least trustworthy component in the system, so it is given the least authority. It phrases answers. It does not decide what is safe, what a user may see, whether a question is answerable, or what Supra's protocol is. Those four decisions are made in Python, from database records, and are reproducible without the model.

### The five gates

Before Gemini is called at all, a request passes through four gates, and its output passes through a fifth.

- Authorization — records outside the user's clearance are removed before retrieval, so nothing unauthorised ever enters the prompt.

- Permission disclosure — if a record the user is NOT cleared for matches the question better than anything they are, the model is never called and the user is told plainly that a record exists and they lack clearance.

- Ambiguity — a topic rather than a question, matching several records, returns the list instead of picking one.

- Coverage — below a relevance threshold the model is not called and the assistant states that Supra has no documented record, rather than answering from general medical knowledge.

- Safety interlock — patient standing orders are injected from the record before generation, and the generated text is scanned afterwards for prohibited drugs.

## 2. Why a hospital cannot just use a public chatbot

This is what the brief is really asking, so it deserves a precise answer rather than the usual line about hallucination. There are five distinct failure modes and only one is about the model being wrong.

### 2.1 It is confidently right in general and wrong here

Asked about post-TKR analgesia, a general model returns textbook multimodal analgesia including an NSAID, because that is the correct general answer. At Supra it is a bleeding risk that a named consultant removed from the ladder. The model is not hallucinating; it is answering a question the doctor did not ask. That is more dangerous than a hallucination because it survives scrutiny.

### 2.2 It has no patient-level standing orders

Rajan's alert is not a guideline, it is an instruction with a history: a 2022 stent, dual antiplatelet therapy, eight documented refusals, and an explicit note that the family will ask again. To a public chatbot, Rajan is a name with no chart, and the answer to knee pain is ibuprofen or diclofenac.

### 2.3 It cannot enforce who may know what

The FY2026 ortho budget and the board expansion plan are confidential. A chatbot has no concept of clearance: whatever is pasted into its context is readable by whoever pasted it. Access control must sit outside the model, because anything inside the prompt is negotiable.

### 2.4 It has no version, owner or date

Supra's sepsis bundle v3 tightened lactate to one hour. A general model returns the Surviving Sepsis Campaign position, which is defensible and is not Supra's. Clinical governance requires knowing which version is current, who approved it, and when it changed.

### 2.5 It leaves no trace

Under NABH accreditation and India's DPDP Act 2023, a system touching patient data needs auditability: who asked, what was released, what was withheld. A public chatbot session is unlogged from the hospital's side, and pasting patient identifiers into it is a disclosure the hospital cannot account for.

*The one-line version: a public chatbot gives you medicine. Supra needs its medicine — the version its own committee approved, filtered by who is asking, with the patient's standing orders attached, and a record that the exchange happened.*

## 3. Architecture

| **Tier** | **Component**                         | **Responsibility**                                                         |
|----------|---------------------------------------|----------------------------------------------------------------------------|
| Client   | Static HTML + JS, served on port 5500 | Role selection, question entry, rendering the answer and its cited sources |
| API      | FastAPI on port 8000                  | Authorization, retrieval, safety interlocks, gate decisions, audit         |
| Data     | Supabase Postgres                     | hospital_users, hospital_knowledge, query_audit                            |
| Model    | Gemini 3.7 Flash, server-side         | Phrasing an answer from records it is handed. No tools, no autonomy        |

### Request flow

1.  Client POSTs user_id and question to /ask.

2.  The user row is loaded from Supabase; clearance, department and prescriber status are derived from it.

3.  The knowledge base is partitioned into released and withheld records for that user. Withheld records are carried forward as metadata only — title, department, reason — never content.

4.  Released records are scored lexically. A record must anchor on title, keywords or patient name to qualify; body-word matches add weight but cannot admit a record alone.

5.  Withheld records are scored on title and keywords only. If the best withheld match beats the best released match, the request stops here with a permission statement.

6.  If the query is short, names no patient, and several records qualify, it stops with an ambiguity statement listing them.

7.  If the top released score is below the coverage threshold, it stops with a no-record statement.

8.  Otherwise: patient standing orders are extracted from the matched records and injected as mandatory directives, and Gemini is called with the records, the directives and a system prompt requiring citation.

9.  The generated text is scanned for drugs prohibited for the named patient. A genuine recommendation replaces the answer with a block notice.

10. The exchange is written to query_audit as a background task, off the response path.

### Why the model is called last and least

Every decision that carries consequence — clearance, relevance, safety, answerability — is made before the model runs and can be reproduced without it. Gemini's only job is to turn three or four records into four to seven sentences with citations. If Gemini is unreachable, the system degrades to showing the authorized record text rather than to silence, because staff who have restructured their workflow around an assistant are worse off with nothing than with the raw protocol.

## 4. Data model

The 15 records were supplied as title and content. Treating them as flat text is the first available mistake, because their governance differs enormously: one is a vendor preference, one is an absolute patient contraindication, one is board-confidential. Metadata is the load-bearing part.

| **Column**      | **Purpose**                                           | **Consequence if wrong**                             |
|-----------------|-------------------------------------------------------|------------------------------------------------------|
| department      | Owning department; 'global' applies hospital-wide     | Wrong value hides a record from someone who needs it |
| confidential    | Restricted to HOD and Administration                  | Budget or board strategy reaches a nurse             |
| safety_critical | Marks records that must cross department walls        | A covering physician misses a contraindication       |
| patient         | Pins a record to a named patient for entity retrieval | Standing order missed when the patient is named      |
| banned_drugs    | Explicit prohibition list for the interlock           | Interlock falls back to parsing prose                |
| keywords        | Retrieval vocabulary, including brand names           | Query misses the record it needed                    |
| owner           | Who decided this and when                             | Doctor cannot verify or challenge the instruction    |

### Record classes that emerged

- Standing protocols with an owner and version — post-TKR ladder, sepsis v3, DVT prophylaxis, diabetic fasting.

- Patient-level standing orders — Rajan, Padma. Absolute, attached to a name, carrying a refusal history. These behave differently from protocols and needed their own retrieval and safety path.

- Incident-derived rules — the 48-hour TKR discharge rule, the verbal orders policy. These encode why, and the why is what produces compliance, so the incident text stays in the record.

- Operational and commercial — implant vendor, formulary brands, handover format, emergency codes.

- Confidential business records — ortho budget, expansion plan. Not clinical, same store, different governance.

## 5. Authorization

Authorization runs before retrieval, so nothing a user may not see ever enters the prompt. Prompt-level instructions of the form "do not reveal the budget to nurses" are a request, not a control; they survive until someone phrases a question cleverly.

> if is_admin(user): return True
>
> if record.confidential:
>
> return user.can_see_confidential and record.department == user.department
>
> return record.department in ('global', user.department) or record.safety_critical

Confidentiality is evaluated before the safety flag, so a safety-critical marking can never be used to release a budget. Administration sits above departmental walls, because without that Admin Suresh cannot read the record that says in its own text "HOD and Admin only".

### The correction that mattered

The first implementation scoped every record strictly by department. That produced a specific, plausible and dangerous outcome: Dr. Meera and Dr. Ananya in General Medicine could not see the Rajan NSAID alert, because it was tagged Orthopaedics. A medicine doctor covering a ward at night, asked about Rajan's knee pain, would have received a context-free answer from a system branded as the hospital's own. There was no error and no warning — the worst possible failure shape.

Confidentiality scoping and safety scoping are opposite problems and cannot share a filter. The safety_critical flag exists to separate them, and it releases records more widely, never less.

### Disclosure rather than silence

Silent filtering converts an access control into a clinical hazard. A user who is told a record exists and they lack clearance can escalate; a user silently handed a thinner answer cannot distinguish "Supra has no rule" from "Supra has a rule you are not cleared for". The permission gate therefore names the record title and the reason, and explicitly states that it will not answer around the restriction with a different record.

## 6. The safety interlock

The design assumption is that the model will eventually produce an unsafe sentence — through unusual phrasing, a long conversation, a model version change, or straightforward social pressure in the query. The system is built so that this does not reach the doctor.

### 6.1 Before the model

A patient name in the query pins that patient's record and injects its content as a mandatory directive above the generated answer. This is record text, not model output, so it cannot vary between runs and it survives a failed model call.

### 6.2 After the model

The generated answer is split into sentences and scanned for the prohibited drug list attached to the named patient. A genuine recommendation suppresses the answer and replaces it with the record-sourced instruction.

### 6.3 Two details that decide whether it works

Negation awareness. A correct answer contains the word ibuprofen, inside the sentence telling you not to use it. The first implementation blocked the correct answer. Sentences containing a negation cue are excluded from the scan; without this the interlock has to be switched off, which means it does not exist.

Brand names. A doctor or a family member says Combiflam, Brufen, Voveran, Ecosprin — not ibuprofen and diclofenac. A blocklist built from the record text alone misses every one of them. An alias table expands generics to Indian brands, and a class-level prohibition on NSAIDs expands to the full vocabulary.

### 6.4 Pressure resistance

The realistic failure is not a clean clinical question but a request under pressure: the family insisting, just this once. The record itself anticipates this. Because the block is deterministic and fires on the patient name before generation, the pressure has no surface to act on. A prompt-level guardrail would be arguing with the request; this one never sees it as a request.

### 6.5 What it deliberately does not do

It does not attempt general clinical safety. It enforces the specific rules Supra has written down. A blocklist attempting to catch every unsafe prescription would produce false blocks, and a system that blocks correct instructions is switched off within a week. Narrow and reliable beats broad and noisy. The escape valve is that the rules are data, so Pharmacy and Therapeutics extend them without touching code.

## 7. Retrieval and the answerability gates

At 15 records the released corpus fits in a context window with room to spare, so a vector index would add infrastructure, latency and a failure mode without improving recall. Retrieval is transparent lexical scoring: keyword +3, title word +2, body word +1, patient name +40, safety-critical +4.

### Anchoring

A record only qualifies if the question matched its title, keywords or patient. Body-word matches add weight but cannot admit a record on their own. Without this rule the query "ortho budget" pulled in every record whose text happened to contain the word ortho, and the model faithfully summarised post-TKR analgesia at someone who asked about money.

### Entity pinning

A patient name gives that record a dominant score. Patient standing orders must never be out-ranked by a protocol that shares more keywords: "Rajan has knee pain" contains more knee words than Rajan words, and pure relevance ranking surfaces the analgesia ladder while dropping the contraindication.

### Coverage threshold

Below a top score of 8, the model is not called. The assistant states that Supra has no documented record and routes to the HOD. Retrieval that fails silently is worse than retrieval that fails loudly, because the model will fill the gap fluently and in the hospital's voice.

### Ambiguity

A short query naming no patient, with several records qualifying, returns the list rather than an answer. "Ortho surgery" is a subject area, not a question: DVT prophylaxis, post-TKR analgesia and the discharge rule all apply. Answering with whichever record anchored on a shared keyword presents a keyword collision as a clinical judgement and silently omits the rest — including the discharge rule that exists because a patient was readmitted with a DVT.

### What replaces this at scale

At a few thousand records the ranking becomes hybrid: BM25 for exact protocol and drug names, embeddings for paraphrase, a reranker over the union, and a metadata pre-filter so authorization still runs before scoring rather than after. Entity pinning stays a hard rule regardless of ranker, because it is a safety mechanism wearing a retrieval costume.

## 8. Demonstrated behaviour

| **Query**                | **Role**    | **System response**                                                                                                                                                                |
|--------------------------|-------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Post-TKR pain medication | Dr. Vikram  | Paracetamol 650mg QDS, escalate to Tramadol 50mg above VAS 6, NSAIDs avoided at all steps, each claim cited                                                                        |
| Patient Rajan, knee pain | Dr. Vikram  | Standing order first: no ibuprofen, aspirin or diclofenac; stent and dual antiplatelet reason; 8 documented refusals; refuse family requests. Then the Paracetamol/Tramadol ladder |
| Patient Rajan, knee pain | Nurse Priya | Same, prefixed with the prescriber note — the order must come from a prescriber in writing                                                                                         |
| DVT prophylaxis timing   | Dr. Vikram  | Enoxaparin 40mg SC at 12 hours post-op, all ortho surgical patients, 14 days TKR / 28 days THR                                                                                     |
| Sepsis protocol          | Dr. Meera   | Bundle v3 2026, lactate within 1 hour, tightened from v2's 3 hours, cultures before antibiotics, vasopressors below MAP 65                                                         |
| Mrs. Padma               | Dr. Meera   | Ekadashi fasting twice monthly, 3 hypoglycemia episodes in 2025, adjust insulin timing not dose, skip Glimepiride                                                                  |
| Ortho budget             | Nurse Priya | Permission statement naming the record and the reason. Model not called                                                                                                            |
| Ortho budget             | Dr. Vikram  | Budget figures released                                                                                                                                                            |
| Ortho budget             | Dr. Ananya  | Permission statement — a doctor, but wrong department and no clearance                                                                                                             |
| Ortho surgery            | Dr. Meera   | Ambiguity statement listing the applicable records                                                                                                                                 |
| Stroke thrombolysis      | Dr. Vikram  | No documented record; escalate to HOD. Model not called                                                                                                                            |

The role comparison is the clearest demonstration. Nurse Priya asks two two-word questions from the same department: "ortho surgery" returns the full prophylaxis protocol with a prescriber note, and "ortho budget" returns a named refusal. Same user, same system, opposite outcomes, decided in code.

## 9. Problems discovered while building

In the order they were hit.

### 9.1 Department scoping quietly hid a contraindication

Covered in section 5. Correct as an access control, wrong as a clinical system, and it failed without any signal that it had failed.

### 9.2 Relevance ranking demotes the most important record

Ranking by relevance is the wrong objective when one record is an absolute prohibition. The contraindication scored lower than the analgesia protocol on a query about the patient with the contraindication.

### 9.3 A naive blocklist blocks the correct answer

The first output scan flagged the safe answer, because the safe answer names the drug it is forbidding. Negation handling is not a refinement; it is the difference between a working interlock and one that gets disabled.

### 9.4 Generic drug names are insufficient in India

Combiflam, Brufen, Voveran and Ecosprin are what people actually say. Maintaining that mapping against the formulary is a pharmacy responsibility, not an engineering one, which is an argument for holding it in the database.

### 9.5 Body-word matching produced confident wrong answers

"Ortho budget" asked by a nurse returned post-TKR analgesia. Two independent faults compounded: common words admitted irrelevant records, and there was no gate to say "a record matches but you are not cleared for it", so the system answered an adjacent question instead. This produced the single worst behaviour observed during the build, because it was fluent, cited, and about the wrong subject.

### 9.6 Thinking tokens consumed the answer budget

Gemini 3.x counts reasoning against max_output_tokens. A 400-token budget was consumed by reasoning and the answer truncated eight words in, mid-drug-name. Configuration guidance for 3.x also drops temperature, top_p and top_k, which are silently rejected.

### 9.7 The model borrows tone from the prompt

Asked about DVT prophylaxis, Gemini described it as "a safety-critical standing order that must be strictly followed" — phrasing absent from the record, drawn from the safety metadata and the directive language in the system prompt. Harmless here, but it is precisely the drift the citation requirement exists to expose, and it argues for keeping governance metadata out of the model's context.

### 9.8 Superseded protocols look identical to current ones

The sepsis record contains both v3 and the v2 figure it replaced. A retrieval system returning text without version semantics can surface the wrong number from the right record. Production needs an explicit supersedes relationship and a rule that superseded content is never quoted as current.

## 10. Limitations

Stated plainly, because a hospital tool that oversells itself is the problem it claims to solve.

- Authentication is a dropdown. There is no identity provider and no way to prove who is asking, so every access-control claim here is a design demonstration rather than an implementation.

- The Supabase service key bypasses row-level security. All authorization is enforced in the FastAPI layer. A production build moves these rules into RLS policies so they hold even if the application is wrong.

- The coverage threshold of 8 is uncalibrated. Lexical scores are not comparable across differently-worded questions — a long question accumulates score by being long. The number was chosen to fail toward refusing rather than answering, but it is a guess.

- The ambiguity test is a token count, which is a crude proxy for "the user has not said what they want". The right signal is the gap between the top two scores, which needs calibrated scores this prototype does not have.

- Keywords are hand-curated. A doctor asking about "blood thinners" rather than anticoagulation may not surface the record they need. This is the most likely source of a false refusal, and false refusals are how a tool gets abandoned.

- There is no evaluation harness. Answers were verified by reading them, which is adequate for a 90-minute build and inadequate for a ward.

- No EMR or pharmacy integration. Every patient fact comes from a static record, so the Warfarin–NSAID rule is a document rather than a live check against what the patient is actually taking.

- The audit table records queries but is not tamper-evident and has no retention policy.

## 11. Roadmap

### First 30 days — make it trustworthy

- Evaluation harness: roughly 100 questions with approved answers, run on every prompt, record or model change, with a safety subset that must pass at 100% before release. Calibrate the coverage threshold against measured false-refusal and false-answer rates instead of judgement.

- Move authorization into Supabase row-level security so the rules hold at the database, not only in the application.

- SSO against the hospital directory, with role and department inherited from HR data rather than selected from a dropdown.

- Protocol authoring workflow: HODs draft, a committee approves, changes are versioned with owner and effective date, superseded content marked rather than deleted.

### Days 30–90 — make it useful at the bedside

- EMR and pharmacy integration so patient-level interlocks read the live medication list.

- Hybrid retrieval with metadata pre-filtering once the corpus passes a few hundred records.

- Structured model output — answer, cited record IDs, confidence, gap flag — so citations are rendered from data rather than parsed out of prose.

- Mobile and voice entry for ward rounds, where nobody is typing paragraphs into a laptop.

### Beyond 90 days — make it institutional

- Close the loop from incidents: when an incident review produces a rule, it enters the knowledge base with the incident attached and is enforced the same week. This is what turns the assistant into the hospital's memory rather than a search box over old documents.

- Drift monitoring: track questions that repeatedly hit coverage gaps, because those are the protocols the hospital has not written down yet. The gap log is a clinical governance instrument in its own right.

- Escalation routing: an unanswerable query becomes a message to the named owner of the nearest protocol, not a dead end.

- Expansion into ICU, Paediatrics, Surgery and Pharmacy, each with its own owners and release rules.

### Governance to put around it

- A named clinical owner per record, reviewed on a fixed cycle. Unowned content is removed rather than allowed to age.

- A standing rule that the assistant advises and never orders — the existing verbal orders policy applied to software.

- Deployment inside the hospital's own boundary, with patient identifiers never leaving it, consistent with DPDP Act 2023 obligations.

## 12. Closing position

The valuable asset in this brief is not the model. It is the fifteen records — that a named consultant made a decision on a date, that a patient has refused a drug eight times, that someone was readmitted with a DVT after being discharged twelve hours too early. That knowledge currently lives in handover conversations, WhatsApp groups, and the heads of people who might resign. It is the least durable and most valuable thing the hospital owns.

What this prototype argues is that the right job for a language model in a hospital is narrow and unglamorous: make that institutional knowledge answerable in the ten seconds a doctor actually has, while ordinary deterministic code ensures the model never decides what is safe, what is confidential, or what Supra's protocol is.

*A hospital cannot use a public chatbot for the same reason it cannot use a locum who has never read the ward's notes — not because they lack medical knowledge, but because they lack this ward's medical knowledge, and they have no way of knowing what they are missing.*