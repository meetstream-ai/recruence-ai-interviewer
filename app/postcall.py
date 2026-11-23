"""Rebuild the full interview transcript, including the AI's spoken follow-ups, after the call.

Why: MeetStream's live transcript carries only human speech, so the agent's follow-up
questions are missing from it (live test 2026-10-28). The post-call transcript of the
recording does contain them, but on Google Meet every word is credited to the human
participant: the bot is not a speaker in Meet's attribution.

How: the post-call words are split into tokens and aligned with two references:
  - the live candidate lines (human speech only)  -> candidate
  - text we know the agent said: its greeting and the planned questions it was served -> interviewer
Scripted matches win. Everything else is the agent's unscripted speech (acknowledgements,
follow-ups, goodbye). Short islands between two runs of the other speaker are smoothed.
Speaker labels are therefore inferred and marked as such.

The recording's transcript can also lose candidate speech the live transcript caught (live
Meet call 17, 2026-11-01: a whole answer to question 2 was missing). A live candidate line the
recording mostly doesn't account for is kept as it is, after its planned question and after the
candidate's earlier answers.
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from app.models import Utterance

GAP_IN_ANSWER = 5  # unmatched gaps shorter than this inside the candidate's speech are transcription differences
MIN_COVERAGE = 0.3  # share of the live candidate words the recording must account for, or it isn't this call
# Share of the agent's scripted words (greeting, planned questions) the recording must contain. On Teams
# (live test 2026-10-29) the recording had no agent audio at all; merging then made the transcript worse.
MIN_SCRIPT_COVERAGE = 0.5
# A live candidate line with less than this share of its words found in the recording is missing from it.
MIN_LINE_COVERAGE = 0.5
CANDIDATE, INTERVIEWER, SCRIPTED = "candidate", "interviewer", "scripted"


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9']", "", token.lower())


def _post_tokens(post: Any) -> list[tuple[str, float]]:
    """(token, start seconds) for every word of a MeetStream post-call transcript, in order.

    Handles both documented shapes: participant-keyed (`words[].text` are phrase chunks
    with `start_timestamp`) and segment-list (`transcript`, `start_time`).
    """
    out: list[tuple[str, float]] = []
    for item in post if isinstance(post, list) else []:
        if not isinstance(item, dict):
            continue
        if "participant" in item:
            for w in item.get("words") or []:
                start = ((w.get("start_timestamp") or {}).get("relative")) or 0.0
                out.extend((tok, float(start)) for tok in str(w.get("text", "")).split())
        elif "transcript" in item:
            start = float(item.get("start_time") or 0.0)
            out.extend((tok, start) for tok in str(item["transcript"]).split())
    return out


def _mark(post_norm: list[str], ref_tokens: list[str], labels: list[Optional[str]], label: str, ids: list, ref_ids: list) -> None:
    ref_norm = [_norm(t) for t in ref_tokens]
    sm = difflib.SequenceMatcher(None, post_norm, ref_norm, autojunk=False)
    for a, b, size in sm.get_matching_blocks():
        if size < 2 and label == SCRIPTED:
            continue  # single-word coincidences with scripted text ("the", "you") prove nothing
        for k in range(size):
            labels[a + k] = label
            ids[a + k] = ref_ids[b + k]


def _runs(labels: list) -> list[tuple[int, int]]:
    runs, i = [], 0
    while i < len(labels):
        j = i
        while j < len(labels) and labels[j] == labels[i]:
            j += 1
        runs.append((i, j))
        i = j
    return runs


def _resolve(labels: list[Optional[str]], whole_answer: list[bool]) -> list[str]:
    """CANDIDATE / SCRIPTED / None (unmatched) -> CANDIDATE or INTERVIEWER.

    Matched candidate stretches are never relabelled, except a lone matched word with no
    candidate speech on either side (a coincidence like "you" or "the"). A lone word that was
    a whole live line on its own ("Yes.") is a real answer and is kept.
    """
    out = list(labels)
    # 1. Short unmatched gaps between candidate words are the two transcriptions disagreeing.
    for i, j in _runs(out):
        if out[i] is None and i > 0 and j < len(out) and out[i - 1] == CANDIDATE == out[j] and j - i < GAP_IN_ANSWER:
            for k in range(i, j):
                out[k] = CANDIDATE
    # 2. A single candidate word still standing alone is a coincidence inside the agent's speech.
    for i, j in _runs(out):
        if out[i] == CANDIDATE and j - i == 1 and not whole_answer[i]:
            out[i] = None
    # 3. Everything else unmatched is the agent's own wording.
    return [INTERVIEWER if lab in (None, SCRIPTED) else lab for lab in out]


def merge_postcall(
    post: Any,
    live: list[Utterance],
    *,
    agent_name: str,
    candidate_name: str,
    started_at: datetime,
) -> list[Utterance]:
    """Return the interview as utterances with inferred speakers, or [] if the post-call transcript is unusable.

    `live` is the session transcript: candidate lines (human speech) plus the interviewer lines the
    app recorded itself (greeting, planned questions with their question ids).
    """
    tokens = _post_tokens(post)
    if not tokens:
        return []
    post_norm = [_norm(t) for t, _ in tokens]
    labels: list[Optional[str]] = [None] * len(tokens)
    qids: list[Optional[str]] = [None] * len(tokens)

    cand_lines = [u for u in live if u.role == "candidate"]
    cand_tokens, cand_single, cand_line = [], [], []
    for n, u in enumerate(cand_lines):
        words = [t for t in u.text.split() if _norm(t)]
        cand_tokens.extend(words)
        cand_single.extend([len(words) == 1] * len(words))
        cand_line.extend([n] * len(words))
    # The id slot carries the position of the matched live word, for "whole one-word answer" and line coverage.
    cand_pos: list = [None] * len(tokens)
    _mark(post_norm, cand_tokens, labels, CANDIDATE, cand_pos, list(range(len(cand_tokens))))
    whole_answer = [p is not None and cand_single[p] for p in cand_pos]

    script_tokens, script_ids = [], []
    for u in live:
        if u.role == "interviewer" and not u.speaker.endswith("(spoken)"):
            for t in u.text.split():
                if _norm(t):
                    script_tokens.append(t)
                    script_ids.append(u.question_id or "")
    scripted_labels: list[Optional[str]] = [None] * len(tokens)
    _mark(post_norm, script_tokens, scripted_labels, SCRIPTED, qids, script_ids)
    scripted = [lab == SCRIPTED for lab in scripted_labels]
    if script_tokens and sum(scripted) < MIN_SCRIPT_COVERAGE * len(script_tokens):
        return []  # the recording doesn't contain the agent's voice: the live transcript is the better record
    for k, s in enumerate(scripted):
        if s:
            labels[k] = SCRIPTED  # what the agent was given to say wins over a candidate match

    final = _resolve(labels, whole_answer)  # unmatched speech outside answers is the agent's own wording
    matched = sum(1 for k, lab in enumerate(labels) if lab == CANDIDATE and final[k] == CANDIDATE)
    if not cand_tokens or matched < MIN_COVERAGE * len(cand_tokens):
        return []  # the recording doesn't line up with what the candidate said: don't guess

    inserts = _missing_lines(cand_lines, cand_line, cand_pos, final, scripted, qids)

    # Question context: the latest planned question the agent has asked so far.
    current_q: Optional[str] = None
    result: list[Utterance] = []
    run: list[str] = []
    run_role, run_start, run_q = None, 0.0, None

    def flush() -> None:
        if run:
            speaker = f"{agent_name} (from recording)" if run_role == INTERVIEWER else candidate_name
            result.append(
                Utterance(
                    speaker=speaker,
                    role=run_role,
                    text=" ".join(run),
                    ts=started_at + timedelta(seconds=run_start),
                    question_id=run_q,
                )
            )

    def keep(lines: list[Utterance]) -> None:
        for u in lines:
            result.append(u.model_copy(update={"speaker": candidate_name, "question_id": u.question_id or current_q}))

    keep(inserts.get(-1, []))
    for k, (tok, start) in enumerate(tokens):
        if scripted[k] and qids[k]:
            current_q = qids[k]
            if run_role == INTERVIEWER and final[k] == INTERVIEWER:
                run_q = current_q  # "Thanks. Next, ...": the whole line belongs to the question it asks
        if final[k] != run_role:
            flush()
            run, run_role, run_start, run_q = [], final[k], start, current_q
        run.append(tok)
        if k in inserts:
            flush()
            run, run_role = [], None
            keep(inserts[k])
    flush()
    return result


def _missing_lines(
    cand_lines: list[Utterance],
    cand_line: list[int],
    cand_pos: list[Optional[int]],
    final: list[str],
    scripted: list[bool],
    qids: list[Optional[str]],
) -> dict[int, list[Utterance]]:
    """Live candidate lines the recording mostly lacks, keyed by the recording token they follow (-1: the start).

    A line goes after the end of its planned question (the greeting for a line with no question)
    and after the last recorded word of the candidate's earlier lines, whichever is later.
    """
    words = [0] * len(cand_lines)
    for line in cand_line:
        words[line] += 1
    found = [0] * len(cand_lines)
    last_token = [-1] * len(cand_lines)
    for k, p in enumerate(cand_pos):
        if p is not None and final[k] == CANDIDATE:
            found[cand_line[p]] += 1
            last_token[cand_line[p]] = max(last_token[cand_line[p]], k)

    # Where each planned question (and the greeting, id "") first finishes in the recording.
    question_end: dict[str, int] = {}
    k = 0
    while k < len(scripted):
        if scripted[k]:
            qid = qids[k] or ""
            j = k
            while j + 1 < len(scripted) and scripted[j + 1] and (qids[j + 1] or "") == qid:
                j += 1
            question_end.setdefault(qid, j)
            k = j
        k += 1

    inserts: dict[int, list[Utterance]] = {}
    after_earlier = -1  # also covers earlier kept lines, so the live order is never reversed
    for n, u in enumerate(cand_lines):
        if words[n] and found[n] < MIN_LINE_COVERAGE * words[n]:
            anchor = max(after_earlier, question_end.get(u.question_id or "", -1))
            inserts.setdefault(anchor, []).append(u)
            after_earlier = anchor
        else:
            after_earlier = max(after_earlier, last_token[n])
    return inserts
