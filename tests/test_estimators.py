import pytest

from inference.estimators import (
    estimate_analogous_benchmark,
    estimate_cagr_projection,
    estimate_share_x_parent,
)


def test_share_x_parent_formula():
    assert estimate_share_x_parent(1000.0, 0.25) == pytest.approx(250.0)


def test_share_x_parent_boundary():
    assert estimate_share_x_parent(1.0, 1.0) == pytest.approx(1.0)


@pytest.mark.parametrize("parent,ratio", [(0, 0.2), (-1, 0.2), (100, 0), (100, 1.2)])
def test_share_x_parent_reject_invalid(parent, ratio):
    with pytest.raises(ValueError):
        estimate_share_x_parent(parent, ratio)


def test_cagr_projection_formula():
    assert estimate_cagr_projection(100.0, 0.10, 3) == pytest.approx(133.1, rel=1e-6)


@pytest.mark.parametrize("base,cagr,years", [(0, 0.1, 2), (-1, 0.1, 2), (100, -1.0, 1), (100, 0.1, -1)])
def test_cagr_projection_reject_invalid(base, cagr, years):
    with pytest.raises(ValueError):
        estimate_cagr_projection(base, cagr, years)


def test_analogous_benchmark_formula():
    assert estimate_analogous_benchmark(150.0, 0.15) == pytest.approx(1000.0)


@pytest.mark.parametrize("revenue,share", [(-1, 0.1), (100, 0), (100, 1.1)])
def test_analogous_benchmark_reject_invalid(revenue, share):
    with pytest.raises(ValueError):
        estimate_analogous_benchmark(revenue, share)
