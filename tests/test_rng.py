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
    rng = RngStream(seed=42, stream="storylet_draw", tick=5, log=log)

    uniform = rng.uniform("salience jitter")
    bernoulli = rng.bernoulli("secret exposure", probability=0.25)
    choice = rng.weighted_choice("spotlight", [("quiet-room", 1), ("shared-space-repair", 3)])

    assert len(log.records) == 3
    assert log.records[0] == {
        "stream": "storylet_draw",
        "tick": 5,
        "purpose": "salience jitter",
        "primitive": "uniform",
        "result": uniform,
    }
    assert log.records[1]["result"] is bernoulli
    assert log.records[2]["result"] == choice
