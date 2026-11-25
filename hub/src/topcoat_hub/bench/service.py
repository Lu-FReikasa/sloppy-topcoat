"""The bench (PRG-00 R6): a set of suites goes through one model on one node, scored by rules.

A run is started by an admin, goes through the gateway at the last place of the queue (a person's
request always comes first; the pause of the hub's chores does not stop it, the admin asked for
it), never starts on a node that belongs to a game (the gateway refuses those), can be stopped, and
stores one rating row per finished suite. A node serves one run at a time. Only models of the pool
can be rated (the gateway routes the pool only)."""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.bench import scoring, suites, variants
from topcoat_hub.config import Settings
from topcoat_hub.gateway import PRIORITY_IDLE, Gateway, GatewayError, interactive_work
from topcoat_hub.models import ModelRating

log = logging.getLogger(__name__)

KEEP_FINISHED_SECONDS = 3600.0
TEMPERATURE = 0.2
# a node that is gone, busy with a game or without the model ends the run (a case that only got a
# bad answer does not)
ABORTING = {
    "node_not_found",
    "model_not_found",
    "model_unavailable",
    "background_paused",
    "node_kept_free",
    "node_holds_other_model",
    "load_failed",
    "unload_failed",
    "timeout",
}


class BenchError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class LiveBench:
    run_id: uuid.UUID
    user_id: uuid.UUID
    model: str
    node: str
    suites: list[str]
    variant: str = variants.DEFAULT
    status: str = "running"  # running | done | stopped | failed
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    task: "asyncio.Task[Any] | None" = None

    @property
    def finished(self) -> bool:
        return self.task is not None and self.task.done()

    def view(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "model": self.model,
            "node": self.node,
            "suites": self.suites,
            "variant": self.variant,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "progress": self.progress,
            "results": self.results,
            "error": self.error,
        }


def _text_of(data: dict[str, Any]) -> tuple[str, int, str]:
    """The answer, the tokens it took and why the model stopped."""
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else None
    text = message.get("content") if isinstance(message, dict) else None
    tokens = (data.get("usage") or {}).get("completion_tokens")
    finish = str(first.get("finish_reason") or "") if isinstance(first, dict) else ""
    return (text if isinstance(text, str) else ""), int(tokens or 0), finish


