import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from bench_scoring import score_grouped, score_new, score_old, synthetic_tier


# Wall-clock comparisons on a shared machine are a coin flip when taken once:
# a single scheduler preemption inside the timed region is worth more than the
# speedup being asserted. Measured on this repo at load 11.6 / 8 cores, with a
# per-AC build running, the grouped path's five samples spanned 354-433ms
# against flat's 441-488ms -- the two ranges barely miss overlapping, so the
# single-shot form failed roughly one run in five while the underlying win was
# never in doubt. Minimum-of-N is the standard robust statistic here: noise only
# ever adds time, so the fastest sample is the one least contaminated by it, and
# a real regression (the paths at parity, or inverted) still fails because it
# moves the floor, not just the spread -- provided both floors are estimated
# over the same stretch of machine time, which is why the samples are
# interleaved rather than taken in two blocks (see _interleaved_ms).
TIMING_SAMPLES = 5


def _interleaved_ms(first, second, samples=TIMING_SAMPLES):
    """Wall-clock samples of two callables, alternated rather than taken as
    two consecutive blocks. Returns (first_samples, second_samples).

    Best-of-N is robust to noise *within* a measurement window and not at all
    to a shift *between* windows -- a gap in the original reasoning here, not
    a subtlety of it. Sampling one path five times and then the other five
    times makes each minimum an estimate of a different few seconds of
    machine time, so a load transient covering only the second block raises
    only that block's floor and inverts the comparison without ever
    violating "noise only adds time". It just adds it to one side.

    That is the most plausible surviving account of the one unexplained
    failure recorded on the grouped test below: the full-corpus scans this
    machine runs are bursty, which fits both the inversion and the fact that
    *steady* external load never reproduced it. It is not a demonstrated
    cause, and this is not a fix for a diagnosed bug -- it removes a way the
    measurement could always have lied, whether or not that is what happened.

    Alternating costs nothing: both estimates now see the same load
    trajectory, so a transient has to hit both or neither. The order flips
    each round so neither callable is permanently the one sampled a few
    milliseconds earlier as load drifts through a round.
    """
    out = ([], [])
    fns = (first, second)
    for i in range(samples):
        for j in (0, 1) if i % 2 == 0 else (1, 0):
            t0 = time.perf_counter()
            fns[j]()
            out[j].append((time.perf_counter() - t0) * 1000)
    return out


def _report(label, samples):
    return f"{label} {min(samples):.0f}ms (samples " + \
        ", ".join(f"{s:.0f}" for s in samples) + ")"


def _machine():
    """What the box was doing while it measured.

    A wall-clock assertion that fails with two numbers and nothing else can
    only be re-run, and a re-run on a quiet machine says nothing about the
    one that failed. These are cheap and they are the difference between a
    diagnosable report and a shrug.
    """
    try:
        load = ", ".join(f"{x:.2f}" for x in os.getloadavg())
    except (OSError, AttributeError):  # not every platform has it
        load = "unavailable"
    return f"cpu_count={os.cpu_count()}, loadavg=({load})"


@pytest.fixture(scope="module")
def rows():
    return synthetic_tier(1)  # ~40K rows, incl. short/garbled names


@pytest.fixture(scope="module")
def multi_ac_rows():
    return synthetic_tier(5)  # 5 AC groups -- needed to exercise grouped concurrency


@pytest.mark.parametrize("algorithm", ["wratio", "jaro_winkler"])
def test_old_and_new_scoring_produce_identical_top50(rows, algorithm):
    """The vectorized cdist path must be an exact behavioral match for the
    old per-row loop, not an approximation -- same (score, voter_id) top-50,
    same order."""
    old = score_old(rows, "Ravi Kumar", "Anand Sharma", algorithm)
    new = score_new(rows, "Ravi Kumar", "Anand Sharma", algorithm)
    assert len(old) == len(new)
    for (old_score, old_id), (new_score, new_id) in zip(old, new):
        assert old_id == new_id
        assert old_score == pytest.approx(new_score, abs=1e-3)


