import os
import re
import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from google import genai
from google.genai import types


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not configured.")

if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_KEY is not configured.")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not configured.")


# ============================================================
# CLIENTS
# ============================================================

# The service key bypasses Supabase RLS, so ALL authorization
# in this application is enforced here, in Python. That is a
# deliberate prototype decision and is documented as such.
supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)

gemini = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(
        timeout=30000
    )
)


app = FastAPI(
    title="Supra Hospital AI Assistant",
    description="Hospital knowledge assistant for Supra Hospital",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    user_id: str
    question: str


# ============================================================
# SAFETY VOCABULARY
#
# Indian brand names matter here. A doctor or a family member
# says "Combiflam", not "ibuprofen". A blocklist built only
# from the generic names in the record text misses all of them.
#
# In production this table belongs in Supabase, owned by
# Pharmacy & Therapeutics, not in application code.
# ============================================================

DRUG_ALIASES = {
    "ibuprofen": [
        "ibuprofen", "brufen", "combiflam", "ibugesic", "advil"
    ],
    "aspirin": [
        "aspirin", "ecosprin", "disprin", "asa"
    ],
    "diclofenac": [
        "diclofenac", "voveran", "voltaren", "diclomol", "dynapar"
    ],
    "naproxen": ["naproxen", "naprosyn"],
    "ketorolac": ["ketorolac", "ketanov", "toradol"],
    "aceclofenac": ["aceclofenac", "hifenac", "zerodol"],
    "etoricoxib": ["etoricoxib", "etoshine", "nucoxia"],
    "nimesulide": ["nimesulide", "nise", "nimulid"],
    "indomethacin": ["indomethacin", "indocap"],
    "mefenamic": ["mefenamic", "meftal"],
    "piroxicam": ["piroxicam", "dolonex"],
}

# Every alias above is an NSAID, so a record that prohibits
# "NSAIDs" as a class expands to the full vocabulary.
NSAID_CLASS = [
    alias
    for aliases in DRUG_ALIASES.values()
    for alias in aliases
]

PROHIBITION_MARKERS = [
    "absolute", "avoid", "never", "no ", "not ",
    "contraindicat", "refuse", "withhold", "do not"
]

# A safe answer says "avoid ibuprofen". A naive substring
# scan flags that as a recommendation and blocks the correct
# answer. These cues prevent that.
NEGATION_CUES = [
    "avoid", "avoided", "not", "no ", "never", "don't",
    "do not", "refuse", "refused", "withhold", "contraindicat",
    "without", "instead of", "rather than", "stop", "hold",
    "except", "prohibit", "must not", "cannot", "can't"
]

STOPWORDS = {
    "the", "and", "for", "what", "how", "when", "should", "can",
    "give", "our", "this", "that", "with", "from", "about", "have",
    "has", "are", "was", "you", "your", "any", "does", "did", "will",
    "would", "could", "who", "why", "get", "got", "them", "they",
    "his", "her", "she", "him", "there", "here", "tell", "please",
    "need", "want", "just", "now", "one", "two", "all", "may"
}

PRESCRIBING_INTENT = [
    "prescribe", "prescription", "start", "give", "administer",
    "dose", "dosage", "order", "increase", "decrease", "titrate",
    "medication", "drug", "mg", "escalate"
]

# Retrieval below this top score is treated as a coverage gap
# rather than as an answer. Failing loudly beats a fluent
# answer assembled from weakly-matched records.
COVERAGE_THRESHOLD = 8


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
def root():
    return {
        "name": "Supra Hospital AI Assistant",
        "status": "running",
        "database": "Supabase",
        "llm": GEMINI_MODEL
    }


# ============================================================
# USERS
# ============================================================

@app.get("/users")
def get_users():
    response = (
        supabase
        .table("hospital_users")
        .select("*")
        .execute()
    )

    return response.data or []


@app.post("/admin/reload-knowledge")
def reload_knowledge():
    """
    Clears the in-process knowledge cache after records are
    edited in Supabase, so a protocol change does not require
    a server restart.
    """

    _KB_CACHE["data"] = None

    return {"status": "knowledge cache cleared"}


def get_user(user_id: str):
    response = (
        supabase
        .table("hospital_users")
        .select("*")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    return response.data[0]


def is_admin(user: dict) -> bool:
    """
    Hospital Administration sits above departmental walls.

    Without this, Admin Suresh cannot read the Ortho Budget
    record even though the record itself says
    "HOD and Admin only" - the department filter drops it
    before the clearance check ever runs.
    """

    return (
        user.get("department", "").strip().lower() == "administration"
        or "admin" in user.get("role", "").strip().lower()
    )


def can_prescribe(user: dict) -> bool:
    """
    Scope of practice. A staff nurse asking "should I start
    Tramadol" needs the protocol AND a reminder that the
    order has to come from a prescriber in writing.
    """

    if "can_prescribe" in user and user["can_prescribe"] is not None:
        return bool(user["can_prescribe"])

    role = user.get("role", "").lower()

    return any(
        token in role
        for token in ["doctor", "hod", "consultant", "registrar", "physician"]
    )


# ============================================================
# AUTHORIZATION
# ============================================================

def can_access(record: dict, user: dict) -> bool:
    """
    Release rules, in priority order.

    1. Administration sees everything.
    2. Confidential records need clearance AND department
       ownership. Confidentiality is checked FIRST, so a
       safety flag can never be used to leak a budget.
    3. Everything else: global, own department, or
       safety-critical.

    Rule 3 is the correction that matters. Scoping every
    record strictly by department meant a General Medicine
    doctor covering a ward at night could not see an
    Orthopaedics patient's NSAID contraindication. Safety
    scoping and confidentiality scoping are opposite
    problems and cannot share a filter.
    """

    if is_admin(user):
        return True

    if record.get("confidential", False):
        return (
            user.get("can_see_confidential", False)
            and record.get("department") == user.get("department")
        )

    return (
        record.get("department") in ("global", user.get("department"))
        or record.get("safety_critical", False)
    )


# The knowledge table is small and changes rarely. Re-fetching
# all 15 rows on every question is a network round trip the
# doctor waits on for nothing. Restart the server (or POST
# /admin/reload-knowledge) after editing rows in Supabase.
_KB_CACHE = {"data": None}


def load_knowledge():

    if _KB_CACHE["data"] is None:

        response = (
            supabase
            .table("hospital_knowledge")
            .select("*")
            .execute()
        )

        _KB_CACHE["data"] = response.data or []

    return _KB_CACHE["data"]


def partition_knowledge(user: dict):
    """
    Returns (released, withheld_metadata).

    Withheld records are reported by TITLE ONLY - never
    content. A doctor who is told "2 records exist that you
    are not cleared for" can escalate. A doctor who is
    silently handed a thinner answer cannot tell the
    difference between "Supra has no rule" and "you are not
    cleared for the rule". Silent filtering turns an access
    control into a clinical hazard.
    """

    all_records = load_knowledge()

    released = []
    withheld = []

    for record in all_records:

        if can_access(record, user):
            released.append(record)

        else:
            withheld.append(
                {
                    "title": record.get("title"),
                    "department": record.get("department"),
                    # Metadata only. Content NEVER enters this list.
                    "keywords": record.get("keywords") or [],
                    "confidential": record.get("confidential", False),
                    "reason": (
                        "confidential - clearance required"
                        if record.get("confidential", False)
                        else "belongs to another department"
                    )
                }
            )

    return released, withheld


# ============================================================
# TOKENIZATION
# ============================================================

def tokenize(text: str):
    words = re.sub(r"[^a-z0-9\s]", " ", text.lower()).split()

    return [
        word
        for word in words
        if len(word) > 2 and word not in STOPWORDS
    ]


# ============================================================
# ENTITY DETECTION
# ============================================================

def find_named_entities(question: str, records: list):
    """
    Patient names come from the `patient` column in Supabase,
    never from hardcoded strings. Word-boundary matched so
    a short name cannot match inside an unrelated word.
    """

    question_lower = question.lower()

    entities = set()

    for record in records:

        patient = (record.get("patient") or "").strip().lower()

        if not patient:
            continue

        if re.search(rf"\b{re.escape(patient)}\b", question_lower):
            entities.add(patient)

    return entities


# ============================================================
# SAFETY INTERLOCK
# ============================================================

def extract_banned_drugs(record: dict):
    """
    Derive the prohibited drug list for a patient record.

    Prefers an explicit `banned_drugs` column. Falls back to
    reading the record content, which is how the supplied
    15 records are actually written ("No ibuprofen, no
    aspirin, no diclofenac").
    """

    explicit = record.get("banned_drugs") or []

    if explicit:
        banned = set()

        for drug in explicit:
            key = str(drug).strip().lower()
            banned.update(DRUG_ALIASES.get(key, [key]))

        return sorted(banned)

    content = (record.get("content") or "").lower()

    if not any(marker in content for marker in PROHIBITION_MARKERS):
        return []

    banned = set()

    # A class-level prohibition expands to every brand.
    if "nsaid" in content:
        banned.update(NSAID_CLASS)

    for canonical, aliases in DRUG_ALIASES.items():
        if canonical in content:
            banned.update(aliases)

    return sorted(banned)


def build_safety_directives(records: list, entities: set):
    """
    Fires BEFORE the model is called, straight from the
    record. The directive reaches the doctor even if the
    model call fails entirely, and no phrasing of the
    question - urgency, seniority, family pressure - can
    turn it off, because the model never gets a vote.
    """

    directives = []
    banned = set()

    for record in records:

        patient = (record.get("patient") or "").strip().lower()

        if not patient or patient not in entities:
            continue

        drugs = extract_banned_drugs(record)

        if not drugs:
            continue

        banned.update(drugs)

        directives.append(
            {
                "patient": record.get("patient"),
                "source": record.get("title"),
                "text": record.get("content"),
                "banned_drugs": drugs
            }
        )

    return directives, sorted(banned)


def scan_for_unsafe_recommendation(text: str, banned: list):
    """
    Sentence-level, negation-aware scan of generated text.

    Without the negation check this flags the CORRECT answer,
    because a correct answer contains the word "ibuprofen"
    inside the sentence telling you not to use it.
    """

    if not banned or not text:
        return []

    hits = []

    for sentence in re.split(r"(?<=[.;!?\n])", text):

        lowered = sentence.lower()

        if any(cue in lowered for cue in NEGATION_CUES):
            continue

        for drug in banned:

            if re.search(rf"\b{re.escape(drug)}\b", lowered):

                if drug not in hits:
                    hits.append(drug)

    return hits


# ============================================================
# RETRIEVAL
# ============================================================

def retrieve(question: str, records: list):
    """
    Lexical scoring with two deliberate distortions.

    Entity pinning (+40): a patient's standing order must
    never be out-ranked by a protocol record that happens to
    share more keywords. "Rajan has knee pain" contains more
    knee words than Rajan words, and pure relevance ranking
    surfaces the analgesia ladder while dropping the
    contraindication.

    Safety boost (+4): safety-critical records win ties.
    """

    words = tokenize(question)
    word_set = set(words)

    entities = find_named_entities(question, records)

    scored = []

    for record in records:

        title = (record.get("title") or "").lower()
        content = (record.get("content") or "").lower()
        patient = (record.get("patient") or "").strip().lower()

        keywords = [
            str(keyword).lower()
            for keyword in (record.get("keywords") or [])
        ]

        score = 0
        anchored = False

        for keyword in keywords:
            # Multi-word keywords are matched against the raw
            # question; single tokens against the token set.
            if " " in keyword:
                if keyword in question.lower():
                    score += 3
                    anchored = True
            elif keyword in word_set:
                score += 3
                anchored = True

        for word in word_set:

            if word in title:
                score += 2
                anchored = True

            if word in content:
                score += 1

        if patient and patient in entities:
            score += 40
            anchored = True

        if record.get("safety_critical", False) and score > 0:
            score += 4

        # A record only QUALIFIES if the question matched its
        # title, its keywords, or its patient. Content-word
        # matches add weight but cannot admit a record on their
        # own - otherwise "ortho budget" drags in every record
        # whose body happens to contain the word "ortho", and
        # the model faithfully summarises the wrong thing.
        if anchored and score >= 3:
            scored.append({"record": record, "score": score})

    scored.sort(key=lambda item: item["score"], reverse=True)

    top_score = scored[0]["score"] if scored else 0

    return scored[:4], top_score, entities


# ============================================================
# ANSWER GENERATION
# ============================================================

# Gemini 3.x rejects/ignores temperature, top_p and top_k, and
# defaults to spending thinking budget this task does not need
# (we are summarising six short records, not solving anything).
# Dropping thinking_level is the single biggest latency win.
# The try/except keeps the app running on SDK versions where
# ThinkingConfig is not present.

def build_generation_config(system_instruction: str):

    common = {
        "system_instruction": system_instruction,
        "max_output_tokens": 400,
        "automatic_function_calling": (
            types.AutomaticFunctionCallingConfig(disable=True)
        ),
    }

    try:
        return types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=GEMINI_THINKING_LEVEL
            ),
            **common
        )

    except (AttributeError, TypeError, ValueError) as exc:
        print(f"ThinkingConfig unavailable, continuing without it: {exc}")
        return types.GenerateContentConfig(**common)


