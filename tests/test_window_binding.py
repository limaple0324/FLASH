from core.window_binding import CharacterBindingEngine, WindowCandidate
from core.window_registry import WindowHealth, WindowRegistry


def confirmed_registry():
    registry = WindowRegistry()
    registry.register_character("160", "160古")
    registry.confirm_window(
        "160",
        handle=100,
        process_id=200,
        window_class="ShockwaveFlash",
        rect=(0, 0, 800, 600),
        health=WindowHealth.READY,
    )
    return registry


def candidate(handle=100, process_id=200, window_class="ShockwaveFlash", rect=(0, 0, 800, 600)):
    return WindowCandidate(
        handle=handle,
        process_id=process_id,
        window_class=window_class,
        rect=rect,
    )


def test_unconfirmed_character_requires_player_binding():
    registry = WindowRegistry()
    registry.register_character("160", "160古")

    result = CharacterBindingEngine(registry).bind("160", [candidate()])

    assert result.bound is False
    assert result.code == "binding.unconfirmed_character"


def test_no_candidates_marks_character_offline():
    registry = confirmed_registry()

    result = CharacterBindingEngine(registry).bind("160", [])

    assert result.bound is False
    assert result.code == "binding.no_candidates"
    assert registry.get("160").health is WindowHealth.OFFLINE
    assert registry.get("160").handle is None


def test_exact_previous_window_rebinds_with_high_confidence():
    registry = confirmed_registry()

    result = CharacterBindingEngine(registry).bind("160", [candidate()])

    assert result.bound is True
    assert result.code == "binding.rebound"
    assert result.confidence == 1.0
    assert registry.get("160").handle == 100


def test_new_handle_can_rebind_when_process_class_and_geometry_match():
    registry = confirmed_registry()

    result = CharacterBindingEngine(registry, threshold=0.50).bind(
        "160", [candidate(handle=999)]
    )

    assert result.bound is True
    assert result.candidate_handle == 999
    assert registry.get("160").handle == 999


def test_low_confidence_candidate_is_not_bound():
    registry = confirmed_registry()

    result = CharacterBindingEngine(registry).bind(
        "160",
        [candidate(handle=999, process_id=777, window_class="Other", rect=(3000, 3000, 3500, 3400))],
    )

    assert result.bound is False
    assert result.code == "binding.low_confidence"
    assert registry.get("160").handle == 100


def test_similar_candidates_require_player_confirmation():
    registry = confirmed_registry()
    engine = CharacterBindingEngine(registry, threshold=0.50, margin=0.20)

    result = engine.bind(
        "160",
        [candidate(handle=901), candidate(handle=902)],
    )

    assert result.bound is False
    assert result.code == "binding.ambiguous"
    assert registry.get("160").handle == 100


def test_clear_best_candidate_wins_without_guessing():
    registry = confirmed_registry()
    engine = CharacterBindingEngine(registry, threshold=0.50, margin=0.10)

    result = engine.bind(
        "160",
        [
            candidate(handle=100),
            candidate(handle=999, process_id=777, window_class="Other", rect=(2000, 2000, 2500, 2400)),
        ],
    )

    assert result.bound is True
    assert result.candidate_handle == 100
