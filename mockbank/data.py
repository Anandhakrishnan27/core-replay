"""Synthetic members only. No real PII anywhere in this repo.

Every name, street and city is made up. SSNs use the never-issued 900-xx-xxxx range, phones use the
fictional 555-01xx block, and emails use the reserved example.test domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    account_type: str  # e.g. "Share Savings", "Share Draft Checking"
    balance: str  # pre-formatted as the legacy UI shows it, e.g. "$2,450.17"
    status: str  # "Active" | "Dormant" | "Closed"
    account_number: str  # fake, 10 digits


@dataclass(frozen=True)
class Address:
    street: str
    city: str
    state: str
    zip: str


@dataclass(frozen=True)
class Member:
    member_id: str
    first_name: str
    last_name: str
    ssn: str  # 900-xx-xxxx
    dob: str  # MM/DD/YYYY, as the legacy UI shows it
    phone: str  # 555-01xx
    email: str  # @example.test
    address: Address
    accounts: list[Account] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"


MEMBERS: dict[str, Member] = {
    "10001": Member(
        "10001",
        "Avery",
        "Quill",
        "900-41-2087",
        "03/14/1984",
        "(217) 555-0142",
        "avery.quill@example.test",
        Address("1428 Larkspur Lane", "Fernbrook", "IL", "62999"),
        [
            Account("Share Savings", "$2,450.17", "Active", "7300100011"),
            Account("Share Draft Checking", "$812.03", "Active", "7300100012"),
        ],
    ),
    "10002": Member(
        "10002",
        "Marlowe",
        "Tenby",
        "900-58-7731",
        "11/02/1979",
        "(217) 555-0167",
        "marlowe.tenby@example.test",
        Address("77 Cobbler Row", "Ashgrove", "IL", "62998"),
        [
            Account("Share Draft Checking", "$97.40", "Active", "7300100021"),
            Account("Share Savings", "$1,203.55", "Active", "7300100022"),  # different row order on purpose
        ],
    ),
    "10003": Member(
        "10003",
        "Juno",
        "Bramwell",
        "900-63-0459",
        "07/22/1992",
        "(217) 555-0189",
        "juno.bramwell@example.test",
        Address("9 Thistledown Ct", "Millbridge", "IL", "62997"),
        [
            # no savings -> NO_SAVINGS_ACCOUNT
            Account("Share Draft Checking", "$15.00", "Dormant", "7300100031"),
        ],
    ),
}


def find_member(member_id: str) -> Member | None:
    return MEMBERS.get(member_id)
