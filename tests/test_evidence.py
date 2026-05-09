"""Tests for the M5.2 evidence detector (issue #60).

Drives :class:`EvidenceDetector` against fake GitHub responses and a
real local git fixture so the workspace probes (``ls-remote``,
``status --porcelain``, ``diff --numstat``) execute the same code
path production would. The PR-lookup side uses
:class:`httpx.MockTransport` against a real :class:`GitHubClient` —
no live HTTP, but the request shape is what GitHub would see.

Test surface (one test per SPEC §17.2 outcome path + derivation
table coverage):

- COMPLETED + PR found → ``completed_with_pr`` with `pr_linked`
  evidence whose number/url match the mocked PR.
- COMPLETED + no PR + sentinel in last event → ``completed_no_pr_declared``
  with populated ``no_pr_reason`` and a ``no_pr_declared`` entry.
- COMPLETED + no PR + permission_denials_count > 0 → ``incomplete_permission_denied``
  + ``permission_denied`` entry whose tool_names come from the SDK list.
- COMPLETED + no PR / no decl / no denials → ``incomplete_no_evidence``.
- FAILED + blocked → ``blocked_operator_required`` (derivation).
- FAILED + retryable → ``retryable_failure`` (derivation).
- COMPLETED + workspace has uncommitted edits but no PR/decl/denials →
  ``incomplete_no_evidence`` with informational `diff_in_workspace`.
- Branch detection: ls-remote returns the expected branch SHA →
  `branch_pushed` entry with the correct name and head_sha.
- PR lookup error tolerated: GitHubError logged, no crash, evidence
  collection continues.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from symphony.config import GitHubConfig
from symphony.events import AgentEvent
from symphony.evidence import (
    DECIDED_BY_DERIVATION,
    DECIDED_BY_DETECTOR,
    NO_PR_SENTINEL,
    OUTCOME_BLOCKED_OPERATOR_REQUIRED,
    OUTCOME_COMPLETED_NO_PR_DECLARED,
    OUTCOME_COMPLETED_WITH_PR,
    OUTCOME_INCOMPLETE_NO_EVIDENCE,
    OUTCOME_INCOMPLETE_PERMISSION_DENIED,
    OUTCOME_RETRYABLE_FAILURE,
    OUTCOME_UNKNOWN,
    DetectorResult,
    EvidenceDetector,
    collect_recent_assistant_text,
)
from symphony.github.client import GitHubClient
from symphony.models import Issue
from symphony.provider.base import Terminal

# -- Fixtures ----------------------------------------------------------------


def _issue(*, owner: str = "acme", repo: str = "proj", number: int = 1) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"{owner}/{repo}#{number}",
        owner=owner,
        repo=repo,
        title="t",
        body="b",
        state="open",
        url=f"https://github.com/{owner}/{repo}/issues/{number}",
    )


def _github(base_branch: str = "main", branch_prefix: str = "symphony") -> GitHubConfig:
    return GitHubConfig(branch_prefix=branch_prefix, base_branch=base_branch)


def _fake_pr_payload(*, number: int = 99, url: str | None = None, head_ref: str) -> dict[str, Any]:
    return {
        "node_id": f"PR_kgD{number}",
        "id": number,
        "number": number,
        "title": "wip: fix from symphony",
        "html_url": url or f"https://github.com/acme/proj/pull/{number}",
        "state": "open",
        "head": {"ref": head_ref, "repo": {"owner": {"login": "acme"}, "name": "proj"}},
        "base": {"ref": "main", "repo": {"default_branch": "main"}},
        "draft": True,
        "mergeable_state": "clean",
        "body": "Closes acme/proj#1",
    }


def _client_returning(prs: list[dict[str, Any]]) -> GitHubClient:
    """Build a GitHubClient backed by an MockTransport that returns ``prs``
    for the list-pulls endpoint and 404 for everything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=prs)
        return httpx.Response(404, json={"message": "not found"})

    return GitHubClient("ghp_test_xxxx", transport=httpx.MockTransport(handler))


def _client_pr_lookup_fails() -> GitHubClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    return GitHubClient("ghp_test_xxxx", transport=httpx.MockTransport(handler))


