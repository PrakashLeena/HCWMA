"""What may be written to memory (`docs/memory-policy.md` sections 3, 4 and 9).

Every write goes through `screen()` first. It works on plain text with fixed rules; no
model is involved, so nothing a model says can talk its way past it.

- **Blocked parts are dropped, the rest is kept.** Passwords, keys, tokens, recovery codes,
  card and bank account numbers and government ID numbers are cut out part by part.
- **Instruction-like lines are rejected.** Memory is data. A line that tries to change what
  HCWMA is allowed to do ("always approve", "ignore the rules") is refused and flagged.
- **Sensitive topics** (health, politics, religion and similar) are stored only when the
  owner asked for it (`owner_said`), never by an automatic summary.
- **Owner's words only.** `grounded_in()` checks that a "remember this" entry is made of
  what the owner actually typed, so text that came out of a file, a web page or a tool
  result cannot be saved as a fact about the owner.

The rules err on the side of not storing: a harmless sentence that looks like a secret or
an instruction is dropped rather than kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---- blocked content ---------------------------------------------------------------------

# Words that are about a secret whatever else the sentence says.
_SECRET_WORD = re.compile(
    r"\b(pass\s?words?|pass\s?phrases?|passcodes?|api[\s_-]?keys?|secret[\s_-]?keys?|"
    r"access[\s_-]?keys?|private[\s_-]?keys?|"
    r"(?:auth|access|refresh|bearer|session|api)[\s_-]?tokens?|"
    r"(?:session|auth|login)[\s_-]?cookies?|recovery[\s_-]?(?:codes?|phrases?|keys?)|"
    r"seed[\s_-]?phrases?|backup[\s_-]?codes?|2fa[\s_-]?codes?|security[\s_-]?codes?|"
    r"verification[\s_-]?codes?|credentials?)\b",
    re.IGNORECASE,
)
# Words that are only a secret when a value follows ("my PIN is 4821", "token: abc").
_WEAK_SECRET_WORD = re.compile(
    r"\b(?:pin(?:\s?codes?|\s?numbers?)?|tokens?|secrets?|otp|cvv|cvc)\b"
    r"(?:[^.\n]{0,20}?(?:\bis\b|\bare\b|:|=)\s*\S|[^.\n]{0,30}?\d{3,})",
    re.IGNORECASE,
)
_ID_WORD = re.compile(
    r"\b(passport|national\s?id(?:entity)?|identity\s?card|id\s?card|id\s?number|nic|"
    r"ssn|social\s?security|aadhaar|pan\s?card|driver'?s?\s?licen[cs]e|driving\s?licen[cs]e|"
    r"tax\s?(?:id|number|file\s?number)|tin|"
    r"bank\s?account|account\s?(?:number|no)|iban|swift|routing\s?number|sort\s?code|"
    r"card\s?(?:number|no)|credit\s?card|debit\s?card|visa|mastercard|amex)\b",
    re.IGNORECASE,
)
_HAS_DIGITS = re.compile(r"\d{4,}|\d[\d\s-]{6,}\d")
_SECRET_SHAPES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abprs])[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9]{4}[ ]?){2,7}[A-Z0-9]{1,4}\b"),  # IBAN
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # US SSN
    re.compile(r"\b\d{9}[VvXx]\b"),  # Sri Lanka NIC (old format)
)
_CARD_CANDIDATE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9+=]{24,}")
_SENSITIVE = re.compile(
    r"\b(diagnos\w*|illness|disease|cancer|diabet\w*|hiv|depress\w*|anxiety|therap\w*|"
    r"psychiatr\w*|medication|prescri\w*|pregnan\w*|disabilit\w*|surgery|"
    r"vot(?:e|ed|es|ing)\s+for|political\s+part\w*|politic\w*|election\w*|"
    r"religio\w*|church|mosque|temple|hindu\w*|muslim\w*|christian\w*|buddhis\w*|"
    r"atheis\w*|sexual\w*|gay|lesbian|bisexual|transgender|ethnic\w*|caste|"
    r"criminal\s+record|convict\w*|arrest\w*)\b",
    re.IGNORECASE,
)

# ---- instruction-like text ---------------------------------------------------------------

_ACT = r"(?:approv\w*|allow\w*|grant\w*|permit\w*|authori[sz]\w*|confirm\w*|accept\w*)"
_INSTRUCTION = (
    re.compile(rf"\b(?:always|auto(?:matically)?|never\s+ask\w*|pre-?)\W*{_ACT}", re.I),
    re.compile(rf"\b{_ACT}\s+(?:everything|anything|all\b|any\b|every\b)", re.I),
    re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass|skip|disable|turn\s+off|circumvent)\b"
        r"[^.\n]{0,40}\b(?:rules?|instructions?|polic\w+|approvals?|permissions?|prompts?|"
        r"confirmations?|safety|restrictions?|limits?|guardrails?|checks?)\b",
        re.I,
    ),
    re.compile(r"\bwithout\s+(?:asking|approval|confirm\w*|permission|a\s+prompt)\b", re.I),
    re.compile(r"\b(?:no|do\s+not|don'?t|never)\s+(?:need\s+)?(?:to\s+)?ask\w*\b", re.I),
    re.compile(
        r"\b(?:no\s+(?:approval|confirmation|permission)\s+(?:is\s+)?(?:needed|required)|"
        r"(?:approval|confirmation|permission)\s+(?:is\s+)?not\s+(?:needed|required))",
        re.I,
    ),
    re.compile(r"\b(?:system\s+prompt|developer\s+mode|jailbreak|new\s+instructions?)\b", re.I),
    re.compile(
        r"\b(?:raise|lift|increase|change|set|upgrade|elevate|grant)\b[^.\n]{0,40}"
        r"\b(?:permissions?|privileges?|tiers?|T[0-4]\b|allowlists?|admin\w*|root)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:you|hcwma|the\s+(?:assistant|agent|router|model))\s+"
        r"(?:must|should|shall|may|can|are\s+allowed\s+to|is\s+allowed\s+to|have\s+to|has\s+to)"
        r"\s+(?:now\s+)?(?:delete|remove|run|execute|install|send|upload|download|move|open|"
        r"read|write|call|use|restore|email|post|share|trust)\b",
        re.I,
    ),
    re.compile(r"\b(?:is|are|was|were|has\s+been|have\s+been)\s+(?:pre-?)?approved\b", re.I),
    re.compile(r"\bapproved\s+(?:folders?|tools?|actions?)\b", re.I),
    re.compile(r"</?\s*(?:memory|tool_output|system|instructions?)\b", re.I),
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;\n])\s+|\s*\n+\s*")
_PART_SPLIT = re.compile(r"\s*,\s+(?:and\s+|but\s+)?|\s+(?:and|but|also)\s+", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STOPWORDS = frozenset(
    """a an the and or but if so that this these those is are was were be been being am
    to of in on at for from by with about as into it its he she they them his her their
    i me my mine we us our you your owner owners user users remember remembers note fact
    has have had do does did will would can could should may might not no yes please
    also too very just like likes prefer prefers preferred want wants called named""".split()
)


@dataclass(frozen=True)
class Screened:
    """Result of screening one piece of text before it is stored."""

    text: str  # what may be stored ("" if nothing is left)
    dropped: list[str] = field(default_factory=list)  # kinds of blocked content removed
    rejected: str | None = None  # set when the whole entry is refused

    @property
    def ok(self) -> bool:
        return self.rejected is None and bool(self.text)


def _luhn(digits: str) -> bool:
    total, double = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return total % 10 == 0


def _has_card_number(text: str) -> bool:
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def _has_opaque_token(text: str) -> bool:
    """A long unbroken run mixing upper case, lower case and digits looks like a key."""
    for match in _OPAQUE_TOKEN.finditer(text):
        token = match.group()
        if all(re.search(p, token) for p in (r"[a-z]", r"[A-Z]", r"\d")):
            return True
    return False


def blocked_kind(text: str) -> str | None:
    """Why this piece of text may not be stored, or None if it may."""
    if _SECRET_WORD.search(text) or _WEAK_SECRET_WORD.search(text):
        return "secret"
    if any(p.search(text) for p in _SECRET_SHAPES) or _has_opaque_token(text):
        return "secret"
    if _has_card_number(text):
        return "card_number"
    if _ID_WORD.search(text) and _HAS_DIGITS.search(text):
        return "id_or_account_number"
    return None


def is_instruction(text: str) -> bool:
    """True if the text tries to tell HCWMA what it may do, instead of stating a fact."""
    return any(p.search(text) for p in _INSTRUCTION)


def screen(text: str, *, source: str, max_chars: int = 500) -> Screened:
    """Decide what of `text` may be stored. Never raises."""
    text = text.replace("\x00", " ")
    cleaned = " ".join(text.split())
    if not cleaned:
        return Screened("", rejected="empty")
    if is_instruction(cleaned):
        return Screened("", rejected="instruction")

    def kind_of(piece: str) -> str | None:
        kind = blocked_kind(piece)
        if kind is None and source != "owner_said" and _SENSITIVE.search(piece):
            kind = "sensitive"
        return kind

    kept: list[str] = []
    dropped: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = " ".join(sentence.split())
        if not sentence:
            continue
        if kind_of(sentence) is None:
            kept.append(sentence)
            continue
        # Something in this sentence is blocked: keep only the parts that are not.
        parts = [p.strip() for p in _PART_SPLIT.split(sentence) if p.strip()]
        good = [p for p in parts if kind_of(p) is None]
        dropped.extend(k for k in map(kind_of, parts) if k is not None)
        if len(parts) > 1 and good and kind_of(", ".join(good)) is None:
            kept.append(", ".join(good))
        elif not dropped:
            dropped.append(kind_of(sentence) or "blocked")

    result = " ".join(kept).strip()
    if not result:
        return Screened("", dropped=dropped, rejected="blocked")
    if kind_of(result) is not None:  # never store a result that still trips a rule
        return Screened("", dropped=dropped, rejected="blocked")
    if len(result) > max_chars:
        result = result[: max_chars - 1].rstrip() + "…"
    return Screened(result, dropped=dropped)


def content_words(text: str) -> list[str]:
    """Lower-cased words that carry meaning (used for near-duplicates and grounding)."""
    words = []
    for raw in _WORD.findall(text.casefold()):
        word = raw[:-1] if len(raw) > 3 and raw.endswith("s") else raw
        if word not in _STOPWORDS and raw not in _STOPWORDS:
            words.append(word)
    return words


def grounded_in(text: str, owner_words: str, *, min_share: float = 0.6) -> bool:
    """True if `text` is made of what the owner typed.

    Every number in `text` must appear in the owner's words, and so must most of its other
    content words. A model may rephrase ("Sister's birthday: 12 March") but cannot bring in
    text from somewhere else.
    """
    wanted = content_words(text)
    if not wanted:
        return False
    have = set(content_words(owner_words))
    if any(w not in have for w in wanted if w.isdigit()):
        return False
    hits = sum(1 for w in wanted if w in have)
    return hits / len(wanted) >= min_share


def similarity(a: str, b: str) -> float:
    """Word overlap (Jaccard) between two texts, 0..1."""
    wa, wb = set(content_words(a)), set(content_words(b))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)
