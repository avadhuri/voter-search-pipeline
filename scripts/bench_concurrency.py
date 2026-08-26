"""
Benchmark: grouped vs flat scoring at real serving occupancy.

scripts/bench_scoring.py measures one search at a time on an otherwise idle
machine, and every performance claim this project has made about grouped
scoring comes from it. That is not the regime the site runs in. Cloud Run
serves this app as ONE gunicorn process with --threads 4 behind
containerConcurrency: 4 and resources.limits.cpu: "4", so at full occupancy
four searches run concurrently *inside the same interpreter*, each fanning
out its own SCORING_WORKERS-sized pool -- up to 16 threads against 4 vCPU.

The grouped path's whole advantage is spare cores to fan out into. Where
there are none it is not a smaller win, it is overhead. Measured here, on
the deployed shape (--cpus=4, 5 ACs, 1.65M synthetic rows, best of 5,
throughput relative to flat):

    occupancy   grouped w=1   grouped w=2   grouped w=4
        1          0.93x         1.18x         1.50x
        2          0.93x         1.15x         1.19x
        4          0.83x         0.85x         0.78x

**The crossover sits between 2 and 4, i.e. at or below containerConcurrency.**
At full occupancy every grouped configuration loses to flat, and a user
waits ~1.3s longer for a 5-AC search (4.4s flat, 5.7s grouped w=4).

Two things that table settles, both of which cost more to re-derive than to
read:

**SCORING_WORKERS is not the lever.** Sizing it as cpu / containerConcurrency
-- the obvious fix, and the one this was commissioned to check -- lands on
w=1, which at occupancy 4 is 0.83x: still worse than flat. Splitting the
scoring into one cdist per constituency costs ~7% on its own (w=1 is 0.93x
even at occupancy 1, where nothing is contended), and the fan-out that pays
for it returns 1.61x at occupancy 1, 1.28x at occupancy 2 and 0.94x at
occupancy 4. Both halves are negative at the top end, so no pool size
recovers it.

**Nor is the GIL, at this scale.** Re-running with the searches overlapped
across forked processes instead of threads (--processes) isolates it: at
200K rows, flat throughput goes 7.82/s threaded to 11.53/s forked -- the GIL
costs ~a third. At the realistic 1.65M-row tier it goes 0.86/s to 0.93/s,
about 8%, because the GIL-holding half (building the candidate lists) is a
much smaller fraction of a much bigger job. So more gunicorn workers is not
the fix either, which is just as well: --workers 1 is deliberate (app.py's
once-per-instance background DB download), and forked numbers understate
memory anyway, since real workers would not stay copy-on-write.

What is left is choosing the path by observed in-process search concurrency
rather than fixing it at build time. Not done here -- this script measures,
it does not decide.

One caveat that keeps this from being read as "turn grouping off":
**occupancy here counts concurrent *searches*, not concurrent requests.**
containerConcurrency: 4 counts everything, and a single page load fans out
~30 requests, nearly all of them cheap. Four simultaneous searches is a peak
condition, not the mode; at 1-2 -- where the service spends most of its time
-- grouping still wins 1.2-1.5x.

Run it on an idle machine: it is measuring contention, so anything else
competing for cores is measuring something else. To reproduce the serving
quota rather than the dev machine's core count:

    docker run --rm --cpus=4 --memory=8g <image> \\
        python scripts/bench_concurrency.py --rows-per-ac 330000
"""
import argparse
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_scoring
from bench_scoring import score_grouped, score_new, synthetic_tier

# MAX_ACS in the app: the largest search a user can actually submit, and so
# the tier a fully-occupied instance is serving four of.
DEFAULT_ACS = 5
DEFAULT_OCCUPANCY = (1, 2, 4)
DEFAULT_SAMPLES = 5

NAME, RELATIVE = "Ravi Kumar", "Anand Sharma"


