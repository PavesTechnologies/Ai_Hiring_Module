from app.core.config import settings

# Created on first use, not at import: an isolated local stack
# (STORAGE_BACKEND=local) has no Supabase credentials and never connects.
_supabase = None


def get_supabase():
    global _supabase
    if _supabase is None:
        from supabase import create_client
        _supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SECRET_KEY)
    return _supabase
