# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded browser journey plans and evidence-based visual QA reports."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.workspace_tools import _private_name


def validate_browser_request(path: str, width: int, height: int, theme: str) -> None:
    parsed = urlsplit(path)
    decoded = unquote(parsed.path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or "\\" in decoded
        or any(ord(c) < 32 for c in path + decoded)
        or ".." in Path(decoded).parts
        or any(_private_name(part) for part in Path(decoded).parts)
    ):
        raise ValueError("Browser path must be an authorized relative project route without private paths")
    if type(width) is not int or type(height) is not int or not 320 <= width <= 2560 or not 320 <= height <= 1440:
        raise ValueError("Browser viewport is outside the supported bounds")
    if theme not in {"light", "dark"}:
        raise ValueError("Browser theme must be light or dark")


def state_identifier(value: str) -> str:
    if not value or len(value) > 64 or any(not (c.isascii() and (c.isalnum() or c in "-_")) for c in value):
        raise ValueError(
            "Journey and state identifiers must contain bounded ASCII letters, numbers, hyphens or underscores"
        )
    return value


class ExpectedState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    expected: str = Field(min_length=1, max_length=1000)
    _id = field_validator("id")(state_identifier)


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["navigate", "click", "fill", "select", "press", "scroll", "assert"]
    state_id: str
    selector: str | None = Field(default=None, max_length=300)
    role: Literal["button", "textbox", "link", "combobox", "checkbox", "radio", "dialog", "tab", "heading"] | None = (
        None
    )
    name: str | None = Field(default=None, max_length=160)
    value: str | None = Field(default=None, max_length=2000)
    assertion: (
        Literal["visible", "hidden", "text", "value", "enabled", "disabled", "checked", "unchecked", "url", "count"]
        | None
    ) = None
    x: int = Field(default=0, ge=-2000, le=2000, strict=True)
    y: int = Field(default=0, ge=-2000, le=2000, strict=True)
    timeout_ms: int = Field(default=5000, ge=100, le=10000, strict=True)
    screenshot: bool = False
    _state = field_validator("state_id")(state_identifier)

    @model_validator(mode="after")
    def valid_action(self) -> BrowserAction:
        if self.selector and (
            self.selector.startswith(("js=", "xpath=", "internal=")) or any(ord(c) < 32 for c in self.selector)
        ):
            raise ValueError("Browser selectors must be bounded CSS or accessible role locators")
        target = bool(self.selector or self.role)
        if self.selector and self.role:
            raise ValueError("Choose a CSS selector or accessible role locator")
        if self.kind in {"click", "fill", "select"} and not target:
            raise ValueError("This action requires an explicit project DOM locator")
        if self.kind in {"fill", "select", "navigate", "press"} and self.value is None:
            raise ValueError("This action requires a value")
        if self.kind == "navigate":
            validate_browser_request(str(self.value), 390, 844, "light")
        if self.kind == "press" and self.value not in {
            "Tab",
            "Shift+Tab",
            "Enter",
            "Escape",
            "Space",
            "ArrowDown",
            "ArrowUp",
            "ArrowLeft",
            "ArrowRight",
            "Control+A",
            "Meta+A",
        }:
            raise ValueError("Keyboard action must use a supported navigation or editing key")
        if self.kind == "assert":
            if self.assertion is None or (self.assertion != "url" and not target):
                raise ValueError("Assertions require a supported condition and explicit locator")
            if self.assertion in {"text", "value", "url", "count"} and self.value is None:
                raise ValueError("This assertion requires an expected value")
            if self.assertion == "url":
                validate_browser_request(str(self.value), 390, 844, "light")
            if self.assertion == "count" and (not str(self.value).isdigit() or not 0 <= int(str(self.value)) <= 1000):
                raise ValueError("Count assertions must be bounded integers")
        return self


