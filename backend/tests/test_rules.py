"""Synthetic legal matching fixtures are not real normative assertions."""

import pytest
from pydantic import ValidationError

from fortebank_api.rules import RuleCatalog, RuleEngine


def test_shipped_catalog_never_claims_verified_law():
    engine = RuleEngine()
    assert all(rule.review_status == "pending_review" for rule in engine.catalog.rules)
    assert all(rule.provision is None for rule in engine.catalog.rules)
    assert all(event.review_status == "pending_review" for event in engine.catalog.events)
    assessment = engine.assess("CASH_UNSECURED", {})
    reference = assessment.regulatory_references[0]
    assert reference.status == "insufficient_data"
    assert len(reference.missing_conditions) == 3
    assert reference.rule.source_url == "https://adilet.zan.kz/rus/docs/V1900019680"
    assert assessment.severity == "undetermined"


def test_pending_rule_even_when_all_conditions_established():
    engine = RuleEngine()
    conditions = engine.candidates("CASH_UNSECURED")[0].required_conditions
    assert engine.assess("CASH_UNSECURED", conditions).regulatory_references[0].status == "requires_review"


@pytest.mark.parametrize("code,status", [("UNSEEN", "unknown_event"), ("DOOR_LEFT_OPEN", "missing_source")])
def test_unknown_or_missing_source_is_not_ignored(code, status):
    result = RuleEngine().assess(code, {})
    assert result.status == status
    assert result.regulatory_references == []


def test_false_condition_is_not_applicable_even_with_missing_context():
    result = RuleEngine().assess("CASH_UNSECURED", {"cash_previously_accepted": False})
    assert result.regulatory_references[0].status == "not_applicable"


def synthetic_catalog():
    raw = RuleEngine().catalog.model_dump(mode="json")
    # Test fixture only: fictional source on reserved .invalid, never production data.
    for event in raw["events"]:
        event["review_status"] = "verified"
    rule = raw["rules"][0]
    rule.update(rule_id="synthetic-rule", document_title="Synthetic test requirement",
                source_url="https://example.invalid/test", requirement_summary="Synthetic requirement",
                revision_version="fixture", checked_at="2026-10-18", review_status="verified")
    mapping = raw["mappings"][0]
    mapping.update(rule_id="synthetic-rule", review_status="verified")
    raw["rules"].append({**rule, "rule_id": "synthetic-rule-2"})
    raw["mappings"].append({**mapping, "rule_id": "synthetic-rule-2"})
    raw["mappings"].append({**mapping, "event_code": "DOOR_LEFT_OPEN"})
    return raw


def test_many_to_many_candidates_require_conditions_and_human_review():
    engine = RuleEngine(RuleCatalog.model_validate(synthetic_catalog()))
    conditions = engine.candidates("CASH_UNSECURED")[0].required_conditions
    result = engine.assess("CASH_UNSECURED", conditions)
    assert len(result.regulatory_references) == 2
    assert all(ref.status == "candidate" for ref in result.regulatory_references)
    assert result.status == "requires_review"
    assert len(engine.candidates("DOOR_LEFT_OPEN")) == 1


@pytest.mark.parametrize("mutation", ["dangling", "duplicate", "unsafe_url", "no_source"])
def test_invalid_catalog_fails_closed(mutation):
    raw = synthetic_catalog()
    if mutation == "dangling":
        raw["mappings"][0]["rule_id"] = "missing"
    elif mutation == "duplicate":
        raw["events"].append(raw["events"][0])
    elif mutation == "unsafe_url":
        raw["rules"][0]["source_url"] = "javascript:alert(1)"
    else:
        raw["rules"][0]["source_url"] = None
    with pytest.raises(ValidationError):
        RuleCatalog.model_validate(raw)


@pytest.mark.parametrize("value", [1, "true", None])
def test_conditions_are_not_coerced(value):
    with pytest.raises(ValueError):
        RuleEngine().assess("CASH_UNSECURED", {"cash_previously_accepted": value})


def test_pending_rule_without_source_reports_missing_source():
    raw = RuleEngine().catalog.model_dump(mode="json")
    raw["rules"][0]["source_url"] = None
    engine = RuleEngine(RuleCatalog.model_validate(raw))
    assert engine.assess("CASH_UNSECURED", {}).status == "missing_source"
