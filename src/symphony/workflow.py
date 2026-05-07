"""Workflow loader: parse ``WORKFLOW.md`` and render prompts.

A workflow file is a Markdown document whose head is YAML front matter
delimited by ``---`` lines, followed by a Jinja2 prompt template. The loader:

1. Splits the file into front-matter YAML and prompt body.
2. Parses the YAML with :func:`yaml.safe_load`.
3. Hands the mapping to :func:`symphony.config.build_config`, which produces
   a typed :class:`~symphony.config.WorkflowConfig`.
4. Compiles the prompt body with :class:`jinja2.StrictUndefined` so that
   referencing an unknown variable or filter fails closed.

All errors surface as :class:`WorkflowError` (or :class:`~symphony.config.ConfigError`
re-raised for config-shape problems) carrying a path-style location so the CLI
can show operator-friendly messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError, meta

from symphony.config import ConfigError, WorkflowConfig, build_config
from symphony.models import Issue

_FRONT_MATTER_DELIM = "---"


# -- Errors --------------------------------------------------------------------


class WorkflowError(ValueError):
    """Raised when ``WORKFLOW.md`` cannot be loaded or rendered.

    ``location`` mirrors :class:`~symphony.config.ConfigError` so callers can
    handle both with the same surface.
    """

    def __init__(self, location: str, message: str) -> None:
        super().__init__(f"{location}: {message}")
        self.location = location
        self.message = message


# -- Loaded shape --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowFile:
    """A loaded workflow file: typed config plus the prompt template."""

    path: Path
    config: WorkflowConfig
    prompt_template: str
    referenced_variables: frozenset[str] = field(default_factory=frozenset)


# -- Parsing -------------------------------------------------------------------


def _split_front_matter(text: str, *, location: str) -> tuple[str, str]:
    """Split a workflow file into (yaml_text, prompt_body).

    Accepts a leading BOM and a leading newline before the opening ``---``
    delimiter. The opening delimiter MUST be the first non-blank line; we
    intentionally do not search for ``---`` mid-document, so a workflow that
    forgets the front matter fails clearly instead of silently treating the
    whole body as YAML.
    """
    if text.startswith("﻿"):
        text = text[1:]
    stripped = text.lstrip("\n\r ")
    if not stripped.startswith(_FRONT_MATTER_DELIM):
        raise WorkflowError(
            location,
            "workflow file must begin with a '---' YAML front-matter delimiter",
        )
    body = stripped
    # Drop the opening ``---`` line.
    nl = body.find("\n")
    if nl == -1:
        raise WorkflowError(location, "workflow file ends inside front matter")
    body_after_open = body[nl + 1 :]

    # Find the closing ``---`` on its own line.
    end_marker = "\n" + _FRONT_MATTER_DELIM
    idx = body_after_open.find(end_marker)
    if idx == -1:
        # The closing delimiter might be the very first line of body_after_open
        # (empty front matter).
        if body_after_open.startswith(_FRONT_MATTER_DELIM):
            yaml_text = ""
            tail = body_after_open[len(_FRONT_MATTER_DELIM) :]
        else:
            raise WorkflowError(
                location,
                "missing closing '---' for YAML front matter",
            )
    else:
        yaml_text = body_after_open[:idx]
        tail = body_after_open[idx + len(end_marker) :]

    # Strip a single newline immediately after the closing delimiter so the
    # rendered prompt does not start with a blank line.
    if tail.startswith("\r\n"):
        tail = tail[2:]
    elif tail.startswith("\n"):
        tail = tail[1:]
    return yaml_text, tail


def _parse_yaml(yaml_text: str, *, location: str) -> dict[str, Any]:
    if not yaml_text.strip():
        raise WorkflowError(location, "front matter is empty")
    try:
        loaded = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise WorkflowError(location, f"invalid YAML in front matter: {exc}") from exc
    if loaded is None:
        raise WorkflowError(location, "front matter is empty")
    if not isinstance(loaded, dict):
        raise WorkflowError(
            location,
            f"front matter must be a mapping, got {type(loaded).__name__}",
        )
    return loaded


def _jinja_env() -> Environment:
    """Build the Jinja2 environment used for prompt rendering.

    StrictUndefined makes unknown variables fail closed (acceptance criterion
    for #5). ``autoescape=False`` because prompts are plain text, not HTML.
    """
    return Environment(
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def _collect_referenced_variables(template_source: str) -> frozenset[str]:
    """Return the set of top-level identifiers referenced in the template.

    Used to fail closed on unknown filters at parse time as well, and to
    let tests assert the workflow's intent without rendering. Jinja2 catches
    unknown filters during ``meta.find_undeclared_variables`` (it walks the
    AST through TrackingCodeGenerator), so both ``parse()`` and the meta
    walk live inside the same try block.
    """
    env = _jinja_env()
    try:
        ast = env.parse(template_source)
        return frozenset(meta.find_undeclared_variables(ast))
    except TemplateError as exc:
        raise WorkflowError("prompt", f"invalid prompt template: {exc}") from exc


# -- Public entry points -------------------------------------------------------


def load_workflow(
    path: Path | str,
    *,
    env: dict[str, str] | None = None,
) -> WorkflowFile:
    """Load and validate a workflow file.

    On success returns a :class:`WorkflowFile` whose ``config`` is a typed,
    fully-defaulted :class:`WorkflowConfig` and whose ``prompt_template`` is
    the un-rendered Jinja2 template body. The template is *not* compiled at
    load time — :func:`render_prompt` re-compiles it for each render so that
    a syntax error surfaces at render time too (catches Jinja2 caching
    surprises).

    ``env`` is forwarded to :func:`symphony.config.build_config` for
    test-time isolation.
    """
    workflow_path = Path(path)
    if not workflow_path.is_file():
        raise WorkflowError(str(workflow_path), "workflow file does not exist")
    location = str(workflow_path)
    text = workflow_path.read_text(encoding="utf-8")
    yaml_text, prompt_body = _split_front_matter(text, location=location)
    raw = _parse_yaml(yaml_text, location=f"{location}#front-matter")
    try:
        config = build_config(raw, workflow_path=workflow_path, env=env)
    except ConfigError as exc:
        # Re-raise unchanged — ConfigError already carries a useful location.
        raise exc

    referenced = _collect_referenced_variables(prompt_body)
    return WorkflowFile(
        path=workflow_path.resolve(),
        config=config,
        prompt_template=prompt_body,
        referenced_variables=referenced,
    )


def render_prompt(
    workflow: WorkflowFile,
    *,
    issue: Issue,
    extra: dict[str, Any] | None = None,
) -> str:
    """Render the workflow's prompt template against an :class:`Issue`.

    Variables exposed to the template:

    - ``issue`` — the full :class:`Issue` dataclass (attribute access works).
    - ``workspace_path`` and any other key supplied via ``extra``.

    ``extra`` MUST NOT contain the key ``"issue"`` — that key is reserved for
    the issue argument and silent shadowing would be a footgun. Reserved-key
    collisions raise :class:`WorkflowError` at location ``"prompt.extra"``.

    Unknown variable references and unknown filters fail closed by raising
    :class:`WorkflowError` with location ``"prompt"``. This is enforced by
    :class:`jinja2.StrictUndefined` and by re-raising :class:`TemplateError`.
    """
    context: dict[str, Any] = {"issue": issue}
    if extra:
        reserved = {"issue"} & extra.keys()
        if reserved:
            raise WorkflowError(
                "prompt.extra",
                f"reserved key(s) collide with positional arguments: {sorted(reserved)}",
            )
        context.update(extra)

    env = _jinja_env()
    try:
        template = env.from_string(workflow.prompt_template)
        return template.render(**context)
    except TemplateError as exc:
        raise WorkflowError("prompt", str(exc)) from exc
