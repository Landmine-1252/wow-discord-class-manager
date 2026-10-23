from dataclasses import replace

import pytest

from wow_class_manager.character_names import normalized_name_key, validate_names
from wow_class_manager.roles import UserError


@pytest.mark.parametrize(
    "primary,secondary",
    [
        ("John", "Stormwind"),
        ("Sam", "Ironforge"),
        ("Élodie", "Cœur"),
        ("李明", "星海"),
        ("Иван", "Звезда"),
    ],
)
def test_valid_two_part_names(settings, primary, secondary):
    first, second, key = validate_names(settings, primary, secondary)
    assert first == primary and second == secondary
    assert key == normalized_name_key(primary, secondary)


def test_canonical_unicode_and_case_comparison(settings):
    first = validate_names(settings, "E\u0301lodie", "Stormwind")
    second = validate_names(settings, "éLODIE", "stormwind")
    assert first[0] == "Élodie"
    assert first[2] == second[2]
    assert (
        validate_names(settings, "John", "Stormwind")[2]
        != validate_names(settings, "John", "Ironforge")[2]
    )


@pytest.mark.parametrize(
    "name",
    [
        "",
        "A",
        "A" * 13,
        " John",
        "John ",
        "Jo hn",
        "John\n",
        "Jo\t hn",
        "Jo\x00hn",
        "Jo\u200bhn",
        "Jo\u200dhn",
        "Jo\u202ehn",
        "Jo\ufeffhn",
        "Jo\ud800hn",
        "@everyone",
        "<@123>",
        "**John**",
        "John|",
        "John-Sam",
        "John'Sam",
        "John123",
        "\u0301John",
        "Jo\u00a0hn",
        "John\u2060",
    ],
)
def test_invalid_name_components(settings, name):
    for primary, secondary in [(name, "Stormwind"), ("John", name)]:
        with pytest.raises(UserError):
            validate_names(settings, primary, secondary)


def test_configurable_component_limits(settings):
    settings = replace(
        settings,
        character_primary_name_min_length=1,
        character_primary_name_max_length=20,
        character_secondary_name_min_length=3,
        character_secondary_name_max_length=20,
    )
    assert validate_names(settings, "A", "Stormwind")[0] == "A"
    assert validate_names(settings, "LongPrimaryName", "LongSecondaryName")
    with pytest.raises(UserError, match="Secondary Name"):
        validate_names(settings, "A", "Li")


@pytest.mark.parametrize(
    "codepoint",
    [
        0x034F,
        0x115F,
        0x1160,
        0x17B4,
        0x17B5,
        0x3164,
        0xFFA0,
        0x180B,
        0x180D,
        0x180F,
        0xFE00,
        0xFE0F,
        0xE0100,
        0xE01EF,
    ],
)
def test_invisible_letter_and_mark_codepoints_rejected(settings, codepoint):
    with pytest.raises(UserError):
        validate_names(settings, "Jo" + chr(codepoint) + "hn", "Stormwind")
