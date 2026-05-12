"""Agent provider package.

Symphony depends only on :class:`~symphony.provider.base.AgentProviderProtocol`.
The fake (#7) lives next to the real Claude Code provider (#9) so test
imports stay stable across both surfaces.
"""

from symphony.provider.base import (
    AgentProviderProtocol,
    ProviderError,
    ProviderRestoreError,
    ProviderRetryableError,
    SessionRecord,
    Terminal,
)
from symphony.provider.claude_code import ClaudeCodeProvider
from symphony.provider.codex import CodexProvider
from symphony.provider.fake import FakeProvider, FakeTurnScript

__all__ = [
    "AgentProviderProtocol",
    "ClaudeCodeProvider",
    "CodexProvider",
    "FakeProvider",
    "FakeTurnScript",
    "ProviderError",
    "ProviderRestoreError",
    "ProviderRetryableError",
    "SessionRecord",
    "Terminal",
]