def test_new_scoring_is_meaningfully_faster(rows):
    """Regression guard: the whole point of vectorizing is a real wall-clock
    win, not just equivalent output. Best-of-N, interleaved -- see
    _interleaved_ms.

    Both paths here are single-threaded, so unlike the grouped test below
    this one's premise does not depend on the machine having spare cores.
    """
    old, new = _interleaved_ms(
        lambda: score_old(rows, "Ravi Kumar", "Anand Sharma", "wratio"),
        lambda: score_new(rows, "Ravi Kumar", "Anand Sharma", "wratio"),
    )

    assert min(new) < min(old), (
        f"{_report('vectorized', new)} vs {_report('per-row', old)}; {_machine()}"
    )


@pytest.mark.parametrize("algorithm", ["wratio", "jaro_winkler"])
def test_grouped_and_flat_scoring_produce_identical_top50(multi_ac_rows, algorithm):
    """Grouping by constituency is purely a concurrency mechanism -- same
    top-50 as scoring everything in one flat call."""
    flat = score_new(multi_ac_rows, "Ravi Kumar", "Anand Sharma", algorithm)
    grouped, _timing = score_grouped(multi_ac_rows, "Ravi Kumar", "Anand Sharma", algorithm)
    assert len(flat) == len(grouped)
    for (flat_score, flat_id), (grouped_score, grouped_id) in zip(flat, grouped):
        assert flat_id == grouped_id
        assert flat_score == pytest.approx(grouped_score, abs=1e-3)


def test_grouped_scoring_is_meaningfully_faster_across_constituencies(multi_ac_rows):
    """Only a multi-AC tier can show this -- score_fields_batch_by_group's
    concurrency is across groups, so a single-AC tier gets no speedup by
    design (see its docstring).

    A note for whoever sees this fail, because it has once -- 452ms grouped
    against 425ms flat, best of five each -- and **that single inversion
    remains unexplained.** What follows is what was ruled out, not a
    diagnosis.

    The obvious reading is a busy machine: the grouped path's only advantage
    is spare cores to fan out into, and scripts/bench_concurrency.py shows it
    going *negative* -- 0.78x -- once four searches share a 4-vCPU instance,
    so that regime is real and is the one production runs in at peak. But
    that regime is four searches inside one interpreter, and nothing external
    reproduced it here. The assertion still held under eight saturating CPU
    loops (loadavg 7.3 on 8 cores), under three memory-bandwidth hogs
    (loadavg 16.6), and under three separate processes each running this very
    grouped scoring in a loop.

    So a load-average skip was considered and deliberately not added. It
    would gate the test on a signal not shown to invert it, and this machine
    idles at loadavg 3.1 of 8 cores -- already above where such a guard would
    have to sit to have caught the one real failure, which means it would
    mostly just stop the test running. Loosening the assertion was likewise
    rejected: a gating test that tolerates an inversion is worse than one
    that occasionally cannot run, and the inversion is precisely the thing
    worth hearing about.

    Two things changed instead. A failure now carries every sample and the
    machine's state, so the next one can be diagnosed rather than merely
    repeated. And the two paths' samples are interleaved, which closes the
    one route by which best-of-N could have produced that inversion without
    anything being wrong with the code -- a load transient landing on the
    second of two consecutive measurement blocks. That is a plausible
    account rather than a demonstrated one; if the inversion recurs under
    interleaving it is real, and this docstring should say so.
    """
    flat, grouped = _interleaved_ms(
        lambda: score_new(multi_ac_rows, "Ravi Kumar", "Anand Sharma", "wratio"),
        lambda: score_grouped(multi_ac_rows, "Ravi Kumar", "Anand Sharma", "wratio"),
    )

    assert min(grouped) < min(flat), (
        f"{_report('grouped', grouped)} vs {_report('flat', flat)}; {_machine()}"
    )


def test_grouped_scoring_returns_per_constituency_timing(multi_ac_rows):
    _scored, timing_by_group_ms = score_grouped(multi_ac_rows, "Ravi Kumar", "Anand Sharma", "wratio")
    assert len(timing_by_group_ms) == 5
    assert all(ms >= 0 for ms in timing_by_group_ms.values())