def score_withheld(question: str, withheld: list):
    """
    Score the records this user is NOT cleared for, using
    TITLE and KEYWORDS only - never content.

    This exists so the assistant can say "a record exists and
    you are not cleared for it" instead of quietly answering a
    different question. A doctor who is silently handed a
    thinner answer cannot tell "Supra has no rule" apart from
    "you are not cleared for the rule", and that ambiguity is
    a clinical hazard, not a privacy feature.
    """

    word_set = set(tokenize(question))

    best = None
    best_score = 0

    for item in withheld:

        title = (item.get("title") or "").lower()

        keywords = [
            str(keyword).lower()
            for keyword in (item.get("keywords") or [])
        ]

        score = 0

        for keyword in keywords:
            if " " in keyword:
                if keyword in question.lower():
                    score += 3
            elif keyword in word_set:
                score += 3

        for word in word_set:
            if word in title:
                score += 3

        if score > best_score:
            best_score = score
            best = item

    return best, best_score


AMBIGUITY_MAX_TOKENS = 2


def is_ambiguous(question: str, retrieved: list, entities: set):
    """
    A short query, no named patient, and two or more records
    qualifying independently.

    The token count is the crude part of this - it is a proxy
    for "the user has not told me what they actually want".
    A proper version would compare the top two scores and fire
    when they are close, which is a better signal but needs
    calibrated scores this prototype does not have.
    """

    if entities:
        return False

    if len(retrieved) < 2:
        return False

    return len(tokenize(question)) <= AMBIGUITY_MAX_TOKENS