async def latest_ratings(sessionmaker: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    """The newest rating of every model, node and suite."""
    async with sessionmaker() as db:
        newest = select(func.max(ModelRating.id)).group_by(
            ModelRating.model, ModelRating.node, ModelRating.suite, ModelRating.variant
        )
        rows = (
            await db.scalars(
                select(ModelRating)
                .where(ModelRating.id.in_(newest))
                .order_by(
                    ModelRating.model, ModelRating.node, ModelRating.suite, ModelRating.variant
                )
            )
        ).all()
    current = {k: s["version"] for k, s in suites.SUITES.items()}
    return [
        {
            "model": r.model,
            "node": r.node,
            "suite": r.suite,
            "variant": r.variant,
            "function": r.function_kind,
            "version": r.suite_version,
            # a rating of an older version of the suite is shown but not used to rank
            "current": current.get(r.suite) == r.suite_version,
            "score": round(r.score, 3),
            "passed": r.passed,
            "total": r.total,
            "tps": round(r.tps, 1) if r.tps else None,
            "failed": r.failed,
            "at": r.created_at.isoformat() if r.created_at else None,
            "run_id": str(r.run_id),
        }
        for r in rows
    ]


class BenchService:
    def __init__(
        self,
        gateway: Gateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.gateway, self.sessionmaker, self.settings = gateway, sessionmaker, settings
        self._runs: dict[uuid.UUID, LiveBench] = {}

    def get(self, run_id: uuid.UUID) -> LiveBench | None:
        return self._runs.get(run_id)

    def runs(self) -> list[LiveBench]:
        return sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)

    async def start(
        self,
        user_id: uuid.UUID,
        model: str,
        node: str,
        keys: list[str],
        variant: str = variants.DEFAULT,
    ) -> LiveBench:
        unknown = [k for k in keys if k not in suites.SUITES]
        if not keys or unknown:
            raise BenchError(
                422, "unknown_suite", f"suites: {', '.join(suites.SUITES)} (not: {unknown or '-'})"
            )
        if variant not in variants.VARIANTS:
            raise BenchError(
                422,
                "unknown_variant",
                f"variants: {', '.join(n or 'default' for n in variants.VARIANTS)}",
            )
        views = {v.name: v for v in await self.gateway.node_views()}  # the pool only
        view = views.get(node)
        if view is None or not view.connected:
            raise BenchError(409, "node_unavailable", f"{node} is not connected")
        if model not in view.models:
            raise BenchError(
                409,
                "model_not_in_pool",
                f"{model} is not in the pool of {node}: switch it on first",
            )
        if any(r.node == node and not r.finished for r in self._runs.values()):
            raise BenchError(409, "node_busy", f"a bench is already running on {node}")
        run = LiveBench(uuid.uuid4(), user_id, model, node, list(dict.fromkeys(keys)), variant)
        self._runs[run.run_id] = run

        async def drive() -> None:
            try:
                # a person started it: the pause of the hub's chores does not stop it, but it waits
                # behind every real request (the last place of the queue)
                with interactive_work(PRIORITY_IDLE):
                    await self._drive(run)
            except asyncio.CancelledError:
                run.status = "stopped"
            except Exception as exc:  # a bug; the run ends visibly instead of silently
                log.exception("bench: run %s crashed", run.run_id)
                run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"
            finally:
                run.finished_at = datetime.now(UTC)
                asyncio.get_running_loop().call_later(
                    KEEP_FINISHED_SECONDS, self._runs.pop, run.run_id, None
                )

        run.task = asyncio.ensure_future(drive())
        return run

    async def stop(self, run_id: uuid.UUID, wait: float = 10.0) -> bool:
        run = self._runs.get(run_id)
        if run is None or run.finished or run.task is None:
            return False
        run.task.cancel()
        await asyncio.wait({run.task}, timeout=wait)
        return True

    async def shutdown(self, grace: float = 5.0) -> None:
        tasks = [r.task for r in self._runs.values() if r.task is not None and not r.task.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=grace)

    # --- the run -----------------------------------------------------------------------

    async def _drive(self, run: LiveBench) -> None:
        pinned = f"{run.model}@{run.node}"
        try:
            await self.gateway.unload_others(
                run.node, run.model
            )  # a fair measure: alone on the card
        except GatewayError as exc:
            run.status, run.error = "failed", exc.message
            return
        for key in run.suites:
            outcome = await self._suite(run, pinned, key)
            if outcome is None:
                return  # the run ended (error set)
            run.results.append(outcome)
            await self._store(run, key, outcome)
        run.status = "done"

    async def _suite(self, run: LiveBench, pinned: str, key: str) -> dict[str, Any] | None:
        suite = suites.SUITES[key]
        cases = suite["cases"]
        passed, tokens, seconds = 0, 0, 0.0
        failed: list[dict[str, Any]] = []
        for index, case in enumerate(cases, start=1):
            run.progress = {"suite": key, "case": index, "of": len(cases)}
            messages = (
                [{"role": "system", "content": case["system"]}] if case.get("system") else []
            ) + [{"role": "user", "content": case["question"]}]
            payload = {
                "temperature": TEMPERATURE,
                "max_tokens": suite["max_tokens"],
                "messages": messages,
                **variants.VARIANTS[run.variant],  # one setting changed, the rest as every day
            }
            began = time.monotonic()
            try:
                data = await self.gateway.call_streamed(
                    pinned, "/v1/chat/completions", payload, label="bench"
                )
            except GatewayError as exc:
                if exc.code in ABORTING:
                    run.status, run.error = "failed", exc.message
                    return None
                failed.append({"case": case["id"], "why": [f"error:{exc.code}"]})
                continue
            spent = time.monotonic() - began
            text, generated, finish = _text_of(data)
            graded = scoring.score_case(text, case["checks"])
            if graded.passed:
                passed += 1
            else:
                # the start of the answer is kept: a failure is judged by reading what was said
                got = text.strip()[:160] or f"[empty: finish {finish or '?'}, {generated} tokens]"
                failed.append({"case": case["id"], "why": graded.failed[:4], "got": got})
            if generated >= 30 and spent >= 0.5:  # shorter answers are mostly overhead
                tokens += generated
                seconds += spent
        return {
            "suite": key,
            "function": suite["function"],
            "version": suite["version"],
            "score": round(passed / len(cases), 3),
            "passed": passed,
            "total": len(cases),
            "tps": round(tokens / seconds, 1) if seconds > 0 else None,
            "failed": failed,
        }

    async def _store(self, run: LiveBench, key: str, outcome: dict[str, Any]) -> None:
        try:
            async with self.sessionmaker() as db:
                db.add(
                    ModelRating(
                        run_id=run.run_id,
                        model=run.model,
                        node=run.node,
                        suite=key,
                        variant=run.variant,
                        function_kind=outcome["function"],
                        suite_version=outcome["version"],
                        score=outcome["score"],
                        passed=outcome["passed"],
                        total=outcome["total"],
                        tps=outcome["tps"],
                        failed=outcome["failed"],
                    )
                )
                await db.commit()
        except Exception:  # the result stays in the run; a failed write breaks nothing else
            log.exception("bench: could not store the rating of %s", key)
