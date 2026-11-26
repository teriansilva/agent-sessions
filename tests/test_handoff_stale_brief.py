"""A handoff brief describes the source session as it stands NOW (#2418).

The observed failure: a two-hop chain (API session → API session → …) whose middle session did
all of its later work in turns it ran for itself (wake-ups, background completions). The fold every
consumer reads an API session through kept only operator operations, so the brief said "not yet
done" about work finished and pushed hours earlier, and its `task:` was the previous hop's brief.
Every transcript here is synthetic.
"""

import asyncio
import json
import urllib.parse
import uuid

import pytest

import test_native_journal as journal_tests
from agent_sessions import handoff, metadata, native_journal, prompts, review, transcript
from test_native_agent_turns import T0, agent, at, background, own
from test_native_journal import event, request

conversation = journal_tests.conversation

# Hop 0 → hop 1: a brief written when the work had only been planned.
HOP0_TASK = "Plan the MVP and file its issues"
HOP0_STATE = "Subagents are still drafting the issue texts; no issue has been created yet."


def _hop0_brief(monkeypatch) -> str:
    """The brief the FIRST handoff delivered, built by the real Quick builder."""
    turns = [
        transcript.Turn("user", HOP0_TASK, ts=T0 - 7200),
        transcript.Turn("assistant", HOP0_STATE, ts=T0 - 7100),
    ]
    # A scoped patch: `monkeypatch.undo()` would also undo the conftest's autouse pins.
    with monkeypatch.context() as m:
        m.setattr(transcript, "adapter_for", lambda _e: lambda *_a: turns)
        seed, _ = handoff.build_quick_seed("opencode-api", "ses_hop0", title=HOP0_TASK)
    return seed


def _submit(root, sid, binding, monkeypatch, t, text):
    at(monkeypatch, t)
    claim = request(binding, revision=native_journal.read(root, sid).revision, text=text)
    op = claim["params"]["operation_id"]
    native_journal.claim(root, claim)
    native_journal.record_handoff(root, binding, op, "sent")
    return op


def _reply(root, sid, binding, monkeypatch, t, op, text):
    at(monkeypatch, t)
    done = {"kind": "turn_completed", "data": {"operation_id": op, "state": "completed"}}
    done["data"].update(native_turn_id="native-turn-1", error="")
    native_journal.append_events(root, binding, [event(op, text), done])


def _hop1_session(conversation, monkeypatch, brief):
    """Hop 1: delivered the hop-0 brief, acknowledged it, then worked on in its OWN turns."""
    root, sid, binding = conversation
    op = _submit(root, sid, binding, monkeypatch, T0, brief)
    _reply(root, sid, binding, monkeypatch, T0 + 30, op, "Picking this up; checking the drafts.")
    agent(root, sid, binding, monkeypatch, T0 + 3600, "Created issues #11-#14 and the tracker #10.")
    agent(root, sid, binding, monkeypatch, T0 + 7200, "Pushed docs/ROADMAP.md; PR #2 waits for CI.")
    return root, sid


def _read_as_source(monkeypatch, root, sid):
    """Make `sid`'s journal the source transcript, through the real fold."""
    monkeypatch.setattr(
        transcript, "adapter_for", lambda _e: lambda *_a: transcript._native_turns(root, sid)
    )


# --- the fold: an API session's own turns are part of its transcript -------------------------


def test_own_turns_are_folded_after_the_message_they_followed(conversation, monkeypatch):
    root, sid, binding = conversation
    op = _submit(root, sid, binding, monkeypatch, T0, "Watch PR #2.")
    _reply(root, sid, binding, monkeypatch, T0 + 5, op, "Watching.")
    agent(root, sid, binding, monkeypatch, T0 + 60, "CI is green.")
    later = _submit(root, sid, binding, monkeypatch, T0 + 120, "Merge it.")
    _reply(root, sid, binding, monkeypatch, T0 + 125, later, "Merged.")
    turns = transcript._native_turns(root, sid)
    texts = [(t.role, t.text) for t in turns if t.kind == "text"]
    assert texts == [
        ("user", "Watch PR #2."),
        ("assistant", "Watching."),
        ("assistant", "CI is green."),
        ("user", "Merge it."),
        ("assistant", "Merged."),
    ]
    # The own turn's tool call is folded too, inside its turn.
    kinds = [(t.role, t.kind) for t in turns]
    assert kinds.index(("assistant", "tool")) == 2
    # Timestamped: the operator turn at its claim, a reply at its newest text.
    stamps = {t.text: t.ts for t in turns if t.kind == "text"}
    assert stamps["Watch PR #2."] == T0 and stamps["CI is green."] == T0 + 60


