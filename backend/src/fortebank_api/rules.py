"""Offline, deterministic candidate matching; never a legal verdict."""

from __future__ import annotations

from datetime import date
from importlib.resources import files
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=4096)]
Review = Literal["pending_review", "verified"]


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventDefinition(CatalogModel):
    event_code: Text
    name: Text
    description: Text
    preliminary_risk: Literal["undetermined", "low", "medium", "high", "critical"]
    applicability: list[Text]
    definition_version: Text
    review_status: Review


class LegalRule(CatalogModel):
    rule_id: Text
    document_title: Text
    document_type: Literal["state_regulation", "internal_policy"]
    document_number: str | None = None
    document_date: date | None = None
    provision: str | None = None
    candidate_provision: str | None = None
    source_url: str | None = None
    requirement_summary: str | None = None
    revision_date: date | None = None
    revision_version: str | None = None
    checked_at: date | None = None
    review_status: Review
    limitations: Text

    @field_validator("source_url")
    @classmethod
    def safe_url(cls, value: str | None) -> str | None:
        if value is not None:
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("A source must be a public HTTPS URL without credentials")
        return value

    @model_validator(mode="after")
    def verified_source(self) -> LegalRule:
        if self.review_status == "verified" and not all((
            self.source_url, self.requirement_summary, self.checked_at, self.revision_version,
        )):
            raise ValueError("Verified rules need a source, summary, check date and revision")
        return self


class RuleMapping(CatalogModel):
    event_code: Text
    rule_id: Text
    required_conditions: Annotated[dict[str, StrictBool], Field(min_length=1)]
    explanation: Text
    review_status: Review


class RuleCatalog(CatalogModel):
    version: Text
    events: list[EventDefinition]
    rules: list[LegalRule]
    mappings: list[RuleMapping]

    @model_validator(mode="after")
    def references(self) -> RuleCatalog:
        codes = {event.event_code for event in self.events}
        rules = {rule.rule_id for rule in self.rules}
        pairs = {(m.event_code, m.rule_id) for m in self.mappings}
        if len(codes) != len(self.events) or len(rules) != len(self.rules) or len(pairs) != len(self.mappings):
            raise ValueError("Duplicate catalog identifiers")
        if any(m.event_code not in codes or m.rule_id not in rules for m in self.mappings):
            raise ValueError("Dangling catalog reference")
        return self


class RegulatoryReference(CatalogModel):
    rule: LegalRule
    status: Literal["requires_review", "insufficient_data", "not_applicable", "candidate"]
    missing_conditions: list[str]
    explanation: str


class RuleAssessment(CatalogModel):
    event_code: str
    name: str
    severity: str
    definition_version: str | None
    catalog_version: str
    status: Literal["unknown_event", "missing_source", "requires_review"]
    explanation: str
    regulatory_references: list[RegulatoryReference]


class RuleEngine:
    def __init__(self, catalog: RuleCatalog | None = None) -> None:
        self.catalog = catalog or RuleCatalog.model_validate_json(
            files("fortebank_api").joinpath("rule_catalog.json").read_text(encoding="utf-8")
        )
        self._events = {event.event_code: event for event in self.catalog.events}
        self._rules = {rule.rule_id: rule for rule in self.catalog.rules}

    def candidates(self, event_code: str) -> list[RuleMapping]:
        return [m for m in self.catalog.mappings if m.event_code == event_code]

    def assess(self, event_code: str, conditions: dict[str, bool]) -> RuleAssessment:
        if any(not isinstance(key, str) or type(value) is not bool for key, value in conditions.items()):
            raise ValueError("Conditions must be established boolean facts")
        definition = self._events.get(event_code)
        references = []
        for mapping in self.candidates(event_code):
            rule = self._rules[mapping.rule_id]
            missing = sorted(key for key in mapping.required_conditions if key not in conditions)
            contradicted = any(
                key in conditions and conditions[key] != expected
                for key, expected in mapping.required_conditions.items()
            )
            if contradicted:
                status, message = "not_applicable", "Установленные условия не соответствуют привязке."
            elif missing:
                status, message = "insufficient_data", "Недостаточно данных для применения нормы."
            elif (mapping.review_status != "verified" or rule.review_status != "verified"
                  or definition is None or definition.review_status != "verified"):
                status, message = "requires_review", "Привязка требует юридической проверки."
            else:
                status, message = "candidate", "Условия сопоставления выполнены; требуется оценка специалиста."
            references.append(RegulatoryReference(
                rule=rule, status=status, missing_conditions=missing,
                explanation=f"{message} {mapping.explanation}",
            ))
        return RuleAssessment(
            event_code=event_code,
            name=definition.name if definition else event_code,
            severity=definition.preliminary_risk if definition else "undetermined",
            definition_version=definition.definition_version if definition else None,
            catalog_version=self.catalog.version,
            status="unknown_event" if definition is None else "missing_source"
            if not references or not any(ref.rule.source_url for ref in references) else "requires_review",
            explanation="Наблюдаемое событие — подсказка человеку, не доказанное нарушение закона. "
                        "Уровень риска предварительный и не определяет юридическую ответственность.",
            regulatory_references=references,
        )