def _one_round(fn, concurrency):
    """Run `concurrency` searches that genuinely overlap, and report both
    what each caller waited and what the instance got through.

    The threads are held at a barrier and released together: staggered starts
    would let an early finisher hand its cores to a late starter, which is
    the low-occupancy regime wearing a high-occupancy label.
    """
    ready = threading.Barrier(concurrency)
    latencies = [0.0] * concurrency

    def run(i):
        ready.wait()
        t0 = time.perf_counter()
        fn()
        latencies[i] = (time.perf_counter() - t0) * 1000

    threads = [threading.Thread(target=run, args=(i,)) for i in range(concurrency)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = (time.perf_counter() - t0) * 1000
    return statistics.mean(latencies), wall


def _one_round_forked(fn, concurrency):
    """The same overlap, across processes instead of threads.

    Worth measuring because the deployed shape is `gunicorn --workers 1
    --threads 4`: concurrent searches share one interpreter, so the Python
    that builds each cdist's candidate arrays contends for a single GIL even
    when the machine has cores to spare. Forking after the rows exist keeps
    them copy-on-write, so a forked round differs from a threaded one in the
    GIL and little else -- which is what makes the pair an answer to "would
    more gunicorn workers help?" rather than two unrelated benchmarks.
    """
    t0 = time.perf_counter()
    pids = []
    for _ in range(concurrency):
        pid = os.fork()
        if pid == 0:
            try:
                fn()
            finally:
                os._exit(0)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)
    return (time.perf_counter() - t0) * 1000


def measure(fn, concurrency, samples, forked=False):
    """Best of `samples` rounds, on the minimum-of-N reasoning in
    tests/test_scoring_bench.py: contention from *outside* the experiment
    only ever adds time, so the fastest round is the least contaminated. The
    contention being measured is inside the round, and survives the minimum.
    """
    best = None
    for _ in range(samples):
        if forked:
            wall_ms = _one_round_forked(fn, concurrency)
            mean_ms = wall_ms  # they start together, so each waited the round
        else:
            mean_ms, wall_ms = _one_round(fn, concurrency)
        if best is None or wall_ms < best[1]:
            best = (mean_ms, wall_ms)
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--acs", type=int, default=DEFAULT_ACS)
    ap.add_argument("--rows-per-ac", type=int, default=bench_scoring.SYNTHETIC_ROWS_PER_AC)
    ap.add_argument("--occupancy", default=",".join(str(c) for c in DEFAULT_OCCUPANCY))
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    ap.add_argument("--processes", action="store_true",
                    help="overlap searches across forked processes instead of "
                         "threads, isolating the GIL's contribution")
    ap.add_argument("--workers", default=None,
                    help="comma list of SCORING_WORKERS values to sweep "
                         "(default: 1, 2, 4 and os.cpu_count())")
    args = ap.parse_args()

    cpus = os.cpu_count() or 1
    sweep = ([int(w) for w in args.workers.split(",")] if args.workers
             else sorted({1, 2, 4, cpus}))

    bench_scoring.SYNTHETIC_ROWS_PER_AC = args.rows_per_ac
    rows = synthetic_tier(args.acs)
    print(f"cpu_count={cpus} acs={args.acs} rows={len(rows)} samples={args.samples} "
          f"overlap={'processes' if args.processes else 'threads'}")
    print(f"{'occupancy':>9}  {'mode':<14} {'latency':>9} {'throughput':>11} {'vs flat':>8}")

    for concurrency in (int(c) for c in args.occupancy.split(",")):
        flat_lat, flat_wall = measure(
            lambda: score_new(rows, NAME, RELATIVE, "wratio"),
            concurrency, args.samples, args.processes)
        flat_thru = concurrency / (flat_wall / 1000)
        print(f"{concurrency:>9}  {'flat':<14} {flat_lat:>8.0f}m {flat_thru:>10.2f}/s "
              f"{1.0:>7.2f}x")
        for workers in sweep:
            lat, wall = measure(
                lambda: score_grouped(rows, NAME, RELATIVE, "wratio", max_workers=workers),
                concurrency, args.samples, args.processes)
            thru = concurrency / (wall / 1000)
            print(f"{concurrency:>9}  {'grouped w=%d' % workers:<14} {lat:>8.0f}m "
                  f"{thru:>10.2f}/s {thru / flat_thru:>7.2f}x")


if __name__ == "__main__":
    main()
