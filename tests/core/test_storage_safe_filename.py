import re

import pytest

from app.core.storage_service import storage_safe_filename

"""
Supabase Storage rejects object keys with characters like [ ] (400
InvalidKey) - job-board exports such as Naukri's "Name[24y_0m].pdf" broke
bulk ZIP extraction. Keys use a storage-safe copy of the name.
"""

_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


@pytest.mark.parametrize("name,expected", [
    ("Naukri_JANEDOE[24y_0m].pdf", "Naukri_JANEDOE_24y_0m.pdf"),
    ("John Doe - Resume (final).DOCX", "John_Doe_-_Resume_final.docx"),
    ("résumé_José.pdf", "resume_Jose.pdf"),
    ("cv#1 {new} 100%.pdf", "cv_1_new_100.pdf"),
    ("plain.pdf", "plain.pdf"),
    ("no_extension", "no_extension"),
])
def test_names_become_safe_keys(name, expected):
    assert storage_safe_filename(name) == expected


@pytest.mark.parametrize("name", ["履歴書.pdf", "[].pdf", "", "....pdf"])
def test_unusable_stems_fall_back_to_file(name):
    safe = storage_safe_filename(name)
    assert safe in ("file.pdf", "file")


def test_long_names_are_truncated_but_keep_extension():
    safe = storage_safe_filename("a" * 300 + ".pdf")
    assert safe.endswith(".pdf") and len(safe) <= 104


@pytest.mark.parametrize("name", [
    "Naukri_X[1y_2m].pdf", "a/b\\c.pdf", "tab\tname.pdf", "emoji 😀.docx", "quote'\"s.pdf",
])
def test_result_only_uses_key_safe_characters(name):
    assert _SAFE.match(storage_safe_filename(name))
