"""Planner tools (build step 15; master document 4.3): reminders, to-dos and deadlines in
HCWMA's own database (`planner_store.py`). No cloud calendar.

- `planner_add` (T2, writes HCWMA's own data only): a reminder, to-do or deadline. The day
  and time come from the owner's own words, worked out by code (`when_parser.py`), never by
  the model's date arithmetic. Reminders and deadlines show a desktop notification from the
  tray app at the due time (`POST /reminders/due`).
- `planner_list` (T1): what is planned, for a day, a week or everything.
- `planner_close` (T2): mark one item done or cancelled. Nothing is ever erased.

Every successful call is answered by code (`answer`), so a planner request needs one model
call (to fill in the arguments) and no second one to word the reply.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from integrations.adapters.local_data.planner_store import Kind, PlannerItem, PlannerStore
from integrations.adapters.local_data.when_parser import (
    WhenError,
    describe,
    parse_day_range,
    parse_when,
)
from integrations.mcp.tool_schemas import (
    PermissionTier,
    ToolManifest,
    ToolPermissions,
    ToolSpec,
    ToolStatus,
)

# Owner approved the manifests on 2026-11-19 (add-tool skill, section 4). A new or changed
# tool here starts as "quarantined" again until the owner has seen it.
PLANNER_TOOLS_STATUS: ToolStatus = "approved"
_SCOPE = ToolPermissions(filesystem=["hcwma_data/planner"])
MAX_LIST = 30


class PlannerRefused(RuntimeError):
    """The request cannot be done as asked. The message is safe to show to the agent."""


@dataclass(frozen=True)
class PlannerDeps:
    store: PlannerStore
    now: Callable[[], datetime] = field(default=lambda: datetime.now().replace(microsecond=0))


def _when_text(item: PlannerItem) -> str:
    return describe(datetime.fromisoformat(item.due)) if item.due else "no date"


# ---------- planner_add ----------


class PlannerAddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200, description="what to do, e.g. 'pay the rent'")
    when: str | None = Field(
        default=None,
        max_length=80,
        description="the owner's own time words, copied exactly: 'on Friday', 'tomorrow at 9'",
    )
    kind: Literal["reminder", "todo", "deadline"] = "reminder"

    @field_validator("title")
    @classmethod
    def _one_line(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("the title is empty")
        return value


class PlannerAddOutput(BaseModel):
    item: PlannerItem
    due_text: str
    notifies: bool


def _add(deps: PlannerDeps, args: PlannerAddInput) -> PlannerAddOutput:
    now = deps.now()
    due: datetime | None = None
    if args.when and args.when.strip():
        try:
            due = parse_when(args.when, now)
        except WhenError as exc:
            raise PlannerRefused(f"{exc}; ask the owner for the day and time") from None
        if due < now:
            raise PlannerRefused(f"{describe(due)} is in the past; ask the owner for a new time")
    elif args.kind != "todo":
        raise PlannerRefused(f"a {args.kind} needs a day or time; ask the owner when")
    item = deps.store.add(args.kind, args.title, due, now)
    return PlannerAddOutput(item=item, due_text=_when_text(item), notifies=args.kind != "todo")


def answer_add(out: PlannerAddOutput) -> str:
    kind = out.item.kind.replace("todo", "to-do").capitalize()
    if out.item.due is None:
        return f"{kind} added: {out.item.title} (no date). Item #{out.item.id}."
    text = f"{kind} set: {out.item.title} — {out.due_text}. Item #{out.item.id}."
    if out.notifies:
        text += " HCWMA will show a desktop notification then (the app must be running)."
    return text


# ---------- planner_list ----------


class PlannerListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str = Field(
        default="all",
        max_length=40,
        description="'all', 'today', 'tomorrow', 'this week', 'next week', a weekday or a date",
    )
    kind: Literal["any", "reminder", "todo", "deadline"] = "any"
    include_done: bool = False


class PlannerListOutput(BaseModel):
    range_text: str
    items: list[PlannerItem]
    due_texts: list[str]
    more: bool = False


def _range_text(bounds: tuple[datetime, datetime] | None) -> str:
    if bounds is None:
        return "everything planned"
    start, end = bounds
    last = end - timedelta(days=1)
    if last.date() == start.date():
        return f"{start:%a} {start.day} {start:%b %Y}"
    return f"{start:%a} {start.day} {start:%b} to {last:%a} {last.day} {last:%b %Y}"


def _list(deps: PlannerDeps, args: PlannerListInput) -> PlannerListOutput:
    now = deps.now()
    try:
        bounds = parse_day_range(args.day, now)
    except WhenError as exc:
        raise PlannerRefused(str(exc)) from None
    kind: Kind | None = None if args.kind == "any" else args.kind
    start, end = bounds if bounds else (None, None)
    found = deps.store.items(
        start=start, end=end, kind=kind, include_closed=args.include_done, limit=MAX_LIST + 1
    )
    range_text = _range_text(bounds)
    shown = found[:MAX_LIST]
    return PlannerListOutput(
        range_text=range_text,
        items=shown,
        due_texts=[_when_text(i) for i in shown],
        more=len(found) > MAX_LIST,
    )


def answer_list(out: PlannerListOutput) -> str:
    if not out.items:
        return f"Nothing planned ({out.range_text})."
    lines = [f"Planned ({out.range_text}):"]
    for item, when in zip(out.items, out.due_texts, strict=True):
        kind = item.kind.replace("todo", "to-do")
        done = "" if item.status == "open" else f" [{item.status}]"
        lines.append(f"- #{item.id} {when} — {item.title} ({kind}){done}")
    if out.more:
        lines.append(f"(showing the first {MAX_LIST})")
    return "\n".join(lines)


# ---------- planner_close ----------


class PlannerCloseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int = Field(ge=1, description="the item number (#) from planner_list")
    status: Literal["done", "cancelled"] = "done"


class PlannerCloseOutput(BaseModel):
    item: PlannerItem


def _close(deps: PlannerDeps, args: PlannerCloseInput) -> PlannerCloseOutput:
    item = deps.store.close(args.item_id, args.status, deps.now())
    if item is None:
        existing = deps.store.get(args.item_id)
        if existing is None:
            raise PlannerRefused(f"there is no item #{args.item_id}; list the items first")
        raise PlannerRefused(f"item #{args.item_id} is already {existing.status}")
    return PlannerCloseOutput(item=item)


def answer_close(out: PlannerCloseOutput) -> str:
    word = "done" if out.item.status == "done" else "cancelled"
    return f"Marked {word}: #{out.item.id} {out.item.title}."


# ---------- specs ----------


def planner_tool_specs(deps: PlannerDeps, status: ToolStatus = "quarantined") -> list[ToolSpec]:
    def manifest(name: str, description: str, tier: PermissionTier, writes: bool) -> ToolManifest:
        return ToolManifest(
            name=name,
            version="0.1.0",
            created_by="developer",
            status=status,
            route="local_data",
            description=description,
            permission_tier=tier,
            side_effects="writes" if writes else "none",
            permissions=_SCOPE,
            timeout_s=10,
        )

    return [
        ToolSpec(
            manifest=manifest(
                "planner_add",
                "Add a reminder, to-do or deadline to HCWMA's own planner. Copy the owner's "
                "time words into `when` exactly; code works out the date.",
                PermissionTier.T2,
                writes=True,
            ),
            input_model=PlannerAddInput,
            output_model=PlannerAddOutput,
            handler=lambda a: _add(deps, a),
            answer=answer_add,
        ),
        ToolSpec(
            manifest=manifest(
                "planner_list",
                "List planned reminders, to-dos and deadlines for a day, a week or all.",
                PermissionTier.T1,
                writes=False,
            ),
            input_model=PlannerListInput,
            output_model=PlannerListOutput,
            handler=lambda a: _list(deps, a),
            answer=answer_list,
        ),
        ToolSpec(
            manifest=manifest(
                "planner_close",
                "Mark one planner item (by its # number) done or cancelled. Nothing is erased.",
                PermissionTier.T2,
                writes=True,
            ),
            input_model=PlannerCloseInput,
            output_model=PlannerCloseOutput,
            handler=lambda a: _close(deps, a),
            answer=answer_close,
        ),
    ]


def default_planner_tool_specs() -> list[ToolSpec]:
    """The planner tools on the real database in the HCWMA data folder."""
    return planner_tool_specs(PlannerDeps(PlannerStore.default()), status=PLANNER_TOOLS_STATUS)
