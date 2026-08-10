"""Guards the FROZEN seeding algorithm. If any golden assertion here fails,
the change reshuffles every seed users have stored — revert the change, do
not update the constants."""

import support  # noqa: F401

from mrln.promptlib.seeding import derive_rng, weighted_index

NAMES = ["red", "green", "blue", "gold"]
WEIGHTS = [1, 1, 1, 3]


def test_golden_first_floats():
    assert [round(derive_rng(s, "k").random(), 6) for s in range(3)] == [
        0.008334,
        0.440859,
        0.01999,
    ]


def test_golden_weighted_draws():
    draws = [NAMES[weighted_index(derive_rng(s, "paint"), WEIGHTS)] for s in range(8)]
    assert draws == ["red", "blue", "gold", "gold", "gold", "red", "green", "red"]


def test_golden_key_independence():
    draws = [
        NAMES[weighted_index(derive_rng(7, k), WEIGHTS)]
        for k in ("paint", "extra", "lighting", "@variant")
    ]
    assert draws == ["red", "gold", "gold", "gold"]


def test_golden_uniform_pool():
    draws = [
        ["daylight", "night"][weighted_index(derive_rng(s, "lighting"), [1, 1])] for s in range(8)
    ]
    assert draws == [
        "night",
        "daylight",
        "night",
        "daylight",
        "daylight",
        "night",
        "daylight",
        "night",
    ]


def test_same_inputs_same_stream():
    a, b = derive_rng(42, "slot"), derive_rng(42, "slot")
    assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]


def test_weight_zero_never_drawn():
    for seed in range(200):
        assert weighted_index(derive_rng(seed, "z"), [0.0, 1.0]) == 1


def test_weight_bias():
    heavy = sum(1 for seed in range(500) if weighted_index(derive_rng(seed, "b"), [10.0, 1.0]) == 0)
    assert heavy > 400  # ~10/11 expected


def test_single_rng_call_per_draw():
    rng = derive_rng(0, "c")
    weighted_index(rng, [1, 1, 1])
    # the next value must be the SECOND value of the stream
    fresh = derive_rng(0, "c")
    fresh.random()
    assert rng.random() == fresh.random()