def _client_head_empty_fallback_returning(prs: list[dict[str, Any]]) -> GitHubClient:
    """Return no deterministic head match, then return ``prs`` for fallback scan."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls"):
            if request.url.params.get("head"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=prs)
        return httpx.Response(404, json={"message": "not found"})

    return GitHubClient("ghp_test_xxxx", transport=httpx.MockTransport(handler))


def _last_event(payload: dict[str, Any]) -> AgentEvent:
    from datetime import datetime, timezone

    return AgentEvent(
        event="turn_completed",
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        provider="fake",
        issue_identifier="acme/proj#1",
        attempt=1,
        payload=payload,
    )


# -- COMPLETED + PR found ----------------------------------------------------


def test_completed_with_pr(tmp_path: Path) -> None:
    issue = _issue()
    github = _github()
    pr = _fake_pr_payload(number=42, head_ref="symphony/acme-proj-1")
    detector = EvidenceDetector(github, client=_client_returning([pr]))

    result = detector.detect(
        issue=issue,
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=0,
        last_event=_last_event({"result": "all done"}),
        recent_assistant_text="",
        workspace_path=tmp_path,  # no .git → branch/diff probes skip
    )
    assert result.task_outcome == OUTCOME_COMPLETED_WITH_PR
    assert result.outcome_decided_by == DECIDED_BY_DETECTOR
    pr_entries = [e for e in result.task_evidence if e["type"] == "pr_linked"]
    assert len(pr_entries) == 1
    assert pr_entries[0]["number"] == 42
    assert pr_entries[0]["url"].endswith("/pull/42")
    assert pr_entries[0]["state"] == "open"
    assert pr_entries[0]["head_ref"] == "symphony/acme-proj-1"
    # Conservative default: detector cannot tell created vs. updated
    # from a single read; M5.3 may tighten by snapshot diffing.
    assert pr_entries[0]["created"] is False


# -- COMPLETED + no-PR sentinel ---------------------------------------------


def test_completed_no_pr_declared_via_sentinel_in_last_event(tmp_path: Path) -> None:
    issue = _issue()
    github = _github()
    detector = EvidenceDetector(github, client=_client_returning([]))

    event = _last_event(
        {"result": "Symphony-No-PR: typo already fixed in #41 by another contributor."}
    )
    result = detector.detect(
        issue=issue,
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=0,
        last_event=event,
        recent_assistant_text="",
        workspace_path=tmp_path,
    )
    assert result.task_outcome == OUTCOME_COMPLETED_NO_PR_DECLARED
    assert result.no_pr_reason == "typo already fixed in #41 by another contributor."
    decl_entries = [e for e in result.task_evidence if e["type"] == "no_pr_declared"]
    assert len(decl_entries) == 1
    assert decl_entries[0]["marker_source"] == "assistant_message"


def test_sentinel_in_recent_assistant_text_is_picked_up(tmp_path: Path) -> None:
    """The orchestrator may pass a transcript tail; sentinel detected there."""
    detector = EvidenceDetector(_github(), client=_client_returning([]))
    text = "I considered the request but Symphony-No-PR: nothing to change\n"
    result = detector.detect(
        issue=_issue(),
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=0,
        last_event=_last_event({}),
        recent_assistant_text=text,
        workspace_path=tmp_path,
    )
    assert result.task_outcome == OUTCOME_COMPLETED_NO_PR_DECLARED
    assert result.no_pr_reason == "nothing to change"


def test_sentinel_regex_is_case_insensitive_and_strips_reason() -> None:
    m = NO_PR_SENTINEL.search("symphony-no-pr:   already merged elsewhere   \n")
    assert m is not None
    assert m.group("reason").strip() == "already merged elsewhere"


# -- COMPLETED + permission denied -----------------------------------------


def test_completed_permission_denied_promotes_to_incomplete(tmp_path: Path) -> None:
    detector = EvidenceDetector(_github(), client=_client_returning([]))
    event = _last_event(
        {
            "result": "I cannot run shell commands.",
            "permission_denials": [
                {"tool_name": "Bash", "tool_use_id": "tu_1"},
                {"tool_name": "AskUserQuestion", "tool_use_id": "tu_2"},
            ],
        }
    )
    result = detector.detect(
        issue=_issue(),
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=2,
        last_event=event,
        recent_assistant_text="",
        workspace_path=tmp_path,
    )
    assert result.task_outcome == OUTCOME_INCOMPLETE_PERMISSION_DENIED
    perm = [e for e in result.task_evidence if e["type"] == "permission_denied"]
    assert len(perm) == 1
    assert perm[0]["denials_count"] == 2
    assert sorted(perm[0]["tool_names"]) == ["AskUserQuestion", "Bash"]


def test_unrelated_fallback_pr_does_not_mask_permission_denial(tmp_path: Path) -> None:
    pr = _fake_pr_payload(number=264, head_ref="unrelated-branch")
    pr["body"] = "Summary only."
    detector = EvidenceDetector(
        _github(),
        client=_client_head_empty_fallback_returning([pr]),
    )
    result = detector.detect(
        issue=_issue(number=405),
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=1,
        last_event=_last_event(
            {
                "result": "I need clarification.",
                "permission_denials": [
                    {"tool_name": "AskUserQuestion", "tool_use_id": "tu_1"},
                ],
            }
        ),
        recent_assistant_text="",
        workspace_path=tmp_path,
    )

    assert result.task_outcome == OUTCOME_INCOMPLETE_PERMISSION_DENIED
    assert [e for e in result.task_evidence if e["type"] == "pr_linked"] == []


# -- COMPLETED + nothing → incomplete_no_evidence ---------------------------


def test_completed_with_no_evidence_falls_to_incomplete(tmp_path: Path) -> None:
    detector = EvidenceDetector(_github(), client=_client_returning([]))
    result = detector.detect(
        issue=_issue(),
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=0,
        last_event=_last_event({"result": "I would do X but stopped."}),
        recent_assistant_text="",
        workspace_path=tmp_path,
    )
    assert result.task_outcome == OUTCOME_INCOMPLETE_NO_EVIDENCE
    assert result.outcome_decided_by == DECIDED_BY_DETECTOR
    assert result.task_evidence == []


# -- Derivation paths (non-COMPLETED) ---------------------------------------


def test_failed_blocked_derives_to_blocked_operator_required() -> None:
    detector = EvidenceDetector(_github(), client=_client_returning([]))
    result = detector.detect(
        issue=_issue(),
        terminal_state=Terminal.FAILED,
        retryable=False,
        blocked=True,
        permission_denials_count=0,
        last_event=None,
        recent_assistant_text="",
        workspace_path=None,
    )
    assert result.task_outcome == OUTCOME_BLOCKED_OPERATOR_REQUIRED
    assert result.outcome_decided_by == DECIDED_BY_DERIVATION


def test_cancelled_retryable_derives_to_retryable_failure() -> None:
    detector = EvidenceDetector(_github(), client=_client_returning([]))
    result = detector.detect(
        issue=_issue(),
        terminal_state=Terminal.CANCELLED,
        retryable=True,
        blocked=False,
        permission_denials_count=0,
        last_event=None,
        recent_assistant_text="",
        workspace_path=None,
    )
    assert result.task_outcome == OUTCOME_RETRYABLE_FAILURE
    assert result.outcome_decided_by == DECIDED_BY_DERIVATION


def test_unknown_terminal_state_derives_to_unknown() -> None:
    detector = EvidenceDetector(_github(), client=_client_returning([]))
    result = detector.detect(
        issue=_issue(),
        terminal_state=None,
        retryable=False,
        blocked=False,
        permission_denials_count=0,
        last_event=None,
        recent_assistant_text="",
        workspace_path=None,
    )
    assert result.task_outcome == OUTCOME_UNKNOWN


# -- Workspace probes (real git) --------------------------------------------


def _git(cwd: Path, *argv: str) -> None:
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }
    subprocess.run(
        ["git", *argv], cwd=str(cwd), env=env, check=True, capture_output=True, text=True
    )


def _seed_workspace_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Build a workspace cloned from a local bare repo. Returns (workspace, bare)."""
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True,
        capture_output=True,
    )
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("hello\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "branch", "-M", "main")
    _git(seed, "push", "origin", "main")
    workspace = tmp_path / "ws"
    subprocess.run(
        ["git", "clone", str(bare), str(workspace)], check=True, capture_output=True
    )
    return workspace, bare


def test_branch_pushed_evidence_when_remote_has_expected_branch(tmp_path: Path) -> None:
    workspace, bare = _seed_workspace_with_remote(tmp_path)
    # Push the expected branch via a separate working clone.
    work = tmp_path / "agent_work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    _git(work, "checkout", "-b", "symphony/acme-proj-1")
    (work / "fix.txt").write_text("fix\n")
    _git(work, "add", "fix.txt")
    _git(work, "commit", "-m", "fix")
    _git(work, "push", "origin", "symphony/acme-proj-1")

    detector = EvidenceDetector(_github(), client=_client_returning([]))
    result = detector.detect(
        issue=_issue(),
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=0,
        last_event=_last_event({}),
        recent_assistant_text="",
        workspace_path=workspace,
    )
    branch_entries = [e for e in result.task_evidence if e["type"] == "branch_pushed"]
    assert len(branch_entries) == 1
    assert branch_entries[0]["name"] == "symphony/acme-proj-1"
    # head_sha is a hex string of length 40.
    assert len(branch_entries[0]["head_sha"]) == 40
    # branch alone is NOT sufficient — outcome stays incomplete_no_evidence.
    assert result.task_outcome == OUTCOME_INCOMPLETE_NO_EVIDENCE


def test_diff_in_workspace_recorded_but_does_not_promote_outcome(tmp_path: Path) -> None:
    workspace, _bare = _seed_workspace_with_remote(tmp_path)
    # Local uncommitted edit + untracked file.
    (workspace / "README.md").write_text("locally modified\n")
    (workspace / "junk.tmp").write_text("untracked\n")

    detector = EvidenceDetector(_github(), client=_client_returning([]))
    result = detector.detect(
        issue=_issue(),
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=0,
        last_event=_last_event({}),
        recent_assistant_text="",
        workspace_path=workspace,
    )
    diff_entries = [e for e in result.task_evidence if e["type"] == "diff_in_workspace"]
    assert len(diff_entries) == 1
    assert diff_entries[0]["files_changed"] >= 2  # README + junk.tmp at minimum
    # Local-only edits don't reach GitHub → still incomplete_no_evidence.
    assert result.task_outcome == OUTCOME_INCOMPLETE_NO_EVIDENCE


def test_branch_and_diff_probes_skip_when_no_dot_git(tmp_path: Path) -> None:
    """A workspace path without `.git` should not crash the probes."""
    detector = EvidenceDetector(_github(), client=_client_returning([]))
    workspace = tmp_path / "empty_ws"
    workspace.mkdir()
    result = detector.detect(
        issue=_issue(),
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=0,
        last_event=_last_event({}),
        recent_assistant_text="",
        workspace_path=workspace,
    )
    assert result.task_outcome == OUTCOME_INCOMPLETE_NO_EVIDENCE
    assert all(e["type"] != "branch_pushed" for e in result.task_evidence)
    assert all(e["type"] != "diff_in_workspace" for e in result.task_evidence)


# -- PR lookup failure tolerated --------------------------------------------


def test_pr_lookup_failure_returns_unknown_not_incomplete(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Transient GitHub failure during PR detection MUST NOT classify the
    run as `incomplete_no_evidence` — that would falsely block the issue
    on a 500/rate-limit/network blip.

    Required by leader correction on #62 / PR #73 (the detector previously
    returned `[]` on GitHubError, conflating API failure with verified
    absence). Now returns `None` so the routing falls through to
    `unknown` and the issue is NOT marked blocked.
    """
    detector = EvidenceDetector(_github(), client=_client_pr_lookup_fails())
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="symphony.evidence"):
        result = detector.detect(
            issue=_issue(),
            terminal_state=Terminal.COMPLETED,
            retryable=False,
            blocked=False,
            permission_denials_count=0,
            last_event=_last_event({}),
            recent_assistant_text="",
            workspace_path=tmp_path,
        )
    # CRITICAL: outcome MUST be `unknown`, not `incomplete_no_evidence`,
    # so the orchestrator does NOT escalate to mark_issue_blocked.
    assert result.task_outcome == OUTCOME_UNKNOWN
    assert result.outcome_decided_by == DECIDED_BY_DERIVATION
    assert all(e["type"] != "pr_linked" for e in result.task_evidence)
    # WARNING log mentions the lookup failure + the routing implication.
    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("PR lookup failed" in r.getMessage() for r in warnings)
    assert any("`unknown`" in r.getMessage() for r in warnings)


# -- to_terminal_fields shape ------------------------------------------------


def test_detector_result_to_terminal_fields_shape() -> None:
    result = DetectorResult(
        task_outcome=OUTCOME_COMPLETED_WITH_PR,
        task_evidence=[
            {
                "type": "pr_linked",
                "url": "x",
                "number": 1,
                "state": "open",
                "created": False,
            }
        ],
        no_pr_reason=None,
        outcome_decided_by=DECIDED_BY_DETECTOR,
    )
    fields = result.to_terminal_fields()
    assert set(fields.keys()) == {
        "task_outcome",
        "task_evidence",
        "no_pr_reason",
        "outcome_decided_by",
        "task_outcome_recorded_at",
    }
    assert fields["task_outcome"] == OUTCOME_COMPLETED_WITH_PR
    # Timestamp is ISO 8601 string.
    assert isinstance(fields["task_outcome_recorded_at"], str)
    assert "T" in fields["task_outcome_recorded_at"]


def test_detector_with_no_client_returns_unknown_not_incomplete(tmp_path: Path) -> None:
    """When no GitHubClient is wired the detector cannot verify PR
    absence, so a COMPLETED run with no other evidence MUST classify as
    `unknown` (decided_by=derivation) rather than `incomplete_no_evidence`.

    #62 routing treats `incomplete_*` as operator-must-intervene; an
    unverifiable run must NOT be escalated. Tests using FakeGitHubTracker
    (no `.client`) take this path."""
    detector = EvidenceDetector(_github(), client=None)
    result = detector.detect(
        issue=_issue(),
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=0,
        last_event=_last_event({}),
        recent_assistant_text="",
        workspace_path=tmp_path,
    )
    assert result.task_outcome == OUTCOME_UNKNOWN
    assert result.outcome_decided_by == DECIDED_BY_DERIVATION


# -- collect_recent_assistant_text helper -----------------------------------


def test_collect_recent_assistant_text_concatenates_message_deltas() -> None:
    from datetime import datetime, timezone

    def _ev(name: str, payload: dict[str, Any]) -> AgentEvent:
        return AgentEvent(
            event=name,
            timestamp=datetime.now(timezone.utc),
            session_id="s",
            provider="fake",
            issue_identifier="x",
            attempt=1,
            payload=payload,
        )

    events = [
        _ev("message_delta", {"text": "first"}),
        _ev("tool_started", {"tool_name": "Read"}),
        _ev("message_delta", {"text": "second"}),
        _ev("turn_completed", {}),
    ]
    text = collect_recent_assistant_text(events)
    # Message_delta entries come through; non-text events skipped.
    assert "first" in text
    assert "second" in text
    assert "tool_name" not in text  # tool_started has no .text


def test_collect_recent_assistant_text_returns_newest_in_chronological_order() -> None:
    """The helper MUST return the most recent ``limit`` matches in
    chronological order (oldest-of-the-tail first), not the first N
    matches.

    Bug history (#63): the M5.2 #60 implementation walked from the
    start and stopped at ``limit``, returning the OLDEST chunks. A
    sentinel in Claude's final message would slip past undetected on
    any session longer than 16 deltas. Locked in here.
    """
    from datetime import datetime, timezone

    events = [
        AgentEvent(
            event="message_delta",
            timestamp=datetime.now(timezone.utc),
            session_id="s",
            provider="fake",
            issue_identifier="x",
            attempt=1,
            payload={"text": f"chunk{i}"},
        )
        for i in range(20)
    ]
    text = collect_recent_assistant_text(events, limit=3)
    # Newest 3 chunks are 17, 18, 19 — in chronological order.
    assert text == "chunk17\nchunk18\nchunk19"
    # And explicitly NOT the oldest three (use line equality to avoid
    # the chunk1-as-substring-of-chunk17 false positive).
    lines = text.splitlines()
    assert "chunk0" not in lines
    assert "chunk1" not in lines
    assert "chunk2" not in lines


def test_collect_recent_assistant_text_picks_up_sentinel_in_final_message() -> None:
    """End-to-end regression for the bug carried from #72: a
    `Symphony-No-PR:` sentinel in Claude's FINAL assistant message
    must be returned by the helper even when the session has many
    earlier message_delta chunks. Without the fix the helper would
    return the first 16 chunks and miss the sentinel sitting in
    chunk 17+.
    """
    from datetime import datetime, timezone

    # 20 noise chunks then the sentinel — sentinel sits past the
    # default limit of 16 deltas.
    payloads = [f"thinking step {i}" for i in range(20)]
    payloads.append("Symphony-No-PR: nothing to change here")
    events = [
        AgentEvent(
            event="message_delta",
            timestamp=datetime.now(timezone.utc),
            session_id="s",
            provider="fake",
            issue_identifier="x",
            attempt=1,
            payload={"text": text},
        )
        for text in payloads
    ]
    text = collect_recent_assistant_text(events)
    assert "Symphony-No-PR" in text
    # And the sentinel regex picks it up cleanly.
    match = NO_PR_SENTINEL.search(text)
    assert match is not None
    assert match.group("reason").strip() == "nothing to change here"


# -- Orchestrator wiring smoke (terminal.json carries new fields) -----------


async def test_orchestrator_writes_new_terminal_fields(tmp_path: Path) -> None:
    """Smoke test: a clean orchestrator run produces a terminal.json with the
    M5.1 task-outcome row populated by the detector. Doesn't assert specific
    `task_outcome` (the FakeProvider's last event has no permission_denials
    and no FakeGitHubTracker.client, so the detector falls through to
    incomplete_no_evidence — what we care about here is field presence)."""
    import json

    from symphony.config import (
        AgentConfig,
        ClaudeConfig,
        LoggingConfig,
        PollingConfig,
        RetryConfig,
        TrackerConfig,
        WorkflowConfig,
        WorkspaceConfig,
    )
    from symphony.github.tracker import FakeGitHubTracker
    from symphony.orchestrator import Orchestrator
    from symphony.provider.fake import FakeProvider
    from symphony.workspace import WorkspaceManager

    cfg = WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner="acme",
            repo="proj",
            token="literal-token",
            include_labels=("symphony-ready",),
        ),
        agent=AgentConfig(max_concurrency=1, max_turns=1),
        workspace=WorkspaceConfig(root=tmp_path / "ws"),
        claude=ClaudeConfig(
            model="fake-model",
            permission_mode="acceptEdits",
            session_store=tmp_path / "sessions",
            transcript_store=tmp_path / "transcripts",
            artifact_store=tmp_path / "artifacts",
        ),
        github=GitHubConfig(),
        polling=PollingConfig(),
        retry=RetryConfig(),
        logging=LoggingConfig(),
        workflow_path=tmp_path / "WORKFLOW.md",
    )
    tracker = FakeGitHubTracker(
        issues=[
            Issue(
                id="I_1",
                number=1,
                identifier="acme/proj#1",
                owner="acme",
                repo="proj",
                title="t",
                body="b",
                state="open",
                url="https://github.com/acme/proj/issues/1",
                labels=("symphony-ready",),
            )
        ]
    )
    mgr = WorkspaceManager(cfg.workspace)
    orch = Orchestrator(cfg, tracker=tracker, provider=FakeProvider(), workspace_manager=mgr)
    await orch.run_once()

    terminal_files = list(Path(tmp_path / "artifacts").rglob("terminal.json"))
    assert terminal_files, "expected terminal.json under artifacts/"
    record = json.loads(terminal_files[0].read_text())
    # New SPEC §17.1 fields all present.
    assert "task_outcome" in record
    assert "task_evidence" in record and isinstance(record["task_evidence"], list)
    assert "no_pr_reason" in record
    assert "outcome_decided_by" in record
    assert "task_outcome_recorded_at" in record
    # FakeGitHubTracker has no .client → detector returns incomplete_no_evidence
    # for the COMPLETED case (no PR query possible).
    assert record["task_outcome"] in {OUTCOME_INCOMPLETE_NO_EVIDENCE, OUTCOME_UNKNOWN}
    # Existing fields still present (backward compat per SPEC §17.6).
    assert "terminal_state" in record
    assert "permission_denials_count" in record
