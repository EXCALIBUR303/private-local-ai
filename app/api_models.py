"""Request bodies. Kept separate so main.py stays about behaviour."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32000)
    conversation_id: int | None = None
    model: str | None = None
    use_documents: bool = False
    #: Fold the current watch report into the prompt, so "how are my deploys?"
    #: can be answered from what the watchers already collected.
    use_watchers: bool = False
    persona: str | None = None


class NewConversation(BaseModel):
    title: str = Field(default="New chat", max_length=120)


class RenameConversation(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class FolderRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=5, ge=1, le=20)


class ProposeRequest(BaseModel):
    kind: Literal["search_indexed_files", "open_file", "open_url", "draft_reminder"]
    params: dict[str, Any] = Field(default_factory=dict)
    origin: Literal["user", "model-proposal"] = "user"


class ExecuteRequest(BaseModel):
    token: str = Field(min_length=8, max_length=200)
    approved: bool


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    voice: str | None = Field(default=None, max_length=40)
    rate: int | None = Field(default=None, ge=90, le=400)


class DeleteAllRequest(BaseModel):
    confirm_phrase: str


class SettingRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=2000)


class WatcherRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=120)
    settings: dict[str, Any] = Field(default_factory=dict)
    interval_s: int = Field(default=300, ge=60, le=86400)
    notify: Literal["all", "problems", "off"] = "problems"


class WatcherPatch(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    settings: dict[str, Any] | None = None
    interval_s: int | None = Field(default=None, ge=60, le=86400)
    enabled: bool | None = None
    notify: Literal["all", "problems", "off"] | None = None


class TokenRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    token: str = Field(min_length=4, max_length=500)


class EgressToggle(BaseModel):
    enabled: bool


class ScanRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class BuildRequest(BaseModel):
    task: str = Field(min_length=1, max_length=8000)
    path: str = Field(min_length=1, max_length=4096)
    model: str | None = None
    autonomy: Literal["off", "safe", "full"] = "off"
    rounds: int = Field(default=3, ge=1, le=6)
    verify_command: str = Field(default="", max_length=500)
