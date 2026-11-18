"""Step 12 (Image Editing agent) "Done when" check, run by hand (needs Ollama running):

    python -m agents.router.demo_image

The real stack: real Router and agent on Qwen, `configs/permissions.yaml` as it is, the image
tools on an in-process MCP server. No folder is approved yet, so three sample photos are made
with Pillow in a new folder `workspace/step12_demo_<time>/holiday/` (readable because it is in
the workspace). Copies go to `workspace/images/`.

1. "Resize all photos in <folder> to 1080 pixels wide" -> three copies, 1080 px wide.
2. "Crop <beach.jpg> to a square" -> a square copy.
3. A request for a picture in `~/.ssh` -> refused, honestly reported, nothing written.
4. The three originals are byte for byte unchanged and nothing outside `workspace/images/`
   and the demo folder changed.

At the end every tool call made is shown from the audit log.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from pathlib import Path

import anyio
from PIL import Image, ImageDraw

from agents.router.factory import open_router
from agents.router.router import Router, RouterReply
from integrations.adapters.pc.image_tools import ImageToolDeps, image_tool_specs
from integrations.registry.capability_catalog import CapabilityCatalog
from model.serving import Message
from security.audit.audit_logger import get_audit_logger
from security.permissions.config import REPO_ROOT
from security.permissions.path_check import PathPolicy

DEMO = f"step12_demo_{datetime.now():%Y%m%d_%H%M%S}"
PHOTOS = {"beach.jpg": (2000, 1200), "hotel.jpg": (1600, 900), "sunset.png": (1800, 1350)}
WATCHED = ("agents", "integrations", "memory", "security", "configs", "docs", "tests", "workspace")


def prepare(deps: ImageToolDeps) -> Path:
    folder = deps.policy.workspace / DEMO / "holiday"
    folder.mkdir(parents=True)
    for index, (name, size) in enumerate(PHOTOS.items()):
        img = Image.new("RGB", size, (30 + 60 * index, 120, 200 - 50 * index))
        ImageDraw.Draw(img).ellipse(
            (size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4), fill="gold"
        )
        img.save(folder / name)
    return folder


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(images_dir: Path, demo_dir: Path) -> dict[str, tuple[int, int]]:
    """(size, mtime) of every file the agent must not touch."""
    seen: dict[str, tuple[int, int]] = {}
    skip = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".git"}
    for name in WATCHED:
        for path in (REPO_ROOT / name).rglob("*"):
            if not path.is_file() or skip & set(path.parts):
                continue
            if images_dir in path.parents or demo_dir in path.parents:
                continue
            if path.name == "audit.jsonl" or path.suffix in {".sqlite", ".log"}:
                continue  # written by HCWMA itself on every call
            stat = path.stat()
            seen[str(path)] = (stat.st_size, int(stat.st_mtime))
    return seen


async def ask(router: Router, text: str) -> RouterReply:
    print(f"\n> {text}")
    reply = await router.handle([Message(role="user", content=text)])
    path = " -> ".join(["router", *reply.agents])
    stopped = f" | stopped by {reply.stopped_by}" if reply.stopped_by else ""
    print(f"  [{path} | {reply.action} | {reply.status} | tool calls {reply.tool_calls}{stopped}]")
    for line in reply.reply.strip().splitlines():
        print(f"  {line}")
    return reply


def _size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size


async def run() -> bool:
    audit = get_audit_logger()
    before_audit = len(audit.read_all())
    deps = ImageToolDeps(PathPolicy.from_config())
    holiday = prepare(deps)
    images = deps.out_dir
    originals = {p.name: _digest(p) for p in holiday.iterdir()}
    already = {p.name for p in images.iterdir()}
    outside_before = snapshot(images, holiday.parent)

    catalog = CapabilityCatalog()
    for spec in image_tool_specs(deps, status="approved"):
        catalog.register(spec)

    print(f"sample photos: {holiday}")
    async with open_router(catalog=catalog, with_memory=False) as router:
        resized = await ask(router, f"Resize all photos in {holiday} to 1080 pixels wide, please.")
        cropped = await ask(router, f"Crop {holiday / 'beach.jpg'} to a square and save a copy.")
        secret = Path.home() / ".ssh" / "holiday.png"
        refused = await ask(router, f"Resize {secret} to 500 pixels wide.")

    new = sorted(p for p in images.iterdir() if p.name not in already)
    # a name that was taken by an earlier run gets _2, _3 ...: match on the start
    by_stem = {
        p.stem.rsplit("_", 1)[0] if p.stem.rsplit("_", 1)[-1].isdigit() else p.stem: p for p in new
    }
    outside_after = snapshot(images, holiday.parent)
    calls = [r for r in audit.read_all()[before_audit:] if r.event == "tool_call"]
    print(f"\n--- audit log: {audit.path} ---")
    for rec in calls:
        print(f"  {rec.ts}  {rec.agent} -> {rec.tool} {str(rec.args)[:100]}  [{rec.outcome}]")
    print(f"\nnew files in {images}:")
    for path in new:
        print(f"  {path.name}  {_size(path)}")

    resized_files = [p for p in new if "_resized" in p.stem]
    square = [p for p in new if "_cropped" in p.stem]
    made = resized.tool_calls + cropped.tool_calls + refused.tool_calls
    checks = {
        "router -> image_editing for all three requests": all(
            r.agents == ["image_editing"] for r in (resized, cropped, refused)
        ),
        "three resized copies exist, each 1080 px wide": len(resized_files) >= 3
        and all(_size(p)[0] == 1080 for p in resized_files[:3])
        and {"beach_resized", "hotel_resized", "sunset_resized"} <= set(by_stem),
        "proportions kept (beach 2000x1200 -> 1080x648)": _size(by_stem["beach_resized"])
        == (1080, 648),
        "a square copy of beach.jpg exists": any(_size(p)[0] == _size(p)[1] for p in square)
        and bool(square),
        "originals are byte for byte unchanged": {p.name: _digest(p) for p in holiday.iterdir()}
        == originals,
        "the ~/.ssh picture request wrote nothing and says so": not any(
            "holiday" in p.name and p.stem.startswith("holiday") for p in new
        )
        and refused.status == "done",
        "nothing outside workspace/images/ changed": outside_after == outside_before,
        "every tool call is in the audit log": made >= 3 and len(calls) == made,
        "every audit record names the agent": all(r.agent == "image_editing" for r in calls),
    }
    print()
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = all(checks.values())
    print("\nSTEP 12 (IMAGE EDITING) DONE-WHEN:", "PASS" if passed else "FAIL")
    return passed


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    raise SystemExit(0 if anyio.run(run) else 1)


if __name__ == "__main__":
    main()
