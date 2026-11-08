import numpy as np
import pytest

from vision.bbox_distance_eval import (Sample, evaluate, is_truncated, nearest, rel_error_stats, torso_depth,
                                       z_from_height)


def test_pinhole_height_model():
    # a 1.70 m person whose box is 539.2*1.7/2 px tall is 2 m away
    assert z_from_height(np.array([539.2 * 1.7 / 2]), 1.7)[0] == pytest.approx(2.0)


def test_torso_depth_ignores_background_and_missing_pixels():
    d = np.full((100, 100), 6.0, np.float32)    # wall behind
    d[20:90, 40:60] = 2.0                        # person
    d[30:35, 45:55] = 0.0                        # missing depth (0) inside torso
    z, n = torso_depth(d, (30, 20, 70, 95))      # box wider than the person
    assert z == pytest.approx(2.0) and n > 50
    z, n = torso_depth(np.zeros((100, 100), np.float32), (30, 20, 70, 95))
    assert z is None and n == 0


def test_truncation_and_association():
    assert is_truncated((0, 10, 50, 100), (640, 480)) and is_truncated((10, 10, 50, 479), (640, 480))
    assert not is_truncated((10, 10, 50, 400), (640, 480))
    assert nearest([1.0, 2.0, 3.0], 2.4) == 1 and nearest([1.0, 2.0, 3.0], 9) == 2


def test_rel_error_stats_and_verdict_on_exact_data():
    s = rel_error_stats(np.array([2.0, 4.0]), np.array([2.0, 4.0]))
    assert s["abs_rel_err_p95"] == 0 and s["within_10pct"] == 1.0
    # boxes that follow the pinhole model exactly for a 1.80 m person -> calibrated error 0, verdict PASS
    zs = np.linspace(1.5, 4.0, 40)
    samples = [Sample(i, float(i), "x.png", (100, 10, 140, 10 + 539.2 * 1.8 / z), 0.9, float(z), 500, False)
               for i, z in enumerate(zs)]
    res, *_ = evaluate(samples)
    assert res["calibrated"]["fitted_person_height_m"] == pytest.approx(1.8, abs=1e-6)
    assert res["calibrated"]["all"]["abs_rel_err_p95"] == 0 and res["verdict"]["result"] == "PASS"
    assert res["prior_1.70m"]["all"]["signed_rel_err_median"] == pytest.approx(1.7 / 1.8 - 1, abs=1e-3)
