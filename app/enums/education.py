"""
Controlled vocabularies for education normalization (degree level, field of
study) and the deterministic education-match result - shared by both the
resume-side and JD-side AI extraction schemas and by EducationMatchingService.
Kept as plain Python enums here (not a DB-native Postgres enum, no new
tables) - see EducationMatchingService's module docstring for why no
education DB persistence exists yet.
"""
from enum import Enum


class DegreeLevel(str, Enum):
    CERTIFICATE = "CERTIFICATE"
    DIPLOMA = "DIPLOMA"
    ASSOCIATE = "ASSOCIATE"
    BACHELOR = "BACHELOR"
    POSTGRADUATE_DIPLOMA = "POSTGRADUATE_DIPLOMA"
    MASTER = "MASTER"
    DOCTORATE = "DOCTORATE"
    PROFESSIONAL = "PROFESSIONAL"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


# Ordinal rank for "does candidate meet/exceed the required level"
# comparisons. PROFESSIONAL/OTHER/UNKNOWN are deliberately unranked (absent
# from this dict) - there is no safe, universal ordering for a professional
# certification (e.g. PMP, CPA) or an OTHER/UNKNOWN bucket against the
# academic ladder; a comparison involving one of those three is PARTIAL_MATCH
# at best, never a confident EXCEEDS/MISMATCH (see EducationMatchingService).
DEGREE_LEVEL_RANK: dict[DegreeLevel, int] = {
    DegreeLevel.CERTIFICATE: 1,
    DegreeLevel.DIPLOMA: 2,
    DegreeLevel.ASSOCIATE: 3,
    DegreeLevel.BACHELOR: 4,
    DegreeLevel.POSTGRADUATE_DIPLOMA: 5,
    DegreeLevel.MASTER: 6,
    DegreeLevel.DOCTORATE: 7,
}


class EducationField(str, Enum):
    """
    Deliberately scoped to AIRS's current hiring domain (software/technical
    roles) rather than an exhaustive academic taxonomy - Part 12's "do not
    invent a field" instruction. Extend this vocabulary (and
    RELATED_EDUCATION_FIELDS below) as new roles need it, rather than
    introducing a second field-classification mechanism.
    """
    COMPUTER_SCIENCE = "COMPUTER_SCIENCE"
    INFORMATION_TECHNOLOGY = "INFORMATION_TECHNOLOGY"
    SOFTWARE_ENGINEERING = "SOFTWARE_ENGINEERING"
    DATA_SCIENCE = "DATA_SCIENCE"
    ELECTRONICS_ENGINEERING = "ELECTRONICS_ENGINEERING"
    ELECTRICAL_ENGINEERING = "ELECTRICAL_ENGINEERING"
    MECHANICAL_ENGINEERING = "MECHANICAL_ENGINEERING"
    CIVIL_ENGINEERING = "CIVIL_ENGINEERING"
    MATHEMATICS = "MATHEMATICS"
    STATISTICS = "STATISTICS"
    BUSINESS_ADMINISTRATION = "BUSINESS_ADMINISTRATION"
    COMMERCE = "COMMERCE"
    ECONOMICS = "ECONOMICS"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


# Symmetric "related field" adjacency - used by EducationMatchingService to
# decide RELATED_FIELD_MATCH vs DISCIPLINE_MISMATCH when the JD explicitly
# allows a related field. Every key's set is the OTHER fields considered
# related to it; a field is never "related" to itself (that's FULL_MATCH,
# handled separately).
RELATED_EDUCATION_FIELDS: dict[EducationField, frozenset] = {
    EducationField.COMPUTER_SCIENCE: frozenset({
        EducationField.INFORMATION_TECHNOLOGY, EducationField.SOFTWARE_ENGINEERING, EducationField.DATA_SCIENCE,
    }),
    EducationField.INFORMATION_TECHNOLOGY: frozenset({
        EducationField.COMPUTER_SCIENCE, EducationField.SOFTWARE_ENGINEERING, EducationField.DATA_SCIENCE,
    }),
    EducationField.SOFTWARE_ENGINEERING: frozenset({
        EducationField.COMPUTER_SCIENCE, EducationField.INFORMATION_TECHNOLOGY, EducationField.DATA_SCIENCE,
    }),
    EducationField.DATA_SCIENCE: frozenset({
        EducationField.COMPUTER_SCIENCE, EducationField.INFORMATION_TECHNOLOGY,
        EducationField.STATISTICS, EducationField.MATHEMATICS,
    }),
    EducationField.ELECTRONICS_ENGINEERING: frozenset({EducationField.ELECTRICAL_ENGINEERING}),
    EducationField.ELECTRICAL_ENGINEERING: frozenset({EducationField.ELECTRONICS_ENGINEERING}),
    EducationField.STATISTICS: frozenset({EducationField.MATHEMATICS, EducationField.DATA_SCIENCE}),
    EducationField.MATHEMATICS: frozenset({EducationField.STATISTICS, EducationField.DATA_SCIENCE}),
    EducationField.COMMERCE: frozenset({EducationField.BUSINESS_ADMINISTRATION, EducationField.ECONOMICS}),
    EducationField.BUSINESS_ADMINISTRATION: frozenset({EducationField.COMMERCE, EducationField.ECONOMICS}),
    EducationField.ECONOMICS: frozenset({EducationField.COMMERCE, EducationField.BUSINESS_ADMINISTRATION}),
}


class EducationMatchResult(str, Enum):
    FULL_MATCH = "FULL_MATCH"
    RELATED_FIELD_MATCH = "RELATED_FIELD_MATCH"
    DEGREE_LEVEL_EXCEEDS = "DEGREE_LEVEL_EXCEEDS"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    DEGREE_LEVEL_MISMATCH = "DEGREE_LEVEL_MISMATCH"
    DISCIPLINE_MISMATCH = "DISCIPLINE_MISMATCH"
    NO_EDUCATION_DATA = "NO_EDUCATION_DATA"
    UNKNOWN = "UNKNOWN"
