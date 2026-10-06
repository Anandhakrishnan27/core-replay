from cua import catalog


def test_load_latest_and_approve(catalog_root):
    art = catalog.load("mockbank.member.lookup_savings_balance", root=catalog_root)
    assert art.version == "1.0.0" and art.review.status.value == "draft"
    catalog.approve(art.id, "reviewer_a", root=catalog_root)
    again = catalog.load(art.id, root=catalog_root)
    assert again.review.status.value == "approved" and again.review.reviewed_by == "reviewer_a"
