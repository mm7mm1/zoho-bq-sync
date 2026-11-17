"""Deterministic synthetic Wedding_Bookings records with a Booked_Services subform.

All people, emails (example.com) and vendors are invented. Records are marked with the
"WB-SYN-" name prefix so they can never be confused with real bookings.
"""

from __future__ import annotations

import random
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

MODULE = "Wedding_Bookings"
SUBFORM = "Booked_Services"
NAME_PREFIX = "WB-SYN-"

PARENT_FIELDS = (
    "Name",
    "Partner_1_Name",
    "Partner_2_Name",
    "Contact_Email",
    "Ceremony_Type",
    "Destination_Country",
    "Ceremony_City",
    "Wedding_Date",
    "Guest_Count",
    "Package_Price",
    "Booking_Status",
    "Documents_Verified",
    "Consultation_At",
)
SUBFORM_FIELDS = (
    "Service_Name",
    "Service_Category",
    "Vendor_Name",
    "Service_Date",
    "Quantity",
    "Unit_Price",
)

CEREMONY_TYPES = ("Civil Marriage", "Symbolic Ceremony", "Religious Ceremony", "Vow Renewal")
BOOKING_STATUSES = ("Inquiry", "Documents Pending", "Confirmed", "Completed", "Cancelled")
SERVICE_CATEGORIES = (
    "Legal & Registration",
    "Document Translation",
    "Venue",
    "Photography",
    "Floral & Decor",
    "Catering",
    "Transport",
    "Officiant",
)

FIRST_NAMES = (
    "Olena", "Taras", "Sofia", "Marco", "Anna", "Lukas", "Emma", "Noah", "Mariam", "Giorgi",
    "Ingrid", "Mateo", "Aiko", "Daniel", "Chloe", "Andrii", "Freya", "Omar", "Lucia", "Jonas",
)
LAST_NAMES = (
    "Kovalenko", "Rossi", "Muller", "Andersen", "Beridze", "Silva", "Novak", "Dubois",
    "Tanaka", "Fischer", "Shevchenko", "Garcia", "Larsen", "Moretti", "Haddad", "Ivanenko",
)

# (country, city, typical ceremony type)
DESTINATIONS = (
    ("Denmark", "Copenhagen", "Civil Marriage"),
    ("Georgia", "Tbilisi", "Civil Marriage"),
    ("Cyprus", "Larnaca", "Civil Marriage"),
    ("United States", "Las Vegas", "Civil Marriage"),
    ("Italy", "Florence", "Symbolic Ceremony"),
    ("Portugal", "Sintra", "Symbolic Ceremony"),
    ("Greece", "Santorini", "Religious Ceremony"),
    ("Spain", "Seville", "Vow Renewal"),
)

# (service name, category, invented vendor, min price, max price, quantity rule)
SERVICES = (
    ("Civil registration and marriage licence", "Legal & Registration", "Registry Liaison Desk", 450, 900, "one"),
    ("Apostille and legalization of documents", "Legal & Registration", "Northgate Legal Services", 120, 300, "one"),
    ("Certified translation of documents (per page)", "Document Translation", "Polyglot Sworn Translations", 25, 45, "pages"),
    ("Sworn interpreter at the ceremony", "Legal & Registration", "Polyglot Sworn Translations", 150, 400, "one"),
    ("Ceremony venue rental", "Venue", "Harbour View Hall", 800, 3500, "one"),
    ("Wedding photographer (4 hours)", "Photography", "Lumen & Lace Studio", 600, 1800, "one"),
    ("Bridal bouquet and boutonniere", "Floral & Decor", "Wildflower Atelier", 120, 350, "one"),
    ("Celebrant / officiant", "Officiant", "Vow Keepers Celebrants", 300, 700, "one"),
    ("Airport and venue transfers", "Transport", "Coastline Chauffeurs", 90, 260, "one"),
    ("Dinner for guests (per person)", "Catering", "Olive Table Catering", 45, 120, "guests"),
)


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def generate_service(rng: random.Random, wedding_date: date, guest_count: int, index: int | None = None) -> dict[str, Any]:
    name, category, vendor, low, high, rule = SERVICES[index if index is not None else rng.randrange(len(SERVICES))]
    quantity = {"one": 1, "pages": rng.randint(4, 12), "guests": guest_count}[rule]
    offset_days = 0 if category in ("Venue", "Photography", "Officiant", "Catering", "Floral & Decor") else -rng.randint(1, 30)
    return {
        "Service_Name": name,
        "Service_Category": category,
        "Vendor_Name": vendor,
        "Service_Date": (wedding_date + timedelta(days=offset_days)).isoformat(),
        "Quantity": quantity,
        "Unit_Price": float(_money(rng.uniform(low, high))),
    }


def generate_booking(rng: random.Random, number: int) -> dict[str, Any]:
    p1 = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
    p2 = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
    country, city, typical_type = rng.choice(DESTINATIONS)
    ceremony_type = typical_type if rng.random() < 0.75 else rng.choice(CEREMONY_TYPES)
    wedding_date = date(2027, 1, 1) + timedelta(days=rng.randint(30, 330))
    guest_count = rng.choice((2, 2, 4, 8, 12, 20, 35, 60))
    consultation = datetime.combine(wedding_date - timedelta(days=rng.randint(60, 180)), time(rng.randint(8, 17), rng.choice((0, 30))), tzinfo=timezone.utc)

    # Civil marriages always carry the legal paperwork; the rest is a random mix.
    picks: list[int] = [0, 1, 2] if ceremony_type == "Civil Marriage" else []
    for idx in rng.sample(range(3, len(SERVICES)), k=rng.randint(2, 4)):
        picks.append(idx)
    services = [generate_service(rng, wedding_date, guest_count, idx) for idx in picks]
    total = sum(_money(s["Unit_Price"]) * s["Quantity"] for s in services)
    status = rng.choice(BOOKING_STATUSES[:4])

    return {
        "Name": f"{NAME_PREFIX}{number:04d} {p1.split()[0]} & {p2.split()[0]} - {city}",
        "Partner_1_Name": p1,
        "Partner_2_Name": p2,
        "Contact_Email": f"{_ascii(p1).replace(' ', '.')}@example.com",
        "Ceremony_Type": ceremony_type,
        "Destination_Country": country,
        "Ceremony_City": city,
        "Wedding_Date": wedding_date.isoformat(),
        "Guest_Count": guest_count,
        "Package_Price": float(total),
        "Booking_Status": status,
        "Documents_Verified": status in ("Confirmed", "Completed"),
        "Consultation_At": consultation.isoformat(),
        SUBFORM: services,
    }


def generate_bookings(count: int, seed: int = 42, start_number: int = 1) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    return [generate_booking(rng, start_number + i) for i in range(count)]