def test_an_own_turn_that_recorded_nothing_adds_nothing(conversation, monkeypatch):
    root, sid, binding = conversation
    op = _submit(root, sid, binding, monkeypatch, T0, "Hi")
    _reply(root, sid, binding, monkeypatch, T0 + 1, op, "Hello")
    target = str(uuid.uuid4())
    native_journal.append_events(root, binding, [background(target), background(None, "completed")])
    assert [t.text for t in transcript._native_turns(root, sid)] == ["Hi", "Hello"]


def test_an_agent_turn_id_equal_to_an_operation_id_keeps_its_own_output(conversation, monkeypatch):
    """Units are keyed by FIELD (the `native_events` rule): a collision never merges two turns."""
    root, sid, binding = conversation
    op = _submit(root, sid, binding, monkeypatch, T0, "Hi")
    _reply(root, sid, binding, monkeypatch, T0 + 1, op, "Operator reply")
    at(monkeypatch, T0 + 60)
    native_journal.append_events(
        root, binding, [background(op), own(op, "Own reply"), background(None, "completed")]
    )
    texts = [t.text for t in transcript._native_turns(root, sid) if t.kind == "text"]
    assert texts == ["Hi", "Operator reply", "Own reply"]


def test_own_turns_are_ordered_by_journal_position_not_by_clock(conversation, monkeypatch):
    """A wall clock that steps backwards must not move a later own turn before an earlier one."""
    root, sid, binding = conversation
    op = _submit(root, sid, binding, monkeypatch, T0, "Start")
    _reply(root, sid, binding, monkeypatch, T0 + 5, op, "Started")
    agent(root, sid, binding, monkeypatch, T0 - 3600, "After, despite the clock")
    texts = [t.text for t in transcript._native_turns(root, sid) if t.kind == "text"]
    assert texts == ["Start", "Started", "After, despite the clock"]


def test_a_streamed_only_own_turn_reaches_the_transcript(conversation, monkeypatch):
    root, sid, binding = conversation
    op = _submit(root, sid, binding, monkeypatch, T0, "Hi")
    _reply(root, sid, binding, monkeypatch, T0 + 1, op, "Hello")
    target = str(uuid.uuid4())
    piece = {"agent_turn": target, "partial": True, "truncated": False, "item_id": "m1"}
    native_journal.append_events(
        root,
        binding,
        [
            background(target),
            {"kind": "text", "data": {**piece, "text": "CI is "}},
            {"kind": "text", "data": {**piece, "text": "green."}},
            background(None, "completed"),
        ],
    )
    texts = [t.text for t in transcript._native_turns(root, sid) if t.kind == "text"]
    assert texts == ["Hi", "Hello", "CI is green."]


def test_an_untimed_transcript_says_its_last_message_time_is_unknown(monkeypatch):
    turns = [transcript.Turn("user", "go"), transcript.Turn("assistant", "done")]
    monkeypatch.setattr(transcript, "adapter_for", lambda _e: lambda *_a: turns)
    seed, _ = handoff.build_quick_seed("claude", "s1")
    assert "\n- last message: unknown (the transcript records no times)\n" in seed
    assert "\n- state as of: " in seed and seed.split("- state as of: ")[1][19] == "Z"


def test_an_inherited_task_title_is_redacted(monkeypatch):
    monkeypatch.setattr(handoff.template_secrets, "redaction_values", lambda: ["hunter2-token"])
    turns = [transcript.Turn("user", "go")]
    monkeypatch.setattr(transcript, "adapter_for", lambda _e: lambda *_a: turns)
    seed, _ = handoff.build_quick_seed("claude", "s1", title="deploy with hunter2-token")
    assert "hunter2-token" not in seed and "- task: deploy with [secret]" in seed

    async def fake(messages, **_kw):
        return {"state": "done"}

    monkeypatch.setattr(review, "complete_json", fake)
    seed, _ = asyncio.run(handoff.build_ai_seed("claude", "s1", title="deploy with hunter2-token"))
    assert "hunter2-token" not in seed


# --- the brief: latest state, honest task, a timestamp -----------------------------------------


