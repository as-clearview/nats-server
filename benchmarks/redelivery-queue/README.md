# Redelivery-queue contention experiment

Reproduction material for [fork draft PR #2](https://github.com/as-clearview/nats-server/pull/2).
This branch contains only benchmark artifacts; the production fix and deterministic tests stay in the PR.
The large harness is stored as `.go.txt` so ordinary `go test` / CI cannot accidentally run it.
No production data, credentials, or endpoints are needed.

The proposed change advances the redelivery slice head rather than copying all remaining entries:

```diff
- o.rdq = append(o.rdq[:0], o.rdq[1:]...)
+ o.rdq = o.rdq[1:]
```

A dequeue from a large queue otherwise copies O(n) entries while holding the consumer lock.
Draining that queue becomes quadratic, interfering with delivery and ACK processing.
This experiment targets that contention, not ACK scheduling or consumer-store flushing changes.

## Reproduce

Requires Python **3.12+** and Docker. From the root of this benchmark branch:

```sh
# Smoke: 10,000 entries, 2-second offered workload, one baseline/fix pair.
python3 benchmarks/redelivery-queue/run.py

# Large comparison: 12 million entries, 20-second workload, two runs per variant.
# Also reproduce the PR's isolated dequeue/requeue microbenchmark (three samples per size).
python3 benchmarks/redelivery-queue/run.py --full --microbench
```

The large comparison is expensive: each container is limited to **4 CPUs / 12 GiB RAM**,
with swap disabled. Give Docker at least that memory plus host/VM headroom. Setup inserts
12 million file-backed messages per run, so expect substantial disk I/O, temporary disk use,
and minutes of runtime, in addition to first-time compilation/downloads. Avoid concurrent
benchmarks or heavy host workloads. Smoke uses the same container limits but much less memory.
The runner does not contact or alter any existing NATS server.

Options:

- `--repetitions N`: override the number of pairs; order alternates baseline/fix, then fix/baseline.
- `--microbench`: run the PR's queue benchmark after the protocol tests; also works with smoke.
- `--output PATH`: create a new result directory; refuses an existing directory.
- `--cache-dir PATH`: reuse Go module/build caches (subdirectories `go/` and `go-build/`).

Default artifacts and caches live in the gitignored `benchmarks/redelivery-queue/out/`.
Raw test output and a machine-readable `summary.json` are retained. The summary records
actual offered counts, latency/backlog samples, exact source revisions and archive hashes,
harness hashes, builder image, architecture, and resource settings. Failed tests fail the runner;
inspect that run's logs. The runner removes containers on completion, failure, timeout, Ctrl-C,
or SIGTERM. Uncatchable termination (SIGKILL) or Docker-daemon failure can prevent cleanup;
look for containers named `nats-rdq-bench-*` in that case.
The output directory retains source archives/trees and logs; caches are reusable.

### Pinned inputs and isolation

| Input | Revision |
| --- | --- |
| Upstream baseline | `edb1b17a76f149d501131502b847939a14ce300d` |
| Candidate in `as-clearview/nats-server` | `e85c332f4f5b24d3c8a87606d5ea19095c16fc6c` |
| Go image | `golang:1.26.7-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514` |

The runner downloads public GitHub archives at these immutable SHAs, copies the **same**
protocol harness/helper into both trees, and prepares Go dependencies. These setup steps
require network access (GitHub, the image registry, and Go module services). Measured
containers use `--network=none`; only loopback connections inside each container are used.
Only the extracted source and Go caches are mounted, not host credential directories.
`GOTOOLCHAIN=local` prevents automatic toolchain upgrades; `GOMAXPROCS=4` makes the CPU
setting explicit. The image's native platform is recorded, not forced: original measurements
were Linux/arm64. Other architectures may give different results.

For the optional microbenchmark, the runner copies the candidate's
`server/consumer_redelivery_queue_test.go` into the baseline tree and runs:

```sh
go test ./server -run '^$' -bench '^BenchmarkConsumerRedeliveryQueue' -benchmem -count=3 -timeout=10m
```

Protocol invocation inside each container:

```sh
env NATS_FLUSH_PROTOCOL=1 NATS_FLUSH_PENDING=12000000 NATS_FLUSH_DURATION=20s NATS_FLUSH_REDELIVERY=1 \
  go test ./server -run '^TestConsumerFlushProtocolExperiment$' -count=1 -v -timeout=10m
```

## Workload and what the metrics mean

- Starts a real local JetStream server with a **file-backed WorkQueue stream** of synthetic messages.
- Publishes the messages, then internally seeds matching consumer pending/delivery state and an
  expired redelivery population. This skips accumulation time: it is **not an end-to-end incident replay**.
- Uses a one-hour `AckWait` so new expiration cycles do not confound the measured interval.
- Issues real pull requests in batches of 100 while offering a target **20,000 progress commands/s**
  and **100 final ACKs/s**. Progress ACKs cover a rotating subset; final ACKs address a different subset.
- Final ACKs carry reply subjects. Reported latency runs from client submission timestamp to
  **server confirmation**, not merely completion of a client send.
- After stopping the load, waits for the ACK work to drain, verifies every final ACK confirmation,
  and checks the expected retained stream-message count.

The paced generator can miss ticks under pressure: use `progress_sent`, `final_sent`, and
actual duration, not just target rates. `offered_seconds` includes pull shutdown and the final
client flush. `acks_unprocessed_*` samples the internal `o.awl` counter, which includes ACK
processing work, **not just the number of queue entries**, and is not the count of input messages
awaiting acknowledgement. `delivered_at_stop` counts messages returned by pulls, not successful
application processing. The harness counts returned messages but does not classify Fetch errors;
that limitation is preserved to retain the measured harness. The small smoke case can exhaust
its redelivery queue quickly and is only a correctness/setup check.

## Public-runner validation

The unchanged runner from benchmark commit `eede1dde8e79b79d2aae05b9e7a7f84a60ff692f`
completed `--full --microbench` against the exact pinned baseline and candidate commits.
[Raw logs and structured results](results/runner-validation/) preserve the complete run;
[file hashes](results/runner-validation/sha256.json) cover the recorded artifacts.

| Two runs per variant, 12 million entries | Baseline | Dequeue fix |
| --- | ---: | ---: |
| Final-ACK confirmation p95 | 12.06–12.12 s | 0.25–1.44 s |
| Redeliveries during ~20 s | 2,298–2,369 | 120,700–416,000 |
| Unprocessed ACK commands at stop | 223,714–226,528 | 2,614–26,533 |
| Drain after stopping load | 11.93–12.26 s | 0.10–1.54 s |

All four protocol tests confirmed every final ACK and verified the expected retained-message
count. All 24 microbenchmark samples completed (four sizes, three repetitions, two variants).
The sizable variation between candidate runs remains important; these are ranges from a
small local sample, not confidence intervals or production guarantees.

An earlier attempt encountered ACK-drain timeout and Docker storage I/O errors with the host
nearly out of disk space. It is excluded as invalid/incomplete. The run above was started fresh
after freeing disk space, restarting Docker, and verifying storage writes; it does not combine
samples from the failed attempt. Benchmarks ran separately from correctness tests.

### Expanded correctness validation

On candidate `e85c332f4f5b24d3c8a87606d5ea19095c16fc6c`, **207 top-level tests passed with
`-race` in 207.871 seconds**, including the full `TestJetStreamConsumer` selection, queue tests,
consumer-store tests, and two read-only-filesystem permission regressions.
[Verbose test output](results/runner-validation/consumer-race.log).

```sh
go test -race ./server \
  -run '^(TestConsumerRedeliveryQueue.*|TestFileStoreConsumer.*|TestJetStreamConsumer.*|TestJetStreamRedeliverAndLateAck|TestJetStreamCanNotNakAckd|TestFileStore.*PermissionErrorIfFSModeReadOnly)$' \
  -count=1 -v -timeout=15m
```

This used the same pinned Go image, Linux/arm64, 4 CPUs, 12 GiB, `GOTOOLCHAIN=local`,
`GOMAXPROCS=4`, and no container network. Docker options
`--cap-drop=DAC_OVERRIDE --cap-drop=DAC_READ_SEARCH` prevent root from bypassing the
filesystem permissions those two tests exercise. Both passed after an earlier root-container
server-suite attempt stopped at a permission assertion (531 top-level tests had passed).

A full `go test ./...` attempt also stopped in logger tests because the image lacks a syslog
service. **The full repository suite and upstream CI matrix have not completed successfully.**
The expanded race selection is not presented as a substitute for that matrix.

## Historical evidence

[Machine-readable results and provenance](results/summary.json) include the raw output, hashes,
and per-second samples. These are **historical local measurements**, not reruns made by this
new public runner. Baseline and candidate were measured from retained source trees at the
upstream base, with the one-line fix applied for the candidate; the fork candidate commit above
is an equivalent patch, not an asserted checkout used by the historical experiment.

Environment: Linux/arm64, Go 1.26.7, 4 CPUs, 12 GiB container memory. The protocol harness
matches retained experiment copies, apart from formatting in one baseline copy. The helper
was extracted from the original larger local test file; unrelated experiments are excluded.
Hashes establish retained-file provenance, not execution-time attestation.

### Protocol load: upstream main, 12 million entries

Ranges across **two samples per variant**, not confidence intervals:

| Metric | Baseline | Dequeue fix |
| --- | ---: | ---: |
| Final-ACK confirmation p95 | 12.22–17.21 s | 0.67–1.59 s |
| Redeliveries during ~20 s | 1,831–2,324 | 154,200–308,400 |
| Unprocessed ACK commands at stop | 223,915–262,708 | 9,046–28,342 |
| Drain after stopping load | 11.85–17.09 s | 0.41–1.52 s |

Raw logs: [baseline 1](results/main-baseline-protocol-12m-run1.log),
[baseline 2](results/main-baseline-protocol-12m-run2.log),
[fix 1](results/main-dequeue-protocol-12m-run1.log),
[fix 2](results/main-dequeue-protocol-12m-run2.log).

All final ACKs were confirmed after draining. The fix still has a residual backlog under this
extreme workload; it does not guarantee acceptable latency at arbitrary load.

### Isolated queue churn

Three samples per size, continuously dequeueing and requeueing one entry:

| Queue size | Baseline time/op | Fix time/op | Fix's extra allocation/op |
| --- | ---: | ---: | ---: |
| 1 | 74–80 ns | 74–75 ns | unchanged |
| 64 | ~16.8 ns | 12.4–12.6 ns | ~15 B |
| 4,096 | 322–355 ns | 17.2–18.0 ns | ~24 B |
| 1,000,000 | ~125 µs | 50–52 ns | ~40–41 B |

Logs: [baseline](results/main-baseline-queue-bench.log),
[fix](results/main-dequeue-queue-bench.log).

**Trade-off:** advancing the head consumes slice capacity. Sustained dequeue/requeue churn
occasionally reallocates. Integer `allocs/op` can round down; these results do not mean zero
allocations. No ACK semantics or persistence changes are proposed.

### Supporting stock release experiment

One additional pair used stock v2.14.6 (`1aa10f9fe4e7a27b7d877af004a9c0022fdc4910`), with the
same dequeue change applied to its candidate:

| Metric | Baseline | Dequeue fix |
| --- | ---: | ---: |
| Final-ACK confirmation p95 | 11.87 s | 1.01 s |
| Redeliveries | 2,284 | 262,300 |
| Unprocessed ACK commands at stop | 225,322 | 19,900 |
| Drain | 11.09 s | 0.92 s |

Logs: [baseline](results/stock-v2.14.6-baseline-protocol-12m-run1.log),
[fix](results/stock-v2.14.6-dequeue-protocol-12m-run1.log).
The public runner pins the main comparison; these release logs are supplementary historical
evidence. The production-specific patched release build was **not tested**. No results here
establish production throughput or multi-node/cluster behavior.

## Review status

AI-assisted benchmark and implementation; human review and DCO sign-off remain before an
upstream submission. No upstream PR or default CI integration is created by this branch.
