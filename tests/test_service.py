import multiprocessing
import time
from pathlib import Path
from unittest.mock import Mock

from cache_service.database import create_database
from cache_service.schemas import PayloadInput
from cache_service.service import PayloadService


def create_in_process(data_dir: str, barrier, queue) -> None:
    directory = Path(data_dir)

    def transformer(value: str) -> str:
        with (directory / "calls.txt").open("a", encoding="utf-8") as file:
            file.write(value + "\n")
        time.sleep(0.02)
        return value.upper()

    barrier.wait(timeout=10)
    engine = create_database(directory, timeout=10)
    try:
        service = PayloadService(engine, directory, transformer, "uppercase-v1")
        barrier.wait(timeout=10)
        result = service.create(PayloadInput(list_1=["same", "other"], list_2=["same", "last"]))
        queue.put(result.model_dump())
    finally:
        engine.dispose()


def test_separate_processes_transform_each_cache_miss_once(tmp_path: Path) -> None:
    # Start with an empty directory so schema initialization also races.
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(target=create_in_process, args=(str(tmp_path), barrier, queue))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        results = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert len({result["id"] for result in results}) == 1
        assert sorted(result["reused"] for result in results) == [False, True]
        assert sorted((tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()) == [
            "last",
            "other",
            "same",
        ]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()
        queue.join_thread()


def test_transformer_version_invalidates_cached_results(tmp_path: Path) -> None:
    engine = create_database(tmp_path, timeout=10)
    request = PayloadInput(list_1=["hello"], list_2=["world"])
    try:
        old = PayloadService(engine, tmp_path, str.upper, "uppercase-v1").create(request)
        transformer = Mock(side_effect=str.lower)
        service = PayloadService(engine, tmp_path, transformer, "lowercase-v2")
        new = service.create(request)
        assert new.id != old.id
        assert service.read(new.id).output == "hello, world"
        assert service.read(old.id).output == "HELLO, WORLD"
        assert service.create(request).id == new.id
        assert transformer.call_count == 2
    finally:
        engine.dispose()


def test_many_distinct_strings_are_cached_in_batches(tmp_path: Path) -> None:
    engine = create_database(tmp_path, timeout=10)
    transformer = Mock(side_effect=str.upper)
    request = PayloadInput(
        list_1=[f"first-{index}" for index in range(600)],
        list_2=[f"second-{index}" for index in range(600)],
    )
    try:
        service = PayloadService(engine, tmp_path, transformer, "uppercase-v1")
        first = service.create(request)
        assert service.create(request).id == first.id
        assert transformer.call_count == 1_200
        assert service.read(first.id).output.startswith("FIRST-0, SECOND-0, FIRST-1, SECOND-1")
        assert service.read(first.id).output.endswith("FIRST-599, SECOND-599")
    finally:
        engine.dispose()
