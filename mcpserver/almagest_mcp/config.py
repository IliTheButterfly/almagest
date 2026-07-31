"""Runtime configuration, read from the environment.

Same shape as `backend/app/config.py` and `deviceagent/agent/config.py` —
pydantic-settings, explicit env aliases, the repo-root `.env` — because these are
components of one system and every key is documented in one `.env.example`.

Keys are prefixed `ALMAGEST_MCP_`. Not `ALMAGEST_`, because none of these describe
the server the tags point at; not `DEVICEAGENT_`, because this process is not on
the Pi. It runs wherever the agent runs — a laptop, a CI job, a container next to
the API — and its settings are about that reach.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # Both, so `cd mcpserver && almagest-mcp` and a repo-root invocation read
        # the same file.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    #: The API **as reachable from wherever this server runs**. Deliberately not
    #: `ALMAGEST_BASE_URL`: that is the public origin stamped into every tag and
    #: printed label (`https://almagest.lan`, ADR 0001) and it must stay stamped
    #: on physical objects whatever route this process takes to the server. Same
    #: distinction, and same reason, as `DEVICEAGENT_API_BASE_URL`.
    api_base_url: str = Field(default="http://127.0.0.1:8000", alias="ALMAGEST_MCP_API_BASE_URL")

    #: Generous next to the agent's 5 s. Nothing here is holding a container over
    #: a reader waiting for an answer, and the expensive reads this exposes are
    #: genuinely expensive: `read_bom_suggestions` runs a parametric search per
    #: unmatched BOM line, and `search_datasheets` hits FTS5 across every
    #: extracted PDF. A timeout mid-BOM is a worse answer than a slow one.
    timeout_s: float = Field(default=30.0, alias="ALMAGEST_MCP_TIMEOUT_S", gt=0)

    #: **Off by default, and this is not timidity.** Reads are recoverable by
    #: definition; a write is recoverable but not *invisible*. The ledger is
    #: append-only with a compensating-row undo, so a wrong `consume` can always
    #: be reversed — but until somebody recounts that bin, the balance is a
    #: confident lie about physical reality, and the whole point of this system is
    #: that the balance can be trusted. A model that misreads "I used 3" as 30 has
    #: made the inventory wrong in a way no test catches.
    #:
    #: So: the read surface is always on and needs no decision, and turning on
    #: writes is one deliberate env var by someone who has decided they want an
    #: agent adjusting stock. When it is off the write tools are **not
    #: registered** rather than registered-and-refusing, so a model never sees a
    #: tool it cannot use and never spends a turn discovering that.
    allow_writes: bool = Field(default=False, alias="ALMAGEST_MCP_ALLOW_WRITES")

    #: Stamped on every ledger row this server writes, alongside
    #: `source="api"`. Makes "which of these movements did an agent make" a
    #: `WHERE device_id = ...` rather than an archaeology exercise.
    device_id: str = Field(default="mcp", alias="ALMAGEST_MCP_DEVICE_ID", min_length=1)

    @field_validator("api_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """So `api_base_url + path` is unambiguous at every call site.

        A trailing slash in the environment would otherwise produce
        `//api/parts/1`, which most servers tolerate and Starlette redirects —
        turning every request into two and losing the method on some proxies.
        """
        cleaned = value.strip().rstrip("/")
        if not cleaned:
            raise ValueError("ALMAGEST_MCP_API_BASE_URL must not be empty")
        return cleaned


@lru_cache(maxsize=1)
def get_settings() -> McpSettings:
    return McpSettings()