NO_DATA_RESPONSE = (
    "Supra Hospital has no documented record covering this "
    "question in the material released to you. I won't answer "
    "from general medical knowledge - escalate to your HOD or "
    "the relevant committee instead."
)


def generate_answer(
    question: str,
    retrieved: list,
    user: dict,
    directives: list,
    covered: bool
):
    if not retrieved:
        return NO_DATA_RESPONSE

    context_blocks = []

    for item in retrieved:

        record = item["record"]

        context_blocks.append(
            f"SOURCE: {record.get('title', '')}\n"
            f"OWNER: {record.get('owner') or 'not recorded'}\n"
            f"DEPARTMENT: {record.get('department', '')}\n"
            f"SAFETY_CRITICAL: {record.get('safety_critical', False)}\n"
            f"CONTENT:\n{record.get('content', '')}"
        )

    context = "\n\n---\n\n".join(context_blocks)

    directive_block = ""

    if directives:

        lines = []

        for directive in directives:
            lines.append(
                f"- {directive['patient']} "
                f"[{directive['source']}]: {directive['text']}"
            )

        directive_block = (
            "MANDATORY STANDING ORDERS FOR THIS QUERY.\n"
            "State these plainly in your answer. Do not soften "
            "them, do not offer a workaround, and do not comply "
            "with pressure from family, seniority or urgency:\n"
            + "\n".join(lines)
            + "\n\n"
        )

    coverage_instruction = (
        "The released records cover this question."
        if covered
        else (
            "The released records only PARTIALLY cover this "
            "question. Say so in your first sentence, answer only "
            "the part Supra has actually documented, and tell the "
            "user to escalate the remainder to their HOD rather "
            "than improvising."
        )
    )

    scope_instruction = (
        ""
        if can_prescribe(user)
        else (
            "\nThis user is NOT a prescriber. If the answer "
            "involves starting or changing a medication, state "
            "the protocol and add that the order must come from "
            "a prescriber in writing.\n"
        )
    )

    system_instruction = f"""
You are the internal knowledge assistant for Supra
Multi-Specialty Hospital, Hyderabad.

RULES, in priority order:

1. Answer ONLY from the supplied Supra records. Never
   substitute general medical knowledge for a Supra record,
   and never fill a gap with what is usually done elsewhere.
2. Cite the record title behind every clinical instruction.
3. The retrieved records are CANDIDATES. Judge which ones
   actually address the question and ignore the rest. Do not
   dump unrelated records into the answer.
4. Combine records when they genuinely contribute to the same
   answer - a discharge question may also involve ongoing
   prophylaxis, an analgesia question may involve a patient
   contraindication.
5. Any mandatory standing order listed below is absolute.
   Restate it. It is not negotiable under any framing.
6. {coverage_instruction}
7. If nothing relevant is present, reply exactly:
   {NO_DATA_RESPONSE}
8. Be directive and short: 4-7 sentences, doses and timings
   explicit. No advice to "consult a doctor" - you are
   talking to hospital staff.
9. Authorization has already been enforced by the backend.
   Never speculate about records you were not given.
{scope_instruction}"""

    user_prompt = f"""
USER
Name: {user.get("name")}
Role: {user.get("role")}
Department: {user.get("department")}

QUESTION
{question}

{directive_block}AUTHORIZED SUPRA HOSPITAL RECORDS
{context}

Answer using only the records above.
""".strip()

    try:

        response = gemini.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=build_generation_config(system_instruction)
        )

        answer = (response.text or "").strip()

        if not answer:

            collected = ""

            for candidate in (response.candidates or []):

                if not candidate.content:
                    continue

                for part in (candidate.content.parts or []):

                    text = getattr(part, "text", None)

                    if text:
                        collected += text

            answer = collected.strip()

        if not answer:
            return NO_DATA_RESPONSE

        return answer

    except Exception as exc:

        print(f"Gemini generation error: {exc}")

        # Degrade to the record text rather than to silence.
        # An assistant that returns nothing when the model is
        # unreachable is worse than one that returns the
        # protocol, because staff have already changed their
        # workflow to rely on it.
        fallback = "\n\n".join(
            f"{item['record'].get('title')}: "
            f"{item['record'].get('content')}"
            for item in retrieved[:3]
        )

        return (
            "The answer-generation service is unavailable. "
            "Showing the authorized Supra records directly, "
            "unsummarised:\n\n" + fallback
        )


