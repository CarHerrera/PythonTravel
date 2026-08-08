import datetime
import glob
import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "flights_BWI_10-31.jsonl")

ORIGIN = "BWI"
DEPARTURE_DATE = "10/31"
RETURN_DATE = "11/07"

# Southwest's own flight-search API (southwest.com/air/booking/) returns a
# 403 whose body states automated access to fare data is against its Terms &
# Conditions (confirmed live, persists even with a headed/Xvfb browser,
# unlike the generic Akamai bot-check on iberostar.com that headed mode gets
# past) — so this deliberately does NOT touch southwest.com at all. Check
# fares yourself in a real browser (southwest.com/air/booking/, BWI ->
# destination, 10/31-11/07 round trip) and type in what you see; this script
# only structures/saves it in the shape app/services/flights.py reads.


def load_destinations():
    """Same hotels_*.jsonl files every other scraper reads, so the
    destination list here always matches what's actually in the app —
    add/remove a hotels file and this list follows automatically."""
    destinations = set()
    for path in glob.glob(os.path.join(DATA_DIR, "hotels_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                destinations.add(json.loads(line)["destination"])
    return sorted(destinations)


def load_existing():
    by_destination = {}
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                by_destination[record["destination"]] = record
    return by_destination


def prompt_price(destination, existing):
    current = existing.get(destination, {})
    current_price = current.get("price_per_person")
    default_label = f" [{current_price}]" if current_price is not None else " [none yet]"
    raw = input(f"{destination} — round-trip price per person (USD){default_label}, blank to keep, 's' to skip: ").strip()
    if raw == "":
        return current_price
    if raw.lower() == "s":
        return current_price
    try:
        return float(raw.replace("$", "").replace(",", ""))
    except ValueError:
        print("  not a number, keeping previous value")
        return current_price


def main():
    destinations = load_destinations()
    existing = load_existing()
    print(f"Enter today's Southwest fares for BWI -> destination, {DEPARTURE_DATE} - {RETURN_DATE}.")
    print("Check southwest.com/air/booking/ yourself and type in what you see. Enter/blank keeps the current value.\n")

    today = datetime.date.today().isoformat()
    records = []
    for destination in destinations:
        price = prompt_price(destination, existing)
        records.append({
            "origin": ORIGIN,
            "destination": destination,
            "departure_date": DEPARTURE_DATE,
            "return_date": RETURN_DATE,
            "price_per_person": price,
            "passengers": 2,
            "currency": "USD",
            "entered_at": today if price is not None else existing.get(destination, {}).get("entered_at"),
            "notes": existing.get(destination, {}).get("notes"),
        })

    tmp_path = OUTPUT_PATH + ".part"
    with open(tmp_path, "w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    os.replace(tmp_path, OUTPUT_PATH)
    filled = sum(1 for r in records if r["price_per_person"] is not None)
    print(f"\nWrote {OUTPUT_PATH} ({filled}/{len(records)} destinations have a price)")


if __name__ == "__main__":
    main()
