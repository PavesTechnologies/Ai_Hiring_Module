"""
The local env file the app reads its settings and key material from.

`.env` in the working directory, unless AIRS_ENV_FILE names another file - for a git worktree, which has no .env
of its own (it is git-ignored), so it can use the main checkout's instead of a copy:

    AIRS_ENV_FILE=C:/Ai_Hiring_Module/.env python -m pytest

The real process environment still comes first everywhere (production sets variables, not a file). A path
named by AIRS_ENV_FILE must exist: a missing file would otherwise be skipped silently and show up as
unrelated "field required" errors.
"""
import os
from pathlib import Path

ENV_FILE_VARIABLE = "AIRS_ENV_FILE"
DEFAULT_ENV_FILE = ".env"


class EnvFileMissing(RuntimeError):
    pass


def env_file_path() -> str:
    named = os.environ.get(ENV_FILE_VARIABLE, "").strip()
    if not named:
        return DEFAULT_ENV_FILE
    if not Path(named).is_file():
        raise EnvFileMissing(f"{ENV_FILE_VARIABLE} names {named}, which is not a file.")
    return named
