"""Today's TCGplayer prices for the products in products.toml, added to the job's memory, and set against the
history kept there: the facts the model weighs, and which of the alert rules they meet.

    python3 scripts/prices.py check [--products scripts/products.toml] [--memory memory/prices.json]

Standard library only. One plain GET per product to TCGplayer's product details (what the product page itself
shows), from a client that says it is a script. Nothing else is fetched: no price history (TCGplayer refuses it
to scripts), no listings. So the history is this job's own, one reading a day, and the rules that need history
start to say something after a few days.

The rules (products.toml's [rules], with these defaults), each on market price and on the lowest listing with
shipping, which is what buying one costs:

- below:   the lowest listing with shipping is under the product's `below` price.
- bottomed: the low of the last `window_days` came after a fall of more than `bottom_within` (a dip, not a flat
            line), was set before the last `bottom_days` readings, and each of those is within `bottom_within`
            above it: it stopped falling and has held near its low.
- jump:    up `jump` or more since the reading the day before.

A rule that held on the reading before as well says so ("still"), so a model can tell news from what it has
heard. What the readings are worth is the model's to judge; this only counts.
"""

import argparse
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

DETAILS = "https://mp-search-api.tcgplayer.com/v2/product/{id}/details"
PAGE = "https://www.tcgplayer.com/product/{id}"
AGENT = "burst-harness-price-watch/1 (a script: one request per product per run)"
RULES = {"bottom_within": 0.05, "bottom_days": 3, "jump": 0.10, "window_days": 90}
KEEP = 400          # readings kept per product, one a day
SHOWN = 14          # readings shown in each product's table
MEASURES = {"market": "market price", "lowest_shipped": "lowest listing with shipping"}


class Refused(RuntimeError):
    """products.toml is not what this script reads."""


def load_products(path: Path) -> tuple[list[dict], dict]:
    try:
        values = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise Refused(f"{path}: {err}") from err
    rules = {**RULES, **values.get("rules", {})}
    if unknown := sorted(set(rules) - set(RULES)):
        raise Refused(f"{path}: unknown [rules]: {', '.join(unknown)}")
    products = values.get("product", [])
    if not products:
        raise Refused(f"{path}: no [[product]] to watch")
    for p in products:
        if not isinstance(p.get("id"), int):
            raise Refused(f"{path}: each [[product]] has an id, the number in its address ({p!r})")
        if "below" in p and not isinstance(p["below"], (int, float)):
            raise Refused(f"{path}: product {p['id']}'s below is a price ({p['below']!r})")
        if unknown := sorted(set(p) - {"id", "name", "below", "note"}):
            raise Refused(f"{path}: product {p['id']}: unknown setting(s) {', '.join(unknown)}")
    return products, rules


