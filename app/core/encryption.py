import os

from dotenv import dotenv_values

_ENV_VAR_PREFIX = "ENCRYPTION_KEY_"


def _alias_to_env_var(key_alias: str) -> str:
    return _ENV_VAR_PREFIX + key_alias.upper().replace("-", "_").replace(" ", "_")


def resolve_key_material(key_alias: str) -> bytes:
    """
    Resolves the raw Fernet key bytes for a given `encryption_keys.key_alias`
    from the environment — never from the database. Checks the real process
    environment first (production), falling back to `.env` (local dev), since
    the app's Settings object does not expose arbitrary per-alias secrets.
    """
    env_var = _alias_to_env_var(key_alias)

    raw = os.environ.get(env_var)
    if not raw:
        raw = dotenv_values(".env").get(env_var)

    if not raw:
        raise ValueError(
            f"No key material configured for encryption key alias '{key_alias}' "
            f"(expected environment variable '{env_var}')."
        )

    return raw.encode("utf-8")