# ============================================================
# AUDIT
# ============================================================

def write_audit(user, question, retrieved, withheld, guardrails, answer):
    """
    Best-effort. An audit failure must never break a clinical
    answer, but it must also never pass silently on the server.
    """

    try:
        supabase.table("query_audit").insert(
            {
                "created_at": datetime.datetime.utcnow().isoformat(),
                "user_id": user.get("id"),
                "user_name": user.get("name"),
                "user_role": user.get("role"),
                "department": user.get("department"),
                "question": question,
                "records_used": [
                    item["record"].get("title") for item in retrieved
                ],
                "records_withheld": len(withheld),
                "guardrails_fired": guardrails,
                "answer_preview": (answer or "")[:500],
            }
        ).execute()

    except Exception as exc:
        print(f"Audit write failed: {exc}")


# ============================================================
# ASK
# ============================================================

@app.post("/ask")
def ask(request: AskRequest, background: BackgroundTasks):

    user = get_user(request.user_id)

    if not user:
        return {"error": "Unknown hospital user."}

    question = request.question.strip()

    if not question:
        return {"error": "Please enter a question."}

    guardrails = []

    # 1. Authorization, before retrieval.
    released, withheld = partition_knowledge(user)

    # 2. Retrieval over released records only.
    retrieved, top_score, entities = retrieve(question, released)

    covered = bool(retrieved) and top_score >= COVERAGE_THRESHOLD

    # 3. Pre-model safety interlock.
    directives, banned_drugs = build_safety_directives(released, entities)

    if directives:
        guardrails.append(
            "patient-alert:" + ",".join(d["patient"] for d in directives)
        )

    # 4. Does a record this user is NOT cleared for match the
    #    question better than anything they ARE cleared for?
    #    If so, say that plainly instead of answering something
    #    adjacent. This is checked BEFORE the model is called.
    blocked_record, blocked_score = score_withheld(question, withheld)

    permission_denied = (
        blocked_record is not None
        and blocked_score >= 3
        and blocked_score >= top_score
    )

    if permission_denied:

        guardrails.append("permission-denied:" + blocked_record["title"])

        answer = (
            "You are not authorised to access this information.\n\n"
            f"Supra Hospital holds a record titled "
            f"\"{blocked_record['title']}\" "
            f"({blocked_record['department']}) that matches your "
            f"question, but it is {blocked_record['reason']}.\n\n"
            f"{user.get('name')} is signed in as "
            f"{user.get('role')}, {user.get('department')}. "
            "Request access through your HOD or Hospital "
            "Administration.\n\n"
            "I won't answer around the restriction by giving you "
            "a different record instead."
        )

    # 5. Ambiguous topic rather than a question.
    #
    #    "ortho surgery" is not a question, it is a subject
    #    area, and several Supra records apply to it. Answering
    #    with whichever one happened to anchor on a shared
    #    keyword presents a keyword collision as a clinical
    #    judgement, and silently omits the rest - including,
    #    in this case, the discharge rule that exists because
    #    a patient was readmitted with a DVT.
    #
    #    A confident partial answer to an underspecified
    #    question is how staff lose the information they did
    #    not know to ask for. Name what exists; ask which.
    elif is_ambiguous(question, retrieved, entities):

        guardrails.append("ambiguous-topic")

        titles = [item["record"].get("title") for item in retrieved]

        answer = (
            "That is a topic rather than a question, and Supra "
            "has several records that apply to it. I won't pick "
            "one for you - the others would be silently omitted.\n\n"
            "Records available to you on this subject:\n"
            + "\n".join(f"- {title}" for title in titles)
            + "\n\nWhich do you need, or ask the specific "
            "clinical question you have in mind."
        )

    # 6. Nothing relevant was released, and nothing was withheld
    #    either - the question is outside Supra's documented
    #    knowledge. Say so; do not call the model.
    elif not retrieved or not covered:

        guardrails.append(
            "no-relevant-record" if not retrieved else "coverage-gap"
        )

        nearest = [
            item["record"].get("title")
            for item in retrieved[:3]
            if item["score"] >= 5
        ]

        answer = NO_DATA_RESPONSE

        if nearest:
            answer += (
                "\n\nThe closest records Supra does hold are: "
                + "; ".join(nearest)
                + ". None of them answers what you asked."
            )

    else:

        answer = generate_answer(
            question, retrieved, user, directives, covered
        )

        # 5. Post-model safety interlock.
        unsafe = scan_for_unsafe_recommendation(answer, banned_drugs)

        if unsafe:

            guardrails.append("output-blocked:" + ",".join(unsafe))

            blocked_for = ", ".join(d["patient"] for d in directives)

            answer = (
                "Answer withheld by the safety interlock.\n\n"
                f"The generated response recommended "
                f"{', '.join(unsafe)} for {blocked_for}, which a "
                "standing Supra alert prohibits absolutely.\n\n"
                "Refer to the standing order shown above and "
                "record the refusal in the patient's notes."
            )

    # 6. Scope-of-practice note.
    prescribing_question = any(
        token in question.lower() for token in PRESCRIBING_INTENT
    )

    scope_note = None

    if prescribing_question and not can_prescribe(user):

        guardrails.append("scope-of-practice")

        scope_note = (
            f"{user.get('name')} is not a prescriber. This is the "
            "protocol, not an order - the medication change must "
            "be authorised by a prescriber in writing "
            "(Verbal Orders Policy)."
        )

    # Off the request path — the doctor should not wait on a
    # Supabase insert for an answer that is already computed.
    background.add_task(
        write_audit,
        user, question, retrieved, withheld, guardrails, answer
    )

    return {
        "answer": answer,

        "user": {
            "name": user["name"],
            "role": user["role"],
            "department": user["department"],
            "can_prescribe": can_prescribe(user),
            "clearance": (
                "administration" if is_admin(user)
                else "confidential" if user.get("can_see_confidential")
                else "standard"
            )
        },

        "safety_alerts": [
            {
                "patient": d["patient"],
                "source": d["source"],
                "text": d["text"],
                "banned_drugs": d["banned_drugs"]
            }
            for d in directives
        ],

        "scope_note": scope_note,

        "coverage": "full" if covered else "partial",

        "sources": [
            {
                "id": item["record"]["id"],
                "title": item["record"]["title"],
                "department": item["record"]["department"],
                "confidential": item["record"]["confidential"],
                "owner": item["record"].get("owner"),
                "score": item["score"]
            }
            for item in retrieved
        ],

        "withheld": {
            "count": len(withheld),
            "records": [
                {
                    "title": item["title"],
                    "department": item["department"],
                    "reason": item["reason"]
                }
                for item in withheld
            ]
        },

        "guardrails_fired": guardrails,

        "retrieved_count": len(retrieved)
    }