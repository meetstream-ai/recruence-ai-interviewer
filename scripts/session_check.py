"""Summarise a session after a live interview.

    python scripts/session_check.py            # latest session
    python scripts/session_check.py <session_id>

Reads the local database only. Prints counts, speaker names and timings, plus a
yes/no hint for each open question U1-U5. Candidate words are printed only with --text.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.store import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id", nargs="?")
    parser.add_argument("--text", action="store_true", help="also print the transcript text")
    args = parser.parse_args()

    store = Store(Settings.from_env().database)
    sid = args.session_id or store.latest_session_id()
    if not sid:
        print("No sessions yet.")
        return 1
    session = store.get_session(sid)
    if session is None:
        print(f"Session {sid} not found.")
        return 1
    interview = store.get_interview(session["interview_id"])
    transcript = store.transcript(sid)
    notes = store.notes(sid)
    report = store.get_report(sid)

    print(f"Session {sid} · {session['platform']} · state={session['state']} · ended: {session['end_reason']}")
    print(f"bot_id={session['bot_id']} agent_config_id={session['agent_config_id']}")
    print(f"created {session['created_at']}  ended {session['ended_at']}")

    by_role: dict[str, list] = {}
    for u in transcript:
        by_role.setdefault(u.role, []).append(u)
    candidate = by_role.get("candidate", [])
    interviewer = by_role.get("interviewer", [])
    spoken = [u for u in interviewer if u.speaker.endswith("(spoken)")]
    planned_served = [u for u in interviewer if u.question_id and not u.speaker.endswith("(spoken)")]
    consent = [u for u in by_role.get("system", []) if "agreed" in u.text]

    print(f"\nTranscript: {len(transcript)} lines")
    for role, lines in by_role.items():
        speakers = sorted({u.speaker for u in lines})
        print(f"  {role:<11} {len(lines):>3} lines  speakers: {speakers}")
    if candidate:
        print(f"  first candidate line at {candidate[0].ts.isoformat()}, last at {candidate[-1].ts.isoformat()}")

    print(f"\nPlanned questions served: {len(planned_served)} of {len(interview.plan.questions) if interview and interview.plan else '?'}")
    print(f"Notes from the agent: {sum(1 for n in notes if n.note)} with text, {len(notes)} total")
    if report:
        ev = report.evaluation
        print(f"Report: consent={report.consent_given} end={report.end_reason} "
              f"evaluation={'yes (' + ev.recommendation + ', ' + ev.confidence + ')' if ev else 'no: ' + str(report.evaluation_error)}")
        if ev:
            print(f"  evidence quotes kept: {sum(len(s.evidence) for s in ev.competency_scores)}")

    # Word-for-word check, possible only if the agent's speech shows up in the transcript (U2).
    wording = []
    if interview and interview.plan and spoken:
        for q in interview.plan.questions:
            best = max((difflib.SequenceMatcher(None, q.text.lower(), u.text.lower()).ratio() for u in spoken), default=0)
            wording.append((q.id, best))

    print("\nOpen questions (hints; confirm with what you heard in the call):")
    print(f"  U1 live transcript with MIA attached: {'YES' if candidate else 'NO candidate lines received'} ({len(candidate)} lines)")
    print(f"  U2 agent speech in live transcript:   {'YES' if spoken else 'not seen'}"
          + (f" as {sorted({u.speaker for u in spoken})}" if spoken else ""))
    print(f"  U3 agent confirmed consent:           {'YES' if consent else 'NO'}"
          + ("" if consent else " (check the server log for consent_required)"))
    if wording:
        print("  U4 closest spoken match per planned question: " + ", ".join(f"{qid}={r:.0%}" for qid, r in wording))
    else:
        print("  U4 word-for-word: not measurable from the transcript; judge by ear")
    print("  U5 echo: check the server log for 'dropped a likely echo' and look for candidate lines that repeat the AI")

    if args.text:
        print("\n--- transcript ---")
        for u in transcript:
            print(f"[{u.ts.strftime('%H:%M:%S')}] {u.role[:4]} {u.speaker}{' ' + u.question_id if u.question_id else ''}: {u.text}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
