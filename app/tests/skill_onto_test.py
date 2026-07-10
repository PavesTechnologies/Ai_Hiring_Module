"""
Import ESCO's digitalSkillCollection_en into the skill_ontology table.

This file is ESCO's own pre-curated IT/digital skills subset — scoped to the
software/IT domain already, no separate filtering step needed.

Usage:
    python import_esco_skills.py /path/to/digitalSkillCollection_en.csv
    python import_esco_skills.py /path/to/digitalSkillCollection_en.csv --dry-run

ADJUST BEFORE RUNNING:
    The two imports below (SessionLocal, SkillOntology) are placeholders based on
    the file structure you've shown me so far. Point them at your actual modules:
      - SessionLocal likely lives in app/db/session.py (confirmed from your pasted file)
      - SkillOntology's actual import path I haven't seen yet — adjust the path below.
"""

import argparse

import pandas as pd

from app.db.session import SessionLocal          # ADJUST if your path differs
from app.models.skills import SkillOntology  # ADJUST to actual location


REQUIRED_COLUMNS = {"preferredLabel", "altLabels", "skillType", "status", "broaderConceptPT"}


def split_multivalue(value) -> list[str]:
    """
    ESCO isn't fully consistent about separators across files: skills_en.csv used
    newlines for altLabels, but this file's broaderConceptUri/PT columns use ' | '.
    We don't have a confirmed multi-value altLabels example from THIS file, so we
    handle both: split on newlines first, then split each resulting piece on ' | '
    too, so whichever convention this file actually uses gets caught correctly.
    """
    if pd.isna(value):
        return []
    pieces = []
    for line in str(value).split("\n"):
        pieces.extend(part.strip() for part in line.split("|"))
    return [p for p in pieces if p]


def first_category(value) -> str | None:
    """broaderConceptPT may list multiple pipe-separated categories; take the
    most specific (first) one for skill_ontology.category."""
    if pd.isna(value):
        return None
    first = str(value).split("|")[0].strip()
    return first or None


def sanity_check(df: pd.DataFrame) -> None:
    """
    Guards against silent column misalignment caused by an unescaped comma
    somewhere in the file shifting every field after it on that row.
    """
    missing_cols = REQUIRED_COLUMNS - set(df.columns)
    if missing_cols:
        raise ValueError(
            f"Expected columns not found: {missing_cols}. "
            f"Got columns: {list(df.columns)}. "
            "This usually means the CSV didn't parse the way we expect — "
            "check for unquoted commas in any text field."
        )

    empty_labels = df["preferredLabel"].isna().sum()
    empty_ratio = empty_labels / len(df) if len(df) else 0
    if empty_ratio > 0.05:
        raise ValueError(
            f"{empty_labels} of {len(df)} rows ({empty_ratio:.1%}) have an empty "
            "preferredLabel. That's high enough to suggest column misalignment "
            "rather than genuinely missing data — aborting before writing bad rows. "
            "Inspect the raw CSV around the first few empty rows."
        )

    # spot-check: preferredLabel values should look like short phrases, not
    # URLs or ISO timestamps, which is what you'd see if columns shifted.
    sample = df["preferredLabel"].dropna().head(20)
    suspicious = sample[sample.str.contains(r"^https?://|^\d{4}-\d{2}-\d{2}", regex=True, na=False)]
    if not suspicious.empty:
        raise ValueError(
            f"Some preferredLabel values look like URLs or dates, not skill names: "
            f"{suspicious.tolist()}. This strongly suggests column misalignment — aborting."
        )


def import_esco_skills(csv_path: str, dry_run: bool = False) -> None:
    df = pd.read_csv(csv_path)
    sanity_check(df)

    db = SessionLocal()
    inserted, updated, skipped = 0, 0, 0

    try:
        for _, row in df.iterrows():
            preferred_label = str(row.get("preferredLabel", "")).strip()
            if not preferred_label or preferred_label.lower() == "nan":
                skipped += 1
                continue

            alt_aliases = split_multivalue(row.get("altLabels"))
            # dedupe while preserving order; exclude the canonical name itself
            seen = set()
            aliases = []
            for alias in alt_aliases:
                key = alias.lower()
                if key != preferred_label.lower() and key not in seen:
                    seen.add(key)
                    aliases.append(alias)

            category = first_category(row.get("broaderConceptPT"))
            is_active = str(row.get("status", "")).strip().lower() == "released"

            existing = (
                db.query(SkillOntology)
                .filter(SkillOntology.canonical_name == preferred_label)
                .first()
            )

            if existing:
                existing.aliases = aliases
                existing.category = category
                existing.is_active = is_active
                existing.source = "ESCO"
                updated += 1
            else:
                db.add(
                    SkillOntology(
                        canonical_name=preferred_label,
                        aliases=aliases,
                        category=category,
                        is_active=is_active,
                        source="ESCO",
                        confidence="unverified",
                    )
                )
                inserted += 1

        if dry_run:
            db.rollback()
            print(f"[DRY RUN] Would insert {inserted}, update {updated}, skip {skipped}. No changes written.")
        else:
            db.commit()
            print(f"Done. Inserted {inserted}, updated {updated}, skipped {skipped} rows.")

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import ESCO skills into skill_ontology")
    parser.add_argument("csv_path", help="Path to ESCO skills_en.csv")
    parser.add_argument("--dry-run", action="store_true", help="Parse and validate only, no DB writes")
    args = parser.parse_args()

    import_esco_skills(args.csv_path, dry_run=args.dry_run)