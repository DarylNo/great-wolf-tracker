"""Grouping suite codes into rooms (gwt.rooms)."""

from gwt.demo import DEMO_SUITES
from gwt.rooms import Variant, category_rank, group_rooms, slugify


def demo_variants(rooms=None, rates=None):
    rooms = rooms or {}
    rates = rates or {}
    out = []
    for i, s in enumerate(DEMO_SUITES):
        r = rooms.get(s.code, s.capacity)
        saver = rates.get(s.code, s.base_rate + 0.99)
        out.append(
            Variant(
                code=s.code,
                name=s.name,
                category=s.category,
                rooms=r,
                accessible=int(s.accessible),
                beds=s.beds,
                baths=s.baths,
                max_occupancy=s.max_occupancy,
                min_rate=saver if r else None,
                rates={"Saver": saver, "Flexible": saver + 50} if r else {},
                sort_order=i,
            )
        )
    return out


def test_27_codes_group_into_17_rooms():
    groups = group_rooms(demo_variants())
    assert len(DEMO_SUITES) == 27
    assert len(groups) == 17
    sizes = {g.name: len(g.variants) for g in groups}
    assert sizes["Timber Wolf Cottage"] == 4
    for name in ("Family Suite", "KidKamp Suite", "Queen Sofa Suite", "Wolf Den Suite", "King Cabin Suite",
                 "Deluxe KidCabin Suite", "Loft Fireplace Suite"):
        assert sizes[name] == 2, name


def test_group_sums_ranges_and_accessible_rooms():
    groups = {g.name: g for g in group_rooms(demo_variants(rooms={"SFSSB": 40, "SFSSA": 3}))}
    family = groups["Family Suite"]
    assert family.rooms == 43
    assert family.accessible_rooms == 3
    assert family.has_accessible
    assert family.occupancy_label == "6"
    assert groups["Queen Sofa Suite"].occupancy_label == "4–6"
    assert groups["Timber Wolf Cottage"].occupancy_label == "10–14"
    assert not groups["Lakeside Cottage"].has_accessible
    assert groups["Lakeside Cottage"].accessible_rooms == 0
    # Accessible variants are listed after the standard ones.
    assert [v.code for v in family.variants] == ["SFSSB", "SFSSA"]


def test_family_rates_ignore_sold_out_variants():
    variants = [
        Variant("A1", "Room", "standard", rooms=0, rates={"Saver": 199.99}, min_rate=None),
        Variant("A2", "Room", "standard", rooms=5, rates={"Saver": 249.99, "Flexible": 289.99}, min_rate=249.99),
        Variant("A3", "Room", "standard", rooms=2, rates={"Saver": 239.99}, min_rate=239.99),
    ]
    (room,) = group_rooms(variants)
    assert room.family_rate("Saver") == 239.99  # A1's cheaper rate is ignored: no rooms
    assert room.family_rate("Flexible") == 289.99
    assert room.family_rate("Member") is None
    assert room.cheapest == 239.99


def test_sold_out_only_when_every_variant_is_at_zero():
    groups = {g.name: g for g in group_rooms(demo_variants(rooms={"SFSSB": 0, "CTW2B": 0, "CTW3B": 0, "CTW4B": 0, "CTWAA": 0}))}
    assert not groups["Family Suite"].sold_out  # the accessible variant still has rooms
    assert groups["Timber Wolf Cottage"].sold_out
    assert groups["Timber Wolf Cottage"].cheapest is None


def test_sort_by_category_then_price_keeping_sold_out_in_place():
    # Family Suite sold out tonight; the Majestic Bear is pricier than usual. With reference
    # rates (each room's usual price) the order is stable and the sold-out room keeps its place.
    variants = demo_variants(rooms={"SFSSB": 0, "SFSSA": 0}, rates={"PMBSB": 2000.99})
    reference = {s.name: s.base_rate + 0.99 for s in DEMO_SUITES}
    groups = group_rooms(variants, reference_rates=reference)
    names = [g.name for g in groups]
    assert names[:5] == ["Double Queen Suite", "Family Suite", "Queen Sofa Suite", "Fireplace Suite",
                         "Deluxe Family Suite"]
    assert names.index("Majestic Bear Suite") == names.index("Loft Fireplace Suite") + 1
    assert [category_rank(g.category) for g in groups] == sorted(category_rank(g.category) for g in groups)
    assert groups[1].sold_out
    # Without reference rates: current cheapest rate, sold-out rooms last in their category.
    plain = [g.name for g in group_rooms(variants)]
    assert plain[4] == "Family Suite"
    assert plain.index("Majestic Bear Suite") > plain.index("Presidential Suite")


def test_slugify_and_unique_slugs():
    assert slugify("Deluxe KidCabin Suite") == "deluxe-kidcabin-suite"
    assert slugify("Presidents' Suite!") == "presidents-suite"
    assert slugify("???") == "room"
    groups = group_rooms([Variant("X1", "Suite A"), Variant("X2", "Suite-A")])
    assert sorted(g.slug for g in groups) == ["suite-a", "suite-a-2"]