def test_a_multi_hop_quick_brief_carries_the_work_done_after_the_inherited_brief(
    conversation, monkeypatch
):
    brief = _hop0_brief(monkeypatch)
    root, sid = _hop1_session(conversation, monkeypatch, brief)
    _read_as_source(monkeypatch, root, sid)
    # The scan keeps only the first line of the first message (`metadata.title_candidate`).
    first = metadata.title_candidate(brief)
    seed, meta = handoff.build_quick_seed("claude-api", sid, first_message=first)

    # The latest state is in it, last.
    assert "Created issues #11-#14" in seed
    assert seed.rstrip().endswith("Pushed docs/ROADMAP.md; PR #2 waits for CI.")
    # `task:` is the original task, marked inherited, never the old brief's text.
    task = next(line for line in seed.splitlines() if line.startswith("- task: "))
    assert task == f"- task: {HOP0_TASK} (inherited from opencode-api:ses_hop0)"
    assert "no issue has been created" not in task
    # It says how old it is: built now, the newest message at the last own turn.
    assert "\n- state as of: " in seed
    assert "\n- last message: " + handoff._iso(T0 + 7200) + "\n" in seed
    assert meta["mode"] == "quick"


def test_an_inherited_brief_in_the_tail_is_labelled_older(conversation, monkeypatch):
    brief = _hop0_brief(monkeypatch)
    root, sid = _hop1_session(conversation, monkeypatch, brief)
    _read_as_source(monkeypatch, root, sid)
    seed, _ = handoff.build_quick_seed("claude-api", sid)
    assert f"[{handoff.INHERITED_LABEL}] # Handoff — continued from a opencode-api" in seed
    assert "[user] # Handoff" not in seed


def test_a_multi_hop_ai_brief_is_asked_about_the_latest_turns(conversation, monkeypatch):
    brief = _hop0_brief(monkeypatch)
    root, sid = _hop1_session(conversation, monkeypatch, brief)
    _read_as_source(monkeypatch, root, sid)
    sent: list[list[dict]] = []

    async def fake(messages, **_kw):
        sent.append(messages)
        return {"state": "Issues filed; roadmap pushed; PR #2 waits for CI.", "next_steps": []}

    monkeypatch.setattr(review, "complete_json", fake)
    seed, meta = asyncio.run(
        handoff.build_ai_seed("claude-api", sid, first_message=metadata.title_candidate(brief))
    )
    system, user = sent[0][0]["content"], sent[0][1]["content"]
    # The endpoint sees the own-turn work, newest last, and the inherited brief marked as older.
    assert user.rstrip().endswith("[agent] Pushed docs/ROADMAP.md; PR #2 waits for CI.")
    assert f"[{handoff.INHERITED_LABEL}]" in user
    assert system == prompts.effective("handoff_brief")
    assert "supersede" in system and "earlier handoff brief" in system
    assert f"- task: {HOP0_TASK} (inherited from opencode-api:ses_hop0)" in seed
    assert "- last message: " + handoff._iso(T0 + 7200) in seed
    assert meta["turns"] == 4  # the brief, its acknowledgement, two own turns


def test_the_old_handoff_prompt_default_is_retired():
    p = next(p for p in prompts.REGISTRY if p.id == "handoff_brief")
    assert p.retired_defaults and p.default != ""


# --- task_title ---------------------------------------------------------------------------------


def test_task_title_rules():
    plain = "Fix the flaky upload test"
    assert handoff.task_title("Renamed by the operator", plain) == "Renamed by the operator"
    assert handoff.task_title("", plain) == plain
    brief = "# Handoff — continued from a codex session\n\n## Source\n- session: codex:abc\n"
    assert handoff.task_title("", brief) == "(inherited from codex:abc)"
    brief_with_task = brief + "- task: Ship it\n"
    assert handoff.task_title("", brief_with_task) == "Ship it (inherited from codex:abc)"
    # A third hop keeps the FIRST origin rather than nesting markers.
    hop2 = brief.replace("codex:abc", "claude:def") + (
        "- task: Ship it (inherited from codex:abc)\n"
    )
    assert handoff.task_title("", hop2) == "Ship it (inherited from codex:abc)"
    # The scan's first line names no task: the transcript's copy of the brief does.
    line = metadata.title_candidate(brief_with_task)
    assert handoff.task_title("", line) == "(inherited from an earlier session)"
    assert handoff.task_title("", line, [("user", brief_with_task)]) == (
        "Ship it (inherited from codex:abc)"
    )


# --- recent commits: facts, never at the brief's expense ---------------------------------------