def fetch(product_id: int) -> dict:
    request = urllib.request.Request(DETAILS.format(id=product_id),
                                     headers={"Accept": "application/json", "User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _money(value) -> float | None:
    return round(float(value), 2) if isinstance(value, (int, float)) else None


def reading(details: dict, at: str) -> dict:
    return {"date": at[:10], "at": at,
            "market": _money(details.get("marketPrice")),
            "lowest": _money(details.get("lowestPrice")),
            "lowest_shipped": _money(details.get("lowestPriceWithShipping")),
            "median": _money(details.get("medianPrice")),
            "listings": int(details["listings"]) if isinstance(details.get("listings"), (int, float)) else None,
            "sellers": int(details["sellers"]) if isinstance(details.get("sellers"), (int, float)) else None}


def add(readings: list[dict], new: dict) -> list[dict]:
    """One reading a day: a second run on the same day replaces the first."""
    return ([r for r in readings if r["date"] != new["date"]] + [new])[-KEEP:]


def _days(a: str, b: str) -> int:
    return round((time.mktime(time.strptime(b, "%Y-%m-%d")) - time.mktime(time.strptime(a, "%Y-%m-%d"))) / 86400)


def signals(readings: list[dict], measure: str, rules: dict, below: float | None) -> dict[str, str]:
    """The rules the newest reading meets on one measure: {rule: what it is}."""
    series = [r for r in readings if r.get(measure) is not None]
    if not series:
        return {}
    now = series[-1]
    value = now[measure]
    found = {}
    if below is not None and measure == "lowest_shipped" and value < below:
        found["below"] = f"${value:.2f} is under your ${below:.2f}"
    window = [r for r in series if _days(r["date"], now["date"]) <= rules["window_days"]]
    low = min(window, key=lambda r: r[measure])
    held = int(rules["bottom_days"])
    last = window[-held:]
    fell = [r[measure] for r in window[:window.index(low)]]  # a dip, not a flat line: it fell into the low
    if (len(window) > held and window.index(low) < len(window) - held
            and fell and max(fell) > low[measure] * (1 + rules["bottom_within"])
            and all(r[measure] <= low[measure] * (1 + rules["bottom_within"]) for r in last)):
        found["bottomed"] = (f"held within {rules['bottom_within']:.0%} of its low (${low[measure]:.2f} on "
                             f"{low['date']}) for the last {held} readings")
    if len(series) > 1 and _days(series[-2]["date"], now["date"]) <= 2 and series[-2][measure]:
        change = value / series[-2][measure] - 1
        if change >= rules["jump"]:
            found["jump"] = f"up {change:.0%} since {series[-2]['date']} (${series[-2][measure]:.2f})"
    return found


def change(readings: list[dict], measure: str, days: int) -> str:
    """The change on a measure over about `days`, from the newest reading at least that old."""
    now = readings[-1]
    older = [r for r in readings if _days(r["date"], now["date"]) >= days and r.get(measure) is not None]
    if not older or now.get(measure) is None:
        return "n/a"
    then = older[-1][measure]
    return f"{now[measure] / then - 1:+.1%} (from ${then:.2f} on {older[-1]['date']})" if then else "n/a"


def describe(product: dict, entry: dict, rules: dict) -> list[str]:
    readings = entry["readings"]
    now = readings[-1]
    name = product.get("name") or entry.get("name") or f"product {product['id']}"
    lines = [f"## {name}", f"{PAGE.format(id=product['id'])}"
             + (f" ({entry['set']})" if entry.get("set") else "")]
    if product.get("below") is not None:
        lines.append(f"Your price: under ${product['below']:.2f}, for the lowest listing with shipping.")
    else:
        lines.append("Your price: none set; watching for a dip and a turn.")
    if product.get("note"):
        lines.append(f"Your note: {product['note']}")
    money = lambda v: f"${v:.2f}" if v is not None else "n/a"  # noqa: E731
    lines.append(f"Now ({now['at']}): market {money(now['market'])}, lowest listing {money(now['lowest'])}, "
                 f"lowest with shipping {money(now['lowest_shipped'])}, median {money(now['median'])}, "
                 f"{now['listings']} listings from {now['sellers']} sellers.")
    lines.append(f"Readings kept: {len(readings)}, since {readings[0]['date']}.")
    for measure, label in MEASURES.items():
        lines.append(f"{label.capitalize()}: 1 day {change(readings, measure, 1)}; 7 days "
                     f"{change(readings, measure, 7)}; 30 days {change(readings, measure, 30)}.")
    fired = False
    for measure, label in MEASURES.items():
        today = signals(readings, measure, rules, product.get("below"))
        before = signals(readings[:-1], measure, rules, product.get("below")) if len(readings) > 1 else {}
        for rule, what in today.items():
            fired = True
            lines.append(f"Rule {rule} ({'still, as on the reading before' if rule in before else 'new today'}), "
                         f"{label}: {what}.")
    if not fired:
        lines.append("Rules met: none.")
    lines += ["", "| date | market | lowest | lowest+ship | median | listings |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['date']} | {money(r['market'])} | {money(r['lowest'])} | {money(r['lowest_shipped'])} | "
              f"{money(r['median'])} | {r['listings']} |" for r in readings[-SHOWN:]]
    return lines


def check(products: list[dict], rules: dict, memory: Path, *, now: str | None = None,
          get=fetch) -> tuple[str, int]:
    """Read each product, keep the reading, and describe them all. Returns the text and how many failed."""
    at = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        kept = json.loads(memory.read_text())
    except (OSError, ValueError):
        kept = {}
    known = kept.setdefault("products", {})
    out, failed = [f"# Prices at {at}", ""], 0
    for product in products:
        key = str(product["id"])
        entry = known.setdefault(key, {"readings": []})
        try:
            details = get(product["id"])
            entry["name"], entry["set"] = details.get("productName") or entry.get("name"), details.get("setName")
            entry["readings"] = add(entry["readings"], reading(details, at))
            entry.pop("error", None)
        except (urllib.error.URLError, OSError, ValueError) as err:
            failed += 1
            why = f"HTTP {err.code}" if isinstance(err, urllib.error.HTTPError) else str(err)
            entry["error"] = {"at": at, "why": why, "since": (entry.get("error") or {}).get("since", at)}
            out += [f"## {product.get('name') or entry.get('name') or product['id']}",
                    f"Could not be read: {why} (failing since {entry['error']['since']}). "
                    + ("Its last reading is below." if entry["readings"] else "There is no reading of it yet."), ""]
            if not entry["readings"]:
                continue
        out += describe(product, entry, rules) + [""]
    # Products no longer in products.toml are forgotten.
    kept["products"] = {k: v for k, v in known.items() if k in {str(p["id"]) for p in products}}
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text(json.dumps(kept, indent=1))
    return "\n".join(out).rstrip() + "\n", failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("check", help="read today's prices, keep them, and say what they mean")
    run.add_argument("--products", type=Path, default=Path(__file__).with_name("products.toml"))
    run.add_argument("--memory", type=Path, default=Path("memory/prices.json"))
    args = parser.parse_args(argv)
    try:
        products, rules = load_products(args.products)
    except Refused as err:
        print(err, file=sys.stderr)
        return 2
    text, failed = check(products, rules, args.memory)
    print(text, end="")
    return 1 if failed == len(products) else 0


if __name__ == "__main__":
    sys.exit(main())