class BrowserJourney(BaseModel):
    model_config = ConfigDict(extra="forbid")
    journey_id: str
    name: str = Field(min_length=1, max_length=160)
    path: str = Field(default="/", max_length=2000)
    width: int = Field(default=1440, ge=320, le=2560, strict=True)
    height: int = Field(default=900, ge=320, le=1440, strict=True)
    theme: Literal["light", "dark"] = "light"
    zoom: int = Field(default=100, ge=50, le=200, strict=True)
    expected_states: list[ExpectedState] = Field(min_length=1, max_length=20)
    actions: list[BrowserAction] = Field(default_factory=list, max_length=20)
    reference_id: str | None = Field(default=None, max_length=64)
    _id = field_validator("journey_id")(state_identifier)

    @model_validator(mode="after")
    def bounded_plan(self) -> BrowserJourney:
        validate_browser_request(self.path, self.width, self.height, self.theme)
        if len({state.id for state in self.expected_states}) != len(self.expected_states):
            raise ValueError("Expected state identifiers must be unique")
        if len({action.state_id for action in self.actions}) != len(self.actions):
            raise ValueError("Action state identifiers must be unique")
        if self.reference_id and sum(action.screenshot for action in self.actions) > 4:
            raise ValueError("An approved reference reserves one of five image attachment slots")
        if (
            sum(action.screenshot for action in self.actions) > 5
            or sum(action.timeout_ms for action in self.actions) > 120000
        ):
            raise ValueError("Journey exceeds its five screenshot or 120-second action budget")
        return self


class QAReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    sha256: str
    state_id: str
    width: int = Field(ge=320, le=2560, strict=True)
    height: int = Field(ge=320, le=1440, strict=True)
    theme: Literal["light", "dark"]
    zoom: int = Field(default=100, ge=50, le=200, strict=True)
    _state = field_validator("state_id")(state_identifier)

    @field_validator("sha256")
    @classmethod
    def digest(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Reference SHA256 must identify the approved actual image bytes")
        return value


class QAFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["critical", "high", "medium", "low"]
    journey_id: str
    state_id: str
    path: str = Field(min_length=1, max_length=2000)
    expected: str = Field(min_length=1, max_length=2000)
    observed: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=6)


class QAReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["vision", "dom_only", "not_requested"]
    reviewed_states: list[str] = Field(default_factory=list, max_length=40)
    reference_sha256: list[str] = Field(default_factory=list, max_length=10)
    findings: list[QAFinding] = Field(default_factory=list, max_length=50)
    not_reviewed: list[str] = Field(default_factory=list, max_length=40)
    summary: str = Field(min_length=1, max_length=4000)


@dataclass(frozen=True)
class EvidenceImage:
    data: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True)
class JourneyEvidence:
    report: dict[str, Any]
    images: list[EvidenceImage]


def bounded_image_history(messages: list[Any]) -> list[Any]:
    """Keep recent image bytes while preserving explicit evidence captions in history."""
    from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart

    retained: set[int] = set()
    total = 0
    for message in reversed(messages):
        for part in reversed(message.parts):
            if not isinstance(part, UserPromptPart) or isinstance(part.content, str):
                continue
            for item in reversed(part.content):
                if (
                    isinstance(item, BinaryContent)
                    and item.media_type.startswith("image/")
                    and len(retained) < 5
                    and total + len(item.data) <= 6_000_000
                ):
                    retained.add(id(item))
                    total += len(item.data)
    result = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            result.append(message)
            continue
        parts: list[Any] = []
        for part in message.parts:
            if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
                content = [
                    "[Earlier image bytes are not attached to this request; its caption is historical evidence only.]"
                    if isinstance(item, BinaryContent)
                    and item.media_type.startswith("image/")
                    and id(item) not in retained
                    else item
                    for item in part.content
                ]
                parts.append(replace(part, content=content))
            else:
                parts.append(part)
        result.append(replace(message, parts=parts))
    return result


def qa_instructions() -> str:
    return (
        "Browser QA requires a declared journey plan before execution: name the user goal, initial route, "
        "expected states, bounded DOM locators and assertions. Exercise navigation and real JavaScript interactions, "
        "keyboard focus, validation errors, loading behavior, mobile layouts, both themes and zoom when applicable. "
        "Capture the actual DOM, focus and screenshots for observed states. Compare only a matching approved "
        "reference with its SHA256, state, viewport, theme and zoom; report reference_not_reviewed otherwise. "
        "When actual images are attached, inspect their visual content and tie every finding to an evidence ID, "
        "severity, path, expected result and observed result. DOM-only evidence must remain labeled dom_only. "
        "Record states not reviewed and remaining uncertainty. A visual judgment cannot override failing tests, "
        "source boundaries, independent review or delivery guards. "
        "Do not assert pixel perfection or universal approval."
    )