_COMMITS = {
    "commits": [
        {"short": "a0fd68d", "time": int(T0), "subject": "fix: review notes", "pushed": False},
        {"short": "b1c2d3e", "time": int(T0 - 60), "subject": "docs: roadmap", "pushed": True},
    ]
}


def test_recent_commits_are_listed_after_the_brief(monkeypatch):
    from agent_sessions import gitpanel

    monkeypatch.setattr(gitpanel, "git_log", lambda path, limit: _COMMITS)
    turns = [transcript.Turn("user", "go"), transcript.Turn("assistant", "done")]
    monkeypatch.setattr(transcript, "adapter_for", lambda _e: lambda *_a: turns)
    seed, _ = handoff.build_quick_seed("claude", "s1", commits_cwd="/work/repo")
    assert seed.index("## Recent turns") < seed.index("## Recent commits")
    assert f"- a0fd68d {handoff._iso(T0)} fix: review notes (not pushed)" in seed
    assert "- b1c2d3e" in seed and "docs: roadmap\n" in seed


def test_commits_are_dropped_whole_rather_than_evict_the_brief(monkeypatch):
    doc = "x" * (handoff.SEED_CAP_BYTES - 10) + "\n"
    assert handoff._with_commits(doc, ["- abc a long subject line"]) == doc


def test_commits_fit_up_to_the_exact_cap():
    line = "- abc1234 subject"
    section = "\n".join(["", "## Recent commits (source workdir, newest first)", "", line]) + "\n"
    room = handoff.SEED_CAP_BYTES - len(section.encode("utf-8"))
    exact = "x" * (room - 1) + "\n"
    assert len(handoff._with_commits(exact, [line]).encode("utf-8")) == handoff.SEED_CAP_BYTES
    over = "x" * room + "\n"
    assert handoff._with_commits(over, [line]) == over


def test_commits_come_from_the_source_workdir_never_the_launch_folder(monkeypatch):
    """An adopted session launches in its project's default folder; its history is the source's."""
    from agent_sessions import gitpanel

    asked: list[str] = []
    monkeypatch.setattr(gitpanel, "git_log", lambda path, limit: asked.append(path) or _COMMITS)
    turns = [transcript.Turn("user", "go"), transcript.Turn("assistant", "done")]
    monkeypatch.setattr(transcript, "adapter_for", lambda _e: lambda *_a: turns)
    handoff.build_quick_seed("claude", "s1", cwd="/projects/default", commits_cwd="/home/u/src")
    assert asked == ["/home/u/src"]
    asked.clear()
    handoff.build_quick_seed("claude", "s1", cwd="/projects/default")
    assert asked == []  # no source workdir given → no commits, never the launch folder's


def test_a_git_failure_omits_the_commits(monkeypatch):
    from agent_sessions import gitpanel

    def boom(*_a):
        raise gitpanel.GitError("not a repo")

    monkeypatch.setattr(gitpanel, "git_log", boom)
    assert handoff.recent_commits("/nowhere") == []


def test_commits_are_never_sent_to_the_ai_endpoint(monkeypatch):
    from agent_sessions import gitpanel

    monkeypatch.setattr(gitpanel, "git_log", lambda path, limit: _COMMITS)
    turns = [transcript.Turn("user", "go"), transcript.Turn("assistant", "done")]
    monkeypatch.setattr(transcript, "adapter_for", lambda _e: lambda *_a: turns)
    sent: list[str] = []

    async def fake(messages, **_kw):
        sent.append(json.dumps(messages))
        return {"state": "done"}

    monkeypatch.setattr(review, "complete_json", fake)
    seed, _ = asyncio.run(handoff.build_ai_seed("claude", "s1", commits_cwd="/work/repo"))
    assert "fix: review notes" in seed
    assert "review notes" not in sent[0] and "a0fd68d" not in sent[0]


@pytest.mark.parametrize("failure", ["unavailable", "secret"])
def test_commit_subjects_are_redacted_and_fail_closed(monkeypatch, failure):
    from agent_sessions import gitpanel

    commits = {"commits": [{"short": "abc1234", "time": None, "subject": "use hunter2-token"}]}
    monkeypatch.setattr(gitpanel, "git_log", lambda path, limit: commits)
    if failure == "unavailable":

        def refuse():
            raise handoff.template_secrets.RedactionUnavailable("store unreadable")

        monkeypatch.setattr(handoff.template_secrets, "redaction_values", refuse)
        assert handoff.recent_commits("/work/repo") == []
    else:
        monkeypatch.setattr(handoff.template_secrets, "redaction_values", lambda: ["hunter2-token"])
        (line,) = handoff.recent_commits("/work/repo")
        assert "hunter2-token" not in line and "abc1234" in line


