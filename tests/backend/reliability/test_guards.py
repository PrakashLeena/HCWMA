"""Step 18: low RAM / low disk / on battery / paused model refuse new heavy tasks; the sleep
watch notices a wake-up; "Pause model" stops new inference and unloads Qwen."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic import ValidationError

from agents.runtime.model_queue import ModelPaused, ModelQueue
from backend.reliability.health import CachedProbe, run_probes
from backend.reliability.model_control import OllamaModelControl
from backend.reliability.resources import (
    ResourceSnapshot,
    heavy_task_refusal,
    low_resource_warnings,
    take_snapshot,
)
from backend.reliability.settings import AppSettings, SettingsStore
from backend.reliability.sleep_watch import watch_for_sleep
from model.serving import GenerateResult


def snap(
    ram_mb: int = 4096, disk_gb: float = 100.0, on_battery: bool | None = False
) -> ResourceSnapshot:
    return ResourceSnapshot(
        free_ram_mb=ram_mb,
        total_ram_mb=16_000,
        free_disk_gb=disk_gb,
        disk_path="C:\\",
        on_battery=on_battery,
        battery_percent=None if on_battery is None else 80,
    )


# ---------- low resources ----------


def test_enough_resources_allow_heavy_tasks() -> None:
    assert heavy_task_refusal(snap(), AppSettings()) is None
    assert low_resource_warnings(snap(), AppSettings()) == []


@pytest.mark.parametrize(
    ("snapshot", "words"),
    [
        (snap(ram_mb=900), "RAM"),
        (snap(disk_gb=4.2), "disk"),
    ],
)
def test_low_ram_or_disk_warns_and_refuses(snapshot: ResourceSnapshot, words: str) -> None:
    refusal = heavy_task_refusal(snapshot, AppSettings())
    assert refusal is not None and refusal.startswith("Not started") and words in refusal
    assert len(low_resource_warnings(snapshot, AppSettings())) == 1


def test_exactly_at_the_limits_is_still_allowed() -> None:
    assert heavy_task_refusal(snap(ram_mb=1024, disk_gb=5.0), AppSettings()) is None


def test_battery_rule_applies_only_when_the_owner_turned_it_on() -> None:
    on = AppSettings(heavy_only_when_plugged_in=True)
    assert heavy_task_refusal(snap(on_battery=True), AppSettings()) is None
    refusal = heavy_task_refusal(snap(on_battery=True), on)
    assert refusal is not None and "on battery" in refusal
    assert heavy_task_refusal(snap(on_battery=False), on) is None
    assert heavy_task_refusal(snap(on_battery=None), on) is None  # a desktop has no battery


def test_paused_model_refuses_first() -> None:
    refusal = heavy_task_refusal(snap(), AppSettings(), model_paused=True)
    assert refusal is not None and "paused" in refusal


def test_limits_can_be_made_stricter_but_never_looser(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        AppSettings(min_free_ram_mb=512)
    with pytest.raises(ValidationError):
        AppSettings(min_free_disk_gb=1.0)
    store = SettingsStore(tmp_path)
    assert store.update(min_free_ram_mb=2048).min_free_ram_mb == 2048
    (tmp_path / "settings.json").write_text('{"min_free_ram_mb": 10}', "utf-8")
    assert store.load().min_free_ram_mb == 1024  # a tampered file falls back to the defaults


def test_a_real_snapshot_reads_this_pc(tmp_path: Path) -> None:
    taken = take_snapshot([tmp_path / "not-made-yet", tmp_path])
    assert taken.free_ram_mb > 0 and taken.total_ram_mb >= taken.free_ram_mb
    assert taken.free_disk_gb > 0 and taken.disk_path


# ---------- sleep watch ----------


def test_a_wall_clock_jump_is_a_wake_up_and_normal_ticks_are_not() -> None:
    times = iter([0, 5, 10, 15, 2415, 2420, 2425])
    woke: list[float] = []

    async def fake_sleep(_s: float) -> None:
        await anyio.sleep(0)

    async def go() -> None:
        with anyio.move_on_after(1):
            try:
                await watch_for_sleep(woke.append, clock=lambda: next(times), sleep=fake_sleep)
            except StopIteration:
                pass

    with pytest.raises(BaseException):  # noqa: B017 - the fake clock runs out
        anyio.run(go)
    assert woke == [2400]


# ---------- pause model ----------


class FakeBackend:
    def __init__(self) -> None:
        self.unloads = 0
        self.fail = False

    def unload(self) -> None:
        if self.fail:
            raise ConnectionRefusedError
        self.unloads += 1

    def runtime_info(self) -> dict[str, Any]:
        return {"version": "0.40", "loaded": [] if self.unloads else [{"size": 3 << 30}]}


def test_pause_refuses_new_inference_and_unloads_after_the_call_in_flight() -> None:
    started, release = threading.Event(), threading.Event()

    def slow_generate(*_a: Any, **_k: Any) -> GenerateResult:
        started.set()
        release.wait(5)
        return GenerateResult(text="done")

    queue = ModelQueue(slow_generate)
    backend = FakeBackend()
    control = OllamaModelControl(queue=queue, backend=backend)  # type: ignore[arg-type]
    assert control.status()["loaded"] is True

    in_flight: list[str] = []
    worker = threading.Thread(target=lambda: in_flight.append(queue.generate([]).text))
    worker.start()
    started.wait(5)
    control.pause()
    time.sleep(0.2)
    assert backend.unloads == 0  # waits for the model call in flight
    with pytest.raises(ModelPaused):
        queue.generate([])  # nothing new starts (it would load Qwen again)
    release.set()
    worker.join(5)
    deadline = time.monotonic() + 5
    while backend.unloads == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert in_flight == ["done"] and backend.unloads == 1
    assert control.status()["loaded"] is False and control.paused

    control.resume()
    assert queue.generate([]).text == "done"


def test_pause_with_ollama_not_running_still_pauses() -> None:
    backend = FakeBackend()
    backend.fail = True
    control = OllamaModelControl(queue=ModelQueue(lambda *a, **k: None), backend=backend)  # type: ignore[arg-type]
    control.pause(wait=True)
    assert control.paused and control.last_unload_error == "ConnectionRefusedError"


# ---------- health probes ----------


def test_a_slow_or_broken_probe_never_blocks_the_others() -> None:
    def slow() -> tuple[bool | None, str]:
        time.sleep(3)
        return True, "late"

    def broken() -> tuple[bool | None, str]:
        raise RuntimeError("secret detail")

    found = run_probes(
        {"fast": lambda: (True, "fine"), "slow": slow, "broken": broken}, timeout_s=0.5
    )
    assert (found["fast"].ok, found["fast"].value) == (True, "fine")
    assert (found["slow"].ok, found["slow"].value) == (None, "no answer in time")
    assert found["broken"].ok is None and "secret" not in found["broken"].value


def test_cached_probe_runs_at_most_once_per_ttl() -> None:
    calls: list[int] = []
    now = [0.0]
    probe = CachedProbe(lambda: (calls.append(1) or True, "x"), ttl_s=60, clock=lambda: now[0])
    probe()
    probe()
    now[0] = 61
    probe()
    assert len(calls) == 2
