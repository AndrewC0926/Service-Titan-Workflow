"""Block 3 (Master Plan v3.2 section 12a): weakest-why decision rule
(app.pipeline.reason_block.weakest_why_rank)."""
import pytest

from app.models import ReasonStrength as S
from app.pipeline.reason_block import weakest_why_rank


def test_three_strong_ranks_above_two_strong_one_weak():
    three_strong = weakest_why_rank([S.Strong, S.Strong, S.Strong])
    two_strong_one_weak = weakest_why_rank([S.Strong, S.Strong, S.Weak])
    assert three_strong < two_strong_one_weak


def test_two_strong_one_weak_ranks_above_any_abstain():
    two_strong_one_weak = weakest_why_rank([S.Strong, S.Strong, S.Weak])
    one_abstain_two_strong = weakest_why_rank([S.Strong, S.Strong, S.ABSTAIN])
    all_abstain = weakest_why_rank([S.ABSTAIN, S.ABSTAIN, S.ABSTAIN])
    assert two_strong_one_weak < one_abstain_two_strong
    assert two_strong_one_weak < all_abstain


def test_any_abstain_ranks_the_same_regardless_of_the_other_two_being_strong():
    """The rule is 'ranks by its weakest why, never a weighted sum': one
    ABSTAIN alone should push an opportunity into the ABSTAIN tier even when
    the other two whys are Strong -- it must not out-rank a weaker-but-no-
    ABSTAIN combination just because two of its three whys are strong."""
    one_abstain_two_strong = weakest_why_rank([S.Strong, S.Strong, S.ABSTAIN])
    three_weak = weakest_why_rank([S.Weak, S.Weak, S.Weak])
    assert three_weak < one_abstain_two_strong


def test_order_of_the_three_whys_does_not_matter():
    assert weakest_why_rank([S.Strong, S.Weak, S.ABSTAIN]) == weakest_why_rank([S.ABSTAIN, S.Strong, S.Weak])


def test_fewer_abstains_ranks_above_more_abstains():
    one_abstain = weakest_why_rank([S.Strong, S.Strong, S.ABSTAIN])
    two_abstain = weakest_why_rank([S.Strong, S.ABSTAIN, S.ABSTAIN])
    three_abstain = weakest_why_rank([S.ABSTAIN, S.ABSTAIN, S.ABSTAIN])
    assert one_abstain < two_abstain < three_abstain


def test_rejects_anything_other_than_exactly_three_strengths():
    with pytest.raises(ValueError):
        weakest_why_rank([S.Strong, S.Strong])
    with pytest.raises(ValueError):
        weakest_why_rank([S.Strong, S.Strong, S.Strong, S.Weak])


def test_sorting_a_list_of_opportunities_by_this_key_orders_strongest_first():
    """The realistic use: Pipeline sorts opportunities by this key. Rady
    Children's-shaped (Strong/Strong/ABSTAIN) must not out-rank a plain
    3-Weak opportunity, even though it "looks" mostly strong."""
    rady = [S.Strong, S.Strong, S.ABSTAIN]
    uc_davis = [S.Weak, S.Strong, S.Weak]
    three_strong = [S.Strong, S.Strong, S.Strong]

    ordered = sorted([rady, uc_davis, three_strong], key=weakest_why_rank)
    assert ordered == [three_strong, uc_davis, rady]
