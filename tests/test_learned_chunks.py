"""Chunk plan for resumable learned-depth runs: overlapping 8-s chunks after an already finished first chunk."""
from learned.chunked_run import plan


def test_plan_overlaps_and_covers_the_clip():
    p = plan(60.0, 8.0, 2.0, (0.0, 10.0))
    assert p[0] == (0.0, 10.0) and p[1] == (8.0, 16.0)
    assert all(b[0] < a[1] for a, b in zip(p, p[1:]))  # every chunk overlaps the previous one
    assert p[-1][1] == 60.0


def test_plan_without_first_chunk_starts_at_zero():
    p = plan(20.0, 8.0, 2.0, None)
    assert p[0] == (0.0, 8.0) and p[-1][1] == 20.0
