"""
What a settings change puts in its audit entry: ids, codes, numbers and flags only.

A changed field records {"before": ..., "after": ...}. A field people type into (a label, a description, a
source note, a template's wording) records only {"changed": true}: its text stays in the settings row (or
its version), never in the audit log.
"""


def audited_changes(before: dict, after: dict, text_fields=()) -> dict:
    """{field: {"before", "after"}} for every field whose value changed; typed-text fields as {"changed": True}."""
    changes = {}
    for field in after:
        if before.get(field) == after.get(field):
            continue
        changes[field] = {"changed": True} if field in text_fields else {"before": before.get(field),
                                                                        "after": after.get(field)}
    return changes