# --- the route: the AI → Quick fallback keeps the same rules ------------------------------------


def test_the_route_degrades_ai_to_quick_with_the_inherited_task_and_latest_state(
    auth_cfg, fake_jsonl, monkeypatch
):
    import test_handoff as th

    src = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a" / f"{th._SRC}.jsonl"
    if not src.exists():  # the fixture's layout: find the source by its id
        src = next(fake_jsonl.rglob(f"{th._SRC}.jsonl"))
    brief = "# Handoff — continued from a codex session\n\n## Source\n- session: codex:abc\n"
    brief += "- task: Plan the MVP\n\n## State\n\nNothing filed yet.\n"
    records = [
        {"type": "user", "timestamp": "2026-11-26T10:00:00Z", "message": {"content": brief}},
        {
            "type": "assistant",
            "timestamp": "2026-11-26T14:47:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Filed #1-#4."}]},
        },
    ]
    src.write_text("".join(json.dumps(r) + "\n" for r in records))
    th._present_all(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(handoff, "recent_commits", lambda cwd: asked.append(cwd) or [])

    async def boom(messages, *, model=None):
        raise review.ReviewError("endpoint returned HTTP 500")

    monkeypatch.setattr(review, "complete_json", boom)
    c = th._client(auth_cfg)
    csrf = th._login(c, auth_cfg)
    r = th._prepare(c, csrf, auth_cfg, mode="ai")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meta"]["mode"] == "quick" and body["meta"]["degraded"] is True
    preview = body["preview"]
    assert "- task: Plan the MVP (inherited from codex:abc)\n" in preview
    assert "- last message: 2026-11-26T14:47:00Z\n" in preview
    assert preview.rstrip().endswith("[agent] Filed #1-#4.")
    assert f"[{handoff.INHERITED_LABEL}] # Handoff" in preview
    assert len(asked) == 1 and asked[0]  # the source's own workdir, once


# --- Hermes on #2418: a secret cut by an upstream cap, a withdrawn message, slow git -----------

SPLIT = "hunter2-token/0123456789&key=abcdefghijklmnop"
ENCODED = urllib.parse.quote(SPLIT, safe="")


@pytest.mark.parametrize("form", ["raw", "url-encoded"])
def test_a_secret_cut_by_git_logs_subject_cap_never_reaches_the_brief(monkeypatch, form):
    """Hermes on PR #2419: `git_log` cuts a subject at 200 characters, possibly inside a secret in
    any encoding the shared redactor knows. The production `recent_commits` output is checked."""
    from agent_sessions import gitpanel

    value = SPLIT if form == "raw" else ENCODED
    pad = "x" * (gitpanel.LOG_TEXT_MAX - 20)
    subject = (pad + " " + value)[: gitpanel.LOG_TEXT_MAX]  # git_log's own cut, mid-secret
    assert value not in subject
    commits = {"commits": [{"short": "abc1234", "time": None, "subject": subject}]}
    monkeypatch.setattr(gitpanel, "git_log", lambda path, limit: commits)
    monkeypatch.setattr(handoff.template_secrets, "redaction_values", lambda: [SPLIT])
    (line,) = handoff.recent_commits("/work/repo")
    assert line == f"- abc1234 {handoff.SUBJECT_OMITTED}"
    assert "xxxx" not in line and value[:4] not in line


def test_a_short_subject_with_an_encoded_secret_is_redacted_before_the_cut(monkeypatch):
    from agent_sessions import gitpanel

    commits = {"commits": [{"short": "abc1234", "time": None, "subject": f"use {ENCODED}"}]}
    monkeypatch.setattr(gitpanel, "git_log", lambda path, limit: commits)
    monkeypatch.setattr(handoff.template_secrets, "redaction_values", lambda: [SPLIT])
    (line,) = handoff.recent_commits("/work/repo")
    assert line == "- abc1234 use [secret]"


def test_long_subjects_are_kept_when_no_secret_is_configured(monkeypatch):
    from agent_sessions import gitpanel

    subject = "z" * gitpanel.LOG_TEXT_MAX
    commits = {"commits": [{"short": "abc1234", "time": None, "subject": subject}]}
    monkeypatch.setattr(gitpanel, "git_log", lambda path, limit: commits)
    monkeypatch.setattr(handoff.template_secrets, "redaction_values", lambda: [])
    (line,) = handoff.recent_commits("/work/repo")
    assert line == "- abc1234 " + "z" * handoff.COMMIT_SUBJECT_MAX


@pytest.mark.parametrize("form", ["raw", "url-encoded"])
def test_a_first_line_title_cut_mid_secret_is_rebuilt_from_the_full_message(monkeypatch, form):
    value = SPLIT if form == "raw" else ENCODED
    full = "y" * (metadata.TITLE_FALLBACK_MAX - 8) + value
    first = metadata.title_candidate(full)  # the scan's cut, mid-secret
    assert value not in first
    monkeypatch.setattr(handoff.template_secrets, "redaction_values", lambda: [SPLIT])
    turns = [transcript.Turn("user", full)]
    monkeypatch.setattr(transcript, "adapter_for", lambda _e: lambda *_a: turns)
    seed, _ = handoff.build_quick_seed("claude", "s1", first_message=first)
    task = next(line for line in seed.splitlines() if line.startswith("- task: "))
    assert task.endswith("[secret]") and value[:4] not in task


def test_a_cut_title_whose_message_is_not_in_the_transcript_is_dropped(monkeypatch):
    full = "y" * (metadata.TITLE_FALLBACK_MAX - 8) + ENCODED
    first = metadata.title_candidate(full)
    monkeypatch.setattr(handoff.template_secrets, "redaction_values", lambda: [SPLIT])
    turns = [transcript.Turn("user", "a later message"), transcript.Turn("assistant", "ok")]
    monkeypatch.setattr(transcript, "adapter_for", lambda _e: lambda *_a: turns)
    seed, _ = handoff.build_quick_seed("claude", "s1", first_message=first)
    assert "- task: " not in seed and ENCODED[:4] not in seed

    async def fake(messages, **_kw):
        return {"state": "done"}

    monkeypatch.setattr(review, "complete_json", fake)
    seed, _ = asyncio.run(handoff.build_ai_seed("claude", "s1", first_message=first))
    assert "- task: " not in seed and ENCODED[:4] not in seed


def test_a_withdrawn_message_stays_out_beside_own_turns(conversation, monkeypatch):
    root, sid, binding = conversation
    op = _submit(root, sid, binding, monkeypatch, T0, "Kept")
    _reply(root, sid, binding, monkeypatch, T0 + 1, op, "Done")
    agent(root, sid, binding, monkeypatch, T0 + 60, "Own work")
    claim = request(binding, revision=native_journal.read(root, sid).revision, text="Removed")
    gone = claim["params"]["operation_id"]
    native_journal.claim(root, claim, queued=True)
    control = request(
        binding,
        action="withdraw",
        revision=native_journal.read(root, sid).revision,
        queued_turn_id=gone,
    )
    native_journal.claim(root, control)
    native_journal.record_handoff(root, binding, gone, "not_sent")
    native_journal.record_handoff(root, binding, control["params"]["operation_id"], "sent")
    texts = [t.text for t in transcript._native_turns(root, sid) if t.kind == "text"]
    assert texts == ["Kept", "Done", "Own work"]


def test_a_slow_git_read_never_stalls_the_event_loop(monkeypatch):
    """The commit read runs in a worker thread (as the locator does, #718): a slow repository
    delays this one prepare, never a concurrent heartbeat."""
    import time

    from agent_sessions import gitpanel

    def slow(path, limit):
        time.sleep(0.4)
        return _COMMITS

    monkeypatch.setattr(gitpanel, "git_log", slow)
    turns = [transcript.Turn("user", "go"), transcript.Turn("assistant", "done")]
    monkeypatch.setattr(transcript, "adapter_for", lambda _e: lambda *_a: turns)

    async def fake(messages, **_kw):
        return {"state": "done"}

    monkeypatch.setattr(review, "complete_json", fake)

    async def scenario():
        worst = 0.0
        stop = False

        async def heartbeat():
            nonlocal worst
            while not stop:
                t = time.perf_counter()
                await asyncio.sleep(0.01)
                worst = max(worst, time.perf_counter() - t)

        beat = asyncio.create_task(heartbeat())
        seed, _ = await handoff.build_ai_seed("claude", "s1", commits_cwd="/work/repo")
        stop = True
        await beat
        return seed, worst

    seed, worst = asyncio.run(scenario())
    assert "## Recent commits" in seed
    assert worst < 0.2, f"event loop stalled {worst:.3f}s behind a 0.4s git read"
