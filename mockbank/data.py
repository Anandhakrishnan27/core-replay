"""Synthetic members only. No real PII anywhere in this repo."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    account_type: str  # e.g. "Share Savings", "Share Draft Checking"
    balance: str  # pre-formatted as the legacy UI shows it, e.g. "$2,450.17"
    status: str  # "Active" | "Dormant" | "Closed"


@dataclass(frozen=True)
class Member:
    member_id: str
    name: str
    accounts: list[Account] = field(default_factory=list)


MEMBERS: dict[str, Member] = {
    "10001": Member(
        "10001",
        "Test Member A",
        [
            Account("Share Savings", "$2,450.17", "Active"),
            Account("Share Draft Checking", "$812.03", "Active"),
        ],
    ),
    "10002": Member(
        "10002",
        "Test Member B",
        [
            Account("Share Draft Checking", "$97.40", "Active"),
            Account("Share Savings", "$1,203.55", "Active"),  # different row order on purpose
        ],
    ),
    "10003": Member(
        "10003",
        "Test Member C",
        [
            Account("Share Draft Checking", "$15.00", "Dormant"),  # no savings -> NO_SAVINGS_ACCOUNT
        ],
    ),
}


def find_member(member_id: str) -> Member | None:
    return MEMBERS.get(member_id)
