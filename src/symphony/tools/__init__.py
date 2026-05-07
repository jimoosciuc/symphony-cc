"""Symphony's optional client-side tools (SPEC §18).

Each tool is gated by an ``agent.tools.<name>.enabled`` workflow knob
and lives under this package as a pure-Python handler. The Claude
provider stitches enabled tools into the SDK's ``ClaudeAgentOptions``
at session start; tests can drive the handlers directly without the
SDK installed.
"""

from __future__ import annotations
