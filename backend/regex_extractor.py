"""Regex-based question extractor — no AI required.
Handles: '1.', 'Q1.', '(1)' numbering with '1) 2) 3) 4)' or 'A. B. C. D.' options,
plus 'Answer: X' or a trailing answer key block. All patterns tuned for CBSE / NEET /
JEE style question papers."""
import re
from typing import List, Tuple, Optional

# Question anchor: numbered question uses 'N.' or 'QN.' (dot), never 'N)'
# (parentheses/round-bracket numbering is reserved for options and answer-key entries).
Q_ANCHOR = re.compile(r"(?m)^\s*(?:Q\.?\s*)?(\d{1,3})\.\s+", re.IGNORECASE)

# Option patterns inside a question block — MULTILINE so '$' means end-of-line.
OPT_PATTERNS = [
    re.compile(r"(?m)(?:(?<=^)|(?<=\s))([1-4])\)\s*([^\d\n][^\n]*?)(?=\s{2,}[1-4]\)|\s*$)"),  # "1) foo    2) bar"
    re.compile(r"(?m)(?:(?<=^)|(?<=\s))\(([1-4])\)\s*([^\n]+?)(?=\s*\([1-4]\)|\s*$)"),        # "(1) foo"
    re.compile(r"(?m)^\s*([A-D])[\.)]\s+(.+?)$"),                                             # "A. foo"
    re.compile(r"(?m)^\s*\(([a-d])\)\s+(.+?)$"),                                              # "(a) foo"
]

ANS_INLINE = re.compile(r"(?i)Ans(?:wer)?\s*[:\.\-]?\s*([1-4A-Da-d])")

# Answer-key section marker (case-insensitive)
ANSKEY_MARKER = re.compile(r"(?im)^\s*(?:Answer[s]?[\s_]*(?:Key)?|Ans[s]?[\s_]*Key|Key)\s*[:\-]?\s*$")

# Per-question answer key entry inside the answer-key block
ANSKEY_ENTRY = re.compile(r"(?i)(?<![\w-])(\d{1,3})[\.):\-]\s*\(?\s*([1-4A-Da-d])(?![\w])")


def _normalize_option(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().rstrip(".,;:")


def _split_answer_key(text: str) -> Tuple[str, str]:
    """Return (question_text, answer_key_text). Uses 'Answer key:' style marker if
    present; otherwise falls back to a heuristic that detects a compact answer table
    like '1) 2   2) 4   3) 3 ...' near the end."""
    m = ANSKEY_MARKER.search(text)
    if m:
        return text[:m.start()], text[m.end():]
    # Heuristic: if the last 20% of the text contains lots of 'N) L' pairs with SHORT gaps,
    # treat that block as answer key. Only useful when questions themselves are longer.
    tail_start = max(0, int(len(text) * 0.8))
    tail = text[tail_start:]
    tail_entries = ANSKEY_ENTRY.findall(tail)
    if len(tail_entries) >= 5 and len(tail) < 1500:
        return text[:tail_start], tail
    return text, ""


def _letter_from_ans(ans: str) -> Optional[str]:
    ans = ans.upper()
    if ans.isdigit():
        n = int(ans)
        return chr(ord("A") + n - 1) if 1 <= n <= 4 else None
    if ans in ("A", "B", "C", "D"): return ans
    return None


def _extract_answer_key(key_text: str) -> dict:
    """Parse an answer-key blob into {q_number: 'A'|'B'|'C'|'D'}."""
    key: dict = {}
    if not key_text: return key
    for m in ANSKEY_ENTRY.finditer(key_text):
        try:
            qn = int(m.group(1))
            letter = _letter_from_ans(m.group(2))
            if letter and qn not in key:
                key[qn] = letter
        except Exception:
            continue
    return key


def _parse_options_from_block(block: str) -> List[str]:
    """Return the option list (list of strings). Empty if no usable pattern found."""
    for patt in OPT_PATTERNS:
        matches = patt.findall(block)
        if len(matches) >= 2:
            def label_order(m):
                lbl = str(m[0]).upper()
                return int(lbl) if lbl.isdigit() else ord(lbl)
            matches = sorted(matches, key=label_order)
            options = [_normalize_option(m[1]) for m in matches]
            options = [o for o in options if o]
            if len(options) >= 2:
                return options[:6]
    return []


def _clean_question_text(text: str) -> str:
    """Strip trailing option markers + inline 'Answer:' hints from the question stem."""
    cuts = []
    # cut where options start
    for patt in [r"(?<!\S)[1-4]\)\s+\S", r"\([1-4]\)\s+\S", r"(?m)^\s*[A-D][\.)]\s+\S", r"(?m)^\s*\([a-d]\)\s+\S"]:
        m = re.search(patt, text)
        if m: cuts.append(m.start())
    if cuts:
        text = text[:min(cuts)]
    text = re.sub(r"(?i)\s*Answer\s*[:\-].*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def regex_extract_questions(text: str, subject_hint: str = "Physics") -> Tuple[List[dict], List[str]]:
    """Extract questions with regex only. Returns (questions, warnings)."""
    warnings: List[str] = []
    text = (text or "").strip()
    if not text: return [], ["Empty document"]
    text = text.replace("\r", "\n").replace("\u00ad", "")

    # 1) Peel off answer-key section first so its 'N) L' entries don't get mistaken for questions
    q_text, key_text = _split_answer_key(text)
    ans_key = _extract_answer_key(key_text)

    # 2) Anchor scan over the questions-only part
    anchors = list(Q_ANCHOR.finditer(q_text))
    if len(anchors) < 1:
        return [], ["No numbered questions detected. Use '1.', '2.', '3.' numbering or paste text with the 'Paste text' tab."]

    questions: List[dict] = []
    for idx, m in enumerate(anchors):
        try: qn = int(m.group(1))
        except Exception: qn = idx + 1
        start = m.end()
        end = anchors[idx + 1].start() if idx + 1 < len(anchors) else len(q_text)
        block = q_text[start:end].strip()

        options = _parse_options_from_block(block)
        stem = _clean_question_text(block)
        if not stem or len(stem) < 6:
            continue

        # Correct answer: first from the answer-key block, then from inline 'Answer: X'
        correct: List[str] = []
        if qn in ans_key:
            correct = [ans_key[qn]]
        else:
            am = ANS_INLINE.search(block)
            if am:
                letter = _letter_from_ans(am.group(1))
                if letter: correct = [letter]

        # Infer type
        q_type = "mcq_single" if len(options) >= 2 else "subjective"
        if len(options) == 2 and all(
            re.match(r"(?i)^(true|false|yes|no|t|f)$", o.strip()) for o in options
        ):
            q_type = "true_false"

        questions.append({
            "type": q_type,
            "subject": subject_hint or "Physics",
            "chapter": "",
            "topic": "",
            "difficulty": "medium",
            "marks": 4,
            "negative_marks": 1,
            "text": stem,
            "options": options,
            "correct": correct,
            "explanation": "",
            "language": "English",
            "status": "approved",
            "source": "regex_import",
        })

    if not questions:
        warnings.append(f"Detected {len(anchors)} anchors but no valid question blocks.")
    return questions, warnings
