"""The pure parts of the checks before every email: the address format and empty placeholder values."""
from types import SimpleNamespace

import pytest

from app.services.notifications.send_policy import empty_value_problems, valid_address


@pytest.mark.parametrize("address", ["a@example.invalid", "first.last+tag@sub.example.co.in", "x@e2e.invalid"])
def test_valid_addresses(address):
    assert valid_address(address)


@pytest.mark.parametrize("address", [None, "", "no-at-sign", "two@@example.com", "a@b", "a b@example.com",
                                     "a@example.c", "<a@example.com>", "a@example.com, b@example.com",
                                     "x" * 250 + "@example.com"])
def test_invalid_addresses(address):
    assert not valid_address(address)


def _template(subject, body):
    return SimpleNamespace(subject=subject, body_template=body)


def test_an_empty_value_is_reported_by_name():
    found = empty_value_problems(_template("Interview for {job_title}", "Hi {candidate_name}"),
                                 {"job_title": "Engineer", "candidate_name": "  "})
    assert [p.code for p in found] == ["TEMPLATE_VALUE_EMPTY"] and "{candidate_name}" in found[0].message


def test_optional_blocks_may_be_empty_and_unused_values_dont_matter():
    template = _template("Interview", "Hi {candidate_name}. {notes_block}{meeting_info}")
    assert empty_value_problems(template, {"candidate_name": "Asha", "notes_block": "", "meeting_info": None,
                                           "job_title": ""}) == []
