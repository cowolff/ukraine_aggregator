"""Normalisation, transliteration, coordinate regex and gazetteer matching (PLAN §12)."""
from __future__ import annotations

import pytest

from app.services.geo import find_coordinates, in_ukraine_bbox, parse_bbox
from app.services.geocode import (
    canonical_oblast,
    match_place,
    name_search_value,
    normalize,
    transliterate,
)


class TestNormalisation:
    def test_cyrillic_transliterates(self):
        assert transliterate("Часів") == "chasiv"
        assert transliterate("Покровськ") == "pokrovsk"

    def test_noise_tokens_dropped(self):
        assert normalize("село Новоселовка") == normalize("Новоселовка")
        assert normalize("місто Покровськ") == "pokrovsk"

    def test_apostrophes_and_punctuation(self):
        assert normalize("Куп'янськ") == normalize("Купянськ")
        assert normalize("Sloviansk, Donetsk") == "sloviansk donetsk"

    def test_empty_input(self):
        assert normalize(None) == ""
        assert normalize("   ") == ""

    def test_name_search_concatenates_unique_variants(self):
        value = name_search_value("Часів Яр", "Часов Яр", "Chasiv Yar")
        assert "chasiv iar" in value and "chasov iar" in value and "chasiv yar" in value

    def test_name_search_dedupes(self):
        assert name_search_value("Lviv", "Lviv", None) == "lviv"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Донецька область", "donetsk"),
            ("Donetsk Oblast", "donetsk"),
            ("Харківська", "kharkiv"),
            ("Kharkov region", "kharkiv"),
            ("nowhere-land", None),
            (None, None),
            # Noun forms and Russian-derived exonyms the adjectival aliases can't reach.
            ("Запорожье", "zaporizhzhia"),
            ("Nikolaev", "mykolaiv"),
            ("Chernigov", "chernihiv"),
            # Spelling variants absorbed by the fuzzy fallback rather than enumerated.
            ("Zaporizhia", "zaporizhzhia"),
            ("Zaporizhye", "zaporizhzhia"),
            ("Zaporizhzhya", "zaporizhzhia"),
            ("Kharkiw", "kharkiv"),
            ("Mykolayiv", "mykolaiv"),
            ("East-Zaporizhzhia direction", "zaporizhzhia"),
            # Fuzzy must not pull non-Ukrainian regions or generic words to a canon.
            ("Moscow", None),
            ("Belgorod", None),
            ("Kursk", None),
            ("direction", None),
        ],
    )
    def test_oblast_aliases(self, raw, expected):
        assert canonical_oblast(raw) == expected

    def test_fuzzy_never_crosses_close_oblast_pairs(self):
        # chernihiv and chernivtsi are each other's nearest alias neighbours; exact spellings
        # must resolve to themselves and anything in between must stay None, never flip.
        assert canonical_oblast("Chernihiv") == "chernihiv"
        assert canonical_oblast("Chernivtsi") == "chernivtsi"
        assert canonical_oblast("Chernihivtsi") in (None, "chernihiv")


class TestCoordinateRegex:
    def test_decimal_pair(self):
        assert find_coordinates("impact at 48.5678, 37.1234 today") == [(48.5678, 37.1234)]

    def test_dms(self):
        found = find_coordinates("""48°30'15" N, 37°45'00" E""")
        assert len(found) == 1
        lat, lon = found[0]
        assert lat == pytest.approx(48.5042, abs=1e-3)
        assert lon == pytest.approx(37.75, abs=1e-3)

    def test_requires_three_decimals(self):
        # "48.5, 37.1" is far too coarse to be a real geolocation claim.
        assert find_coordinates("about 48.5, 37.1 area") == []

    def test_multiple_and_deduped(self):
        found = find_coordinates("48.500,37.100 then again 48.500,37.100 and 49.700,37.616")
        assert found == [(48.5, 37.1), (49.7, 37.616)]

    def test_no_false_positive_on_version_strings(self):
        assert find_coordinates("build 1.2.3456 of the app") == []

    def test_empty(self):
        assert find_coordinates(None) == []
        assert find_coordinates("") == []


class TestBBox:
    def test_valid(self):
        assert parse_bbox("30,46,40,52") == (30.0, 46.0, 40.0, 52.0)

    def test_none(self):
        assert parse_bbox(None) is None

    @pytest.mark.parametrize("raw", ["1,2,3", "a,b,c,d", "40,46,30,52", "30,52,40,46", "200,46,240,52"])
    def test_invalid(self, raw):
        with pytest.raises(ValueError):
            parse_bbox(raw)

    def test_ukraine_gate(self):
        assert in_ukraine_bbox(48.5, 37.1)
        assert not in_ukraine_bbox(10.0, 10.0)      # outside
        assert not in_ukraine_bbox(None, 37.1)
        assert in_ukraine_bbox(51.1, 35.4)          # Kursk border region is inside the gate


