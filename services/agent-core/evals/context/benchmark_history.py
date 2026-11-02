"""Offline history scaling benchmark; deliberately outside ordinary pytest runs."""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import statistics
import tempfile
import threading
import time
from pathlib import Path

from agent.context_engine.history import reconstruct_turns


def fixture(rounds):
    messages, runs = [], []
    for index in range(rounds):
        user, assistant = f"u{index}", f"a{index}"
        messages.extend([
            dict(id=user, role="user", content=f"question {index}", sequence_no=2 * index + 1),
            dict(id=assistant, role="assistant", content=f"answer {index}", sequence_no=2 * index + 2),
        ])
        runs.append(dict(id=f"r{index}", user_message_id=user, assistant_message_id=assistant,
                         status="completed", finished_at="2026"))
    return messages, runs


async def sample(messages, runs):
    # Exercise the runtime's pure-work scheduler when available; baseline runs
    # execute synchronously, as the pre-C4 runtime did. Count input rows, not SQL
    # page visits. The database tail-count regression is in the test suite.
    from agent.context_engine import service
    lags = []
    running = True

    async def ping():
        while running:
            started = time.perf_counter()
            await asyncio.sleep(.005)
            lags.append(max(0, (time.perf_counter() - started - .005) * 1000))

    ticker = asyncio.create_task(ping())
    await asyncio.sleep(0)
    start = time.perf_counter()
    if hasattr(service, "run_context_work"):
        turns = await service.run_context_work(reconstruct_turns, messages, runs)
    else:
        turns = reconstruct_turns(messages, runs)
    elapsed = (time.perf_counter() - start) * 1000
    running = False
    await ticker
    assert len(turns) == len(runs) and all(turn.complete for turn in turns)
    return dict(rows_read=len(messages) + len(runs), messages_processed=len(messages),
                elapsed_ms=elapsed, event_loop_lag_ms=max(lags, default=0))


async def main(args):
    previous = None
    for rounds in args.rounds:
        messages, runs = fixture(rounds)
        samples = [await sample(messages, runs) for _ in range(args.repeat)]
        median = statistics.median(item["elapsed_ms"] for item in samples)
        print(json.dumps(dict(rounds=rounds, samples=samples, median_ms=median,
                              doubling_ratio=median / previous if previous else None)), flush=True)
        previous = median
    if args.assembly:
        from agent.context_engine.service import ContextEngineService, run_context_work
        from agent.storage import CoreRepository
        rounds = max(args.rounds)
        messages, runs = fixture(rounds)
        with tempfile.TemporaryDirectory(prefix="context-benchmark-") as directory:
            repository = CoreRepository(Path(directory) / "history.db")
            conversation = repository.create_conversation("standard_chat", "Synthetic benchmark", "casual")
            with repository._connect() as connection:
                connection.executemany(
                    "INSERT INTO messages(id,conversation_id,role,content,created_at,sequence_no) VALUES(?,?,?,?,?,?)",
                    [(m["id"], conversation["id"], m["role"], m["content"], "2026", m["sequence_no"]) for m in messages],
                )
                connection.executemany(
                    """INSERT INTO model_runs(id,conversation_id,user_message_id,assistant_message_id,status,
                       provider_profile_id,model_id,created_at,finished_at) VALUES(?,?,?,?,'completed','builtin-demo','demo','2026','2026')""",
                    [(r["id"], conversation["id"], r["user_message_id"], r["assistant_message_id"]) for r in runs],
                )
            connection.close()
            run = repository.start_model_run(conversation_id=conversation["id"], content="current question",
                idempotency_key="current", provider_profile_id="builtin-demo", model_id="demo")
            service = ContextEngineService(repository)
            counts = service.context_repository.load_context_sources(run["id"])["performance"]
            samples, lags = [], []
            running = True

            async def ping():
                while running:
                    start = time.perf_counter()
                    await asyncio.sleep(.005)
                    lags.append(max(0, (time.perf_counter() - start - .005) * 1000))

            ticker = asyncio.create_task(ping())
            for _ in range(args.repeat):
                start = time.perf_counter()
                packed = await service.assemble_async(run, context_window=2_000_000)
                samples.append((time.perf_counter() - start) * 1000)
                assert len(packed.messages) >= 2 * rounds
            running = False
            await ticker
            print(json.dumps(dict(kind="assembly", rounds=rounds, rows_read=counts["rows_read"],
                messages_processed=counts["messages_processed"], elapsed_ms=samples,
                median_ms=statistics.median(samples), event_loop_lag_ms=lags,
                event_loop_lag_p95_ms=percentile(lags, .95))), flush=True)

            # Actual Python history CPU work, occupying both worker slots. A
            # cancellation abandons the result while those workers finish.
            cancellations, lags = [], []
            running = True
            ticker = asyncio.create_task(ping())
            for _ in range(args.repeat):
                entered = threading.Event()
                def cpu_work():
                    entered.set()
                    end = time.perf_counter() + .25
                    while time.perf_counter() < end:
                        reconstruct_turns(messages, runs)
                first = asyncio.create_task(run_context_work(cpu_work))
                second = asyncio.create_task(run_context_work(cpu_work))
                while not entered.is_set():
                    await asyncio.sleep(.001)
                start = time.perf_counter()
                first.cancel()
                try:
                    await first
                except asyncio.CancelledError:
                    pass
                cancellations.append((time.perf_counter() - start) * 1000)
                await second
            running = False
            await ticker
            print(json.dumps(dict(kind="cpu_stress", cancellation_ms=cancellations,
                cancellation_p95_ms=percentile(cancellations, .95), event_loop_lag_ms=lags,
                event_loop_lag_p95_ms=percentile(lags, .95))), flush=True)
            gc.collect()  # Release legacy repository read connections on Windows.


def percentile(values, fraction):
    return sorted(values)[min(len(values) - 1, int(len(values) * fraction))] if values else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", nargs="+", type=int, default=[1000, 2000, 4000, 8000])
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--assembly", action="store_true", help="Also measure synthetic SQLite assembly and CPU cancellation")
    asyncio.run(main(parser.parse_args()))
