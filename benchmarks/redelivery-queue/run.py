#!/usr/bin/env python3
"""Opt-in, synthetic local NATS comparison; requires Python 3.12+ and Docker."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import signal
import subprocess
import sys
import tarfile
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType

BASE = "edb1b17a76f149d501131502b847939a14ce300d"
FIX = "e85c332f4f5b24d3c8a87606d5ea19095c16fc6c"
IMAGE = "golang:1.26.7-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514"
HERE = Path(__file__).resolve().parent
REVISIONS = {"baseline": ("nats-io", BASE), "dequeue": ("as-clearview", FIX)}


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def checked(command: list[str], log: Path, timeout: int) -> None:
    print(f"Running {log.name}", flush=True)
    with log.open("w") as output:
        subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def container(source: Path, cache: Path, log: Path, args: list[str], *, offline: bool,
              timeout: int) -> None:
    name = "nats-rdq-bench-" + uuid.uuid4().hex[:12]
    command = [
        "docker", "run", "--rm", "--name", name, "--cpus=4", "--memory=12g", "--memory-swap=12g",
        "--mount", f"type=bind,src={source},dst=/src", "--workdir", "/src",
        "--mount", f"type=bind,src={cache / 'go'},dst=/go",
        "--mount", f"type=bind,src={cache / 'go-build'},dst=/root/.cache/go-build",
        "--env", "GOTOOLCHAIN=local", "--env", "GOMAXPROCS=4",
    ]
    if offline:
        command += ["--network=none", "--pull=never"]
    try:
        checked(command + [IMAGE] + args, log, timeout)
    finally:
        # Also stop a container whose Docker CLI was interrupted or timed out.
        subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=False)


def source_tree(owner: str, revision: str, directory: Path) -> tuple[Path, str]:
    archive = directory / "source.tar.gz"
    url = f"https://codeload.github.com/{owner}/nats-server/tar.gz/{revision}"
    print(f"Downloading {owner}/nats-server at {revision}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(directory, filter="data")
    tree = directory / f"nats-server-{revision}"
    if not (tree / "go.mod").is_file():
        raise RuntimeError("Source archive did not contain the expected tree")
    return tree, sha256(archive)


def protocol_result(log: Path) -> dict[str, object]:
    lines = [line.removeprefix("PROTOCOL_RESULT ") for line in log.read_text().splitlines()
             if line.startswith("PROTOCOL_RESULT ")]
    if len(lines) != 1:
        raise RuntimeError(f"Expected one protocol result in {log.name}")
    result = json.loads(lines[0])
    if result["final_sent"] != result["final_confirmed"]:
        raise RuntimeError(f"Unconfirmed final ACKs in {log.name}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="12m entries, 20s, two repetitions (expensive)")
    parser.add_argument("--repetitions", type=int, help="Override smoke=1 / full=2 repetitions")
    parser.add_argument("--microbench", action="store_true", help="Also run the PR's queue benchmark three times")
    parser.add_argument("--output", type=Path, help="New output directory (must not already exist)")
    parser.add_argument("--cache-dir", type=Path, default=HERE / "out" / "cache")
    options = parser.parse_args()
    repetitions = options.repetitions if options.repetitions is not None else (2 if options.full else 1)
    if repetitions < 1:
        parser.error("--repetitions must be positive")
    pending, duration = (12000000, "20s") if options.full else (10000, "2s")
    output = (options.output or HERE / "out" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    cache = options.cache_dir.resolve()
    for folder in (cache / "go", cache / "go-build"):
        folder.mkdir(parents=True, exist_ok=True)
    harnesses = sorted(HERE.glob("*.go.txt"))
    metadata: dict[str, object] = {
        "created_utc": datetime.now(timezone.utc).isoformat(), "image": IMAGE,
        "cpus": 4, "memory_bytes": 12 * 1024**3, "gomaxprocs": 4,
        "pending_seed": pending, "duration": duration, "repetitions": repetitions,
        "harness_sha256": {file.name: sha256(file) for file in harnesses},
        "sources": {}, "runs": [],
    }
    sources: dict[str, Path] = {}
    for variant, (owner, revision) in REVISIONS.items():
        directory = output / variant
        directory.mkdir()
        tree, archive_hash = source_tree(owner, revision, directory)
        sources[variant] = tree
        for harness in harnesses:
            shutil.copyfile(harness, tree / "server" / harness.name.removesuffix(".txt"))
        metadata["sources"][variant] = {"repository": f"{owner}/nats-server", "revision": revision,
                                        "archive_sha256": archive_hash}
    if options.microbench:
        queue_test = sources["dequeue"] / "server" / "consumer_redelivery_queue_test.go"
        shutil.copyfile(queue_test, sources["baseline"] / "server" / queue_test.name)
        metadata["queue_test_sha256"] = sha256(queue_test)
    for variant, tree in sources.items():
        container(tree, cache, output / f"{variant}-dependencies.log", ["go", "mod", "download"],
                  offline=False, timeout=600)
    metadata["platform"] = subprocess.check_output(
        ["docker", "image", "inspect", IMAGE, "--format", "{{.Os}}/{{.Architecture}}"], text=True).strip()
    metadata_file = output / "summary.json"
    metadata_file.write_text(json.dumps(metadata, indent=2) + "\n")
    for repetition in range(1, repetitions + 1):
        variants = list(sources) if repetition % 2 else list(reversed(sources))
        for variant in variants:
            log = output / f"{variant}-protocol-run{repetition}.log"
            container(sources[variant], cache, log, [
                "env", "NATS_FLUSH_PROTOCOL=1", f"NATS_FLUSH_PENDING={pending}",
                f"NATS_FLUSH_DURATION={duration}", "NATS_FLUSH_REDELIVERY=1",
                "go", "test", "./server", "-run", "^TestConsumerFlushProtocolExperiment$",
                "-count=1", "-v", "-timeout=10m",
            ], offline=True, timeout=900)
            metadata["runs"].append({"variant": variant, "repetition": repetition, "log": log.name,
                                     "log_sha256": sha256(log), "result": protocol_result(log)})
            metadata_file.write_text(json.dumps(metadata, indent=2) + "\n")
            print(json.dumps(metadata["runs"][-1]["result"], sort_keys=True), flush=True)
    if options.microbench:
        for variant, tree in sources.items():
            container(tree, cache, output / f"{variant}-queue-bench.log", [
                "go", "test", "./server", "-run", "^$", "-bench", "^BenchmarkConsumerRedeliveryQueue",
                "-benchmem", "-count=3", "-timeout=10m",
            ], offline=True, timeout=900)
    print(f"Results: {metadata_file}")


def terminate(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    if sys.version_info < (3, 12):
        sys.exit("Python 3.12+ is required for safe archive extraction")
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f"Benchmark failed: {error}. Inspect the output directory's logs.")
