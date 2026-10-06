import shutil

from cua import catalog
from tests.conftest import ROOT


def test_load_latest_and_approve(tmp_path):
    shutil.copytree(ROOT / "capabilities/mockbank", tmp_path / "mockbank")
    art = catalog.load("mockbank.member.lookup_savings_balance", root=tmp_path)
    assert art.version == "1.0.0" and art.review.status.value == "draft"
    catalog.approve(art.id, "reviewer_a", root=tmp_path)
    again = catalog.load(art.id, root=tmp_path)
    assert again.review.status.value == "approved" and again.review.reviewed_by == "reviewer_a"
