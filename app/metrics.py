"""Talk-time and key moments for a finished interview, derived from its transcript by code (no AI).

- Talk-time counts words, not seconds: live transcript lines carry arrival times, not
  durations. With the live transcript the interviewer's spoken follow-ups are missing,
  so the candidate's share reads high; `basis` says which transcript was used.
- Key moments are the transcript lines behind each evidence quote in the evaluation,
  labelled with the competency or requirement the quote supports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.evaluation import squash

if TYPE_CHECKING:
    from app.models import EvaluationReport

# A quote that spans lines is placed on the line it starts on; at least this many of its words must end that line.
MIN_OPENING_WORDS = 2


def talk_time(report: EvaluationReport) -> dict:
    words = {"interviewer": 0, "candidate": 0}
    per_question: dict[str, int] = {}
    unassigned = 0
    for u in report.transcript:
        if u.role not in words:
            continue
        n = len(u.text.split())
        words[u.role] += n
        if u.role == "candidate":
            if u.question_id:
                per_question[u.question_id] = per_question.get(u.question_id, 0) + n
            else:
                unassigned += n
    total = words["interviewer"] + words["candidate"]
    return {
        "unit": "words",
        "basis": report.transcript_source,
        "interviewer_words": words["interviewer"],
        "candidate_words": words["candidate"],
        "candidate_share": round(words["candidate"] / total, 2) if total else None,
        "per_question": [{"question_id": q, "candidate_words": n} for q, n in per_question.items()],
        "unassigned_candidate_words": unassigned,
    }


def _line_for(quote: str, lines: list[tuple[int, str]]) -> int | None:
    q = squash(quote)
    if not q:
        return None
    for index, text in lines:
        if q in text:
            return index
    # A quote running on into the next line starts at the end of its first line: find the line that ends with
    # the longest opening part of the quote. No match means no moment rather than a guess at an unrelated line.
    words = q.split()
    for k in range(len(words) - 1, MIN_OPENING_WORDS - 1, -1):
        opening = " ".join(words[:k])
        for index, text in lines:
            if text == opening or text.endswith(" " + opening):
                return index
    return None


def key_moments(report: EvaluationReport) -> list[dict]:
    ev = report.evaluation
    if ev is None:
        return []
    lines = [(n, squash(u.text)) for n, u in enumerate(report.transcript) if u.role == "candidate"]
    found = []
    sources = [("competency", s.competency_name, s.evidence) for s in ev.competency_scores]
    sources += [("requirement", r.requirement, r.evidence) for r in ev.requirement_results]
    for kind, label, quotes in sources:
        for quote in quotes:
            line = _line_for(quote, lines)
            if line is not None:
                found.append({"line": line, "kind": kind, "label": label, "quote": quote})
    return sorted(found, key=lambda m: m["line"])