class TestPerspectiveLean:
    """The diverging value behind cluster colour (0 = Ukrainian, 1 = Russian, 0.5 = neutral)."""

    def test_poles_and_midpoint(self):
        from app.api.events import side_lean

        assert side_lean(0, 0) == 0.5, "nothing partisan to weigh"
        assert side_lean(5, 5) == 0.5, "balanced"
        assert side_lean(50, 0) < 0.1, "overwhelmingly Ukrainian"
        assert side_lean(0, 50) > 0.9, "overwhelmingly Russian"

    def test_small_samples_are_damped(self):
        """One report is not evidence of one-sided coverage and must not paint a full pole."""
        from app.api.events import side_lean

        single = side_lean(0, 1)
        many = side_lean(0, 20)
        assert 0.5 < single < 0.75, f"a lone report should be muted, got {single}"
        assert many > single, "more evidence pushes further toward the pole"

    def test_symmetry(self):
        from app.api.events import side_lean

        for ua, ru in ((1, 0), (3, 1), (10, 2), (7, 7)):
            assert side_lean(ua, ru) == pytest.approx(
                1 - side_lean(ru, ua), abs=1e-6
            ), "the scale must treat both sides identically"

    def test_monotonic_in_the_russian_share(self):
        from app.api.events import side_lean

        leans = [side_lean(10 - i, i) for i in range(11)]
        assert leans == sorted(leans), "more Russian share never lowers the value"


class TestGazetteerMatching:
    def test_exact_ukrainian_name(self, gazetteer):
        match = match_place("Часів Яр")
        assert match is not None and not match.ambiguous
        assert match.name == "Часів Яр"
        assert match.lat == pytest.approx(48.5906, abs=1e-3)

    def test_russian_variant_matches(self, gazetteer):
        match = match_place("Часов Яр")
        assert match is not None and match.name == "Часів Яр"

    def test_latin_transliteration_matches(self, gazetteer):
        match = match_place("Pokrovsk")
        assert match is not None and match.name == "Покровськ"

    def test_ambiguous_same_name_two_oblasts_is_flagged(self, gazetteer):
        # Новоселівка exists in both donetsk and sumy in the fixture, with no oblast context.
        match = match_place("Новоселівка")
        assert match is not None and match.ambiguous is True

    def test_oblast_hint_resolves_ambiguity(self, gazetteer):
        match = match_place("Новоселівка", "Донецька область")
        assert match is not None and match.ambiguous is False
        assert match.oblast == "donetsk"

    def test_unknown_place_returns_none(self, gazetteer):
        assert match_place("Atlantis-on-Sea") is None

    def test_too_short_query_returns_none(self, gazetteer):
        assert match_place("Ky") is None
        assert match_place(None) is None

    def test_below_similarity_threshold_returns_none(self, gazetteer):
        # A near-miss word must not silently snap to a real settlement.
        assert match_place("Pokrovskoyeville") is None

    def test_fallback_after_hint_miss_requires_near_exact(self, gazetteer):
        # "Novoselivsky" fuzzy-scores ~0.69 against Novoselivka — placeable country-wide, but
        # when a hinted oblast search came up empty only a near-exact hit may fall back. This is
        # the "Holosiivskyi" bug: a Kyiv district snapping onto a like-named Donetsk village.
        assert match_place("Novoselivsky") is not None
        assert match_place("Novoselivsky", "Lviv") is None

    def test_fallback_after_hint_miss_allows_exact_name(self, gazetteer):
        # A wrong hint must not unplace a unique, exactly-named settlement.
        match = match_place("Бахмут", "Луганська")
        assert match is not None and match.oblast == "donetsk"

    def test_unresolvable_hint_also_requires_near_exact(self, gazetteer):
        # A hint the canonicalizer can't resolve is usually a FOREIGN region ("Eysky District");
        # the extractor claimed to know where the place is, so a mere fuzzy country-wide hit
        # must not place it ("Ейский район" once landed on Євбаз in Kyiv at 0.71).
        assert match_place("Novoselivsky", "Eysky District") is None
        # ...but an exactly-named settlement still survives a garbage hint.
        match = match_place("Бахмут", "Nowhere Federal District")
        assert match is not None and match.oblast == "donetsk"

    def test_non_ukraine_places_stay_unplaced(self, gazetteer):
        # Strike-origin reporting names Russian regions constantly; the gazetteer only holds
        # Ukraine, so these must never fuzzy-match into it.
        for name in ("Rostov region", "Бєлгород", "Брянск", "Engels", "Курськ", "Гомель"):
            assert match_place(name) is None, name
            assert match_place(name, "Kharkiv") is None, name

    def test_match_reports_its_resolution_path(self, gazetteer):
        assert match_place("Покровськ").resolution == "countrywide"
        assert match_place("Новоселівка", "Донецька область").resolution == "hinted"
        assert match_place("Бахмут", "Луганська").resolution == "fallback"


class TestGeocodeTask:
    def test_placement_records_resolution_metadata(self, gazetteer, make_source, make_event):
        import json

        from sqlalchemy import text

        from app.extensions import db
        from celery_worker.tasks.geocode import geocode_pending

        event = make_event(make_source("western"), lat=None, lon=None)
        db.session.execute(
            text(
                "UPDATE extracted_events SET place_name_raw='Покровськ', "
                "llm_raw=CAST(:raw AS jsonb) WHERE id=:i"
            ),
            {"i": event.id, "raw": json.dumps({"oblast": "Donetsk"})},
        )
        db.session.commit()
        stats = geocode_pending()
        assert stats["placed"] >= 1
        row = db.session.execute(
            text(
                "SELECT geo_meta->>'resolution' AS resolution, geo_meta->>'hint' AS hint, "
                "(geo_meta->>'similarity')::float AS similarity "
                "FROM extracted_events WHERE id=:i"
            ),
            {"i": event.id},
        ).first()
        assert row.resolution == "hinted"
        assert row.hint == "Donetsk"
        assert row.similarity == pytest.approx(1.0)
