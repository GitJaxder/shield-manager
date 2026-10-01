"""Background jobs, so long installs survive a phone going to sleep mid-request."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MAX_FINISHED_JOBS = 50

# A step's stage, in the order it moves through them.
QUEUED, DOWNLOADING, COPYING, INSTALLING, REMOVING, DONE, FAILED, SKIPPED = (
    "queued",
    "downloading",  # pulling the APK off the Shield that has it
    "copying",  # pushing it to the target Shield
    "installing",
    "removing",  # sync with prune: an app the reference doesn't have
    "done",
    "failed",
    "skipped",  # already up to date
)


@dataclass
class Step:
    """One app on one Shield, shown as a progress row on the page."""

    package: str
    label: str
    device: str
    source: str | None = None  # Shield the APK is copied from; None for an uploaded APK
    stage: str = QUEUED
    percent: float | None = None  # None while the stage has no measurable progress
    error: str | None = None
    store: bool = False  # failed because no copy fits; the Play Store page can install it
    download_page: str | None = None  # where to download a build that fits, by hand
    download_site: str | None = None  # "APKMirror" etc.; None when download_page is a search
    parts: str = ""  # for split APKs and bundles, e.g. "base + armeabi-v7a + xhdpi"
    remove: bool = False  # removes the app instead of installing it
    setting: str | None = None  # a settings sync step: the value it sets, e.g. "15 min"

    def update(self, stage: str, percent: float | None = None, error: str | None = None) -> None:
        self.stage, self.percent, self.error = stage, percent, error

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "label": self.label,
            "device": self.device,
            "source": self.source,
            "stage": self.stage,
            "percent": None if self.percent is None else round(self.percent, 1),
            "error": self.error,
            "store": self.store,
            "download_page": self.download_page,
            "download_site": self.download_site,
            "parts": self.parts,
            "remove": self.remove,
            "setting": self.setting,
        }


@dataclass
class Job:
    id: str
    title: str
    state: str = "running"  # running, done or failed
    progress: str = ""
    results: list[dict] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    error: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def add_step(self, package: str, label: str, device: str, source: str | None = None) -> Step:
        step = Step(package, label, device, source)
        self.steps.append(step)
        return step

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "progress": self.progress,
            "results": list(self.results),
            "steps": [step.to_dict() for step in list(self.steps)],
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
        }


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, title: str, work: Callable[[Job], None]) -> Job:
        job = Job(uuid.uuid4().hex[:12], title)
        with self._lock:
            self._jobs[job.id] = job
            self._trim()

        def run() -> None:
            try:
                work(job)
                failed = [r for r in job.results if not r.get("ok", True)]
                job.state = "failed" if failed and len(failed) == len(job.results) else "done"
            except Exception as e:
                job.state, job.error = "failed", str(e) or type(e).__name__
            finally:
                job.finished = time.time()

        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)

    def _trim(self) -> None:
        finished = sorted(
            (j for j in self._jobs.values() if j.finished), key=lambda j: j.finished or 0
        )
        for job in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
            del self._jobs[job.id]
