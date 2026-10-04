import pytest

from breakroom.resolution.rng import RngStream, RollLog


def test_same_seed_stream_and_tick_reproduce_draw_sequence() -> None:
    first = RngStream(seed=42, stream="incidents", tick=3)
    second = RngStream(seed=42, stream="incidents", tick=3)

    assert [first.uniform("incident odds") for _ in range(3)] == [
        second.uniform("incident odds") for _ in range(3)
    ]


def test_interleaving_streams_does_not_change_single_stream_sequence() -> None:
    incidents = RngStream(seed=42, stream="incidents", tick=3)
    exposure = RngStream(seed=42, stream="exposure", tick=3)
    interleaved = [
        incidents.uniform("a"),
        exposure.uniform("x"),
        incidents.uniform("b"),
        exposure.uniform("y"),
        incidents.uniform("c"),
    ]

    incidents_only = RngStream(seed=42, stream="incidents", tick=3)

    assert [interleaved[0], interleaved[2], interleaved[4]] == [
        incidents_only.uniform("a"),
        incidents_only.uniform("b"),
        incidents_only.uniform("c"),
    ]


def test_draw_primitives_record_auditable_rolls() -> None:
    log = RollLog()
    rng = RngStream(seed=42, stream="storylet_select", tick=5, log=log)

    uniform = rng.uniform("salience jitter")
    bernoulli = rng.bernoulli("secret exposure", probability=0.25)
    choice = rng.weighted_choice("spotlight", [("quiet-room", 1), ("shared-space-repair", 3)])

    assert len(log.records) == 3
    assert log.records[0] == {
        "stream": "storylet_select",
        "tick": 5,
        "purpose": "salience jitter",
        "primitive": "uniform",
        "result": uniform,
    }
    assert log.records[1]["result"] is bernoulli
    assert log.records[2]["result"] == choice


def test_weighted_choice_rejects_negative_weight_sorted_after_winner() -> None:
    rng = RngStream(seed=42, stream="incidents", tick=3)

    with pytest.raises(ValueError, match="choice weights must be non-negative"):
        rng.weighted_choice("spotlight", [("a", 10), ("b", -5)])


@pytest.mark.parametrize("probability", [float("nan"), float("inf"), float("-inf")])
def test_bernoulli_rejects_non_finite_probability_without_consuming_draw(
    probability: float,
) -> None:
    log = RollLog()
    rng = RngStream(seed=42, stream="incidents", tick=3, log=log)
    control = RngStream(seed=42, stream="incidents", tick=3)

    with pytest.raises(ValueError):
        rng.bernoulli("invalid odds", probability=probability)

    assert log.records == []
    assert rng.uniform("next draw") == control.uniform("next draw")


@pytest.mark.parametrize("bad_weight", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("bad_position", [0, 1])
def test_weighted_choice_rejects_non_finite_weight_without_consuming_draw(
    bad_weight: float, bad_position: int
) -> None:
    choices = [("a", 1.0), ("b", 1.0)]
    choices[bad_position] = (choices[bad_position][0], bad_weight)
    log = RollLog()
    rng = RngStream(seed=42, stream="incidents", tick=3, log=log)
    control = RngStream(seed=42, stream="incidents", tick=3)

    with pytest.raises(ValueError):
        rng.weighted_choice("invalid weights", choices)

    assert log.records == []
    assert rng.uniform("next draw") == control.uniform("next draw")
