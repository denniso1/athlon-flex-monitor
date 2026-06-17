#!/usr/bin/env python3
"""
Athlon Flex showroom-monitor.

Haalt de beschikbare auto's uit de Athlon Flex showroom, vergelijkt ze met
jouw verlanglijst (watchlist.json) en stuurt een pushbericht (ntfy en/of
Telegram) zodra een gewenste auto NIEUW verschijnt t.o.v. de vorige controle.

Gebouwd op de (onofficiele) athlon-flex-client package:
  https://pypi.org/project/athlon-flex-client/
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from athlon_flex_client.client import AthlonFlexClient, DetailLevel
from athlon_flex_client.models.filters.vehicle_cluster_filter import (
    VehicleClusterFilter,
)

ROOT = Path(__file__).parent
WATCHLIST_PATH = ROOT / "watchlist.json"
STATE_PATH = ROOT / "state.json"

# Deze link wordt in het bericht gezet zodat je met een tik de showroom opent.
SHOWROOM_URL = os.environ.get(
    "SHOWROOM_URL",
    "https://flex.athlon.com/app/showroom"
    "?includeTaxInPrices=false&numberOfKmPerMonth=1000"
    "&includeMileageCostsInPricing=true&includeFuelCostsInPricing=true",
)


# --------------------------------------------------------------------- config
def load_watchlist() -> dict:
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_state() -> set[str]:
    """De set auto's die bij de vorige run aanwezig was."""
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return set(data.get("present", []))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_state(present: set[str]) -> None:
    STATE_PATH.write_text(
        json.dumps({"present": sorted(present)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ------------------------------------------------------------------- matching
def cluster_matches(cluster, watch: dict) -> bool:
    """Matcht een auto-cluster met een enkele verlanglijst-regel."""
    make = (watch.get("make") or "").strip().lower()
    model = (watch.get("model") or "").strip().lower()
    if make and make not in (cluster.make or "").lower():
        return False
    if model and model not in (cluster.model or "").lower():
        return False
    max_price = watch.get("max_price")
    if max_price is not None and cluster.minPriceInEuroPerMonth is not None:
        if cluster.minPriceInEuroPerMonth > float(max_price):
            return False
    return True


def key(cluster) -> str:
    """Stabiele sleutel per cluster (merk + model)."""
    return f"{cluster.make} {cluster.model}".strip()


# --------------------------------------------------------------------- athlon
async def fetch_clusters(pricing: dict):
    username = os.environ.get("ATHLON_USERNAME") or None
    password = os.environ.get("ATHLON_PASSWORD") or None
    income = os.environ.get("GROSS_YEARLY_INCOME")

    kwargs: dict = {}
    if username and password:
        # Optioneel: ingelogd levert netto maandbedragen op basis van jouw
        # leasebudget. Niet nodig om beschikbaarheid te zien.
        kwargs["email"] = username
        kwargs["password"] = password
        if income:
            kwargs["gross_yearly_income"] = int(income)

    client = AthlonFlexClient(**kwargs)

    # Zelfde prijsinstellingen als in jouw showroom-URL.
    filt = VehicleClusterFilter(
        IncludeTaxInPrices=pricing.get("include_tax_in_prices", False),
        NumberOfKmPerMonth=pricing.get("number_of_km_per_month", 1000),
        IncludeMileageCostsInPricing=pricing.get(
            "include_mileage_costs_in_pricing", True
        ),
        IncludeFuelCostsInPricing=pricing.get(
            "include_fuel_costs_in_pricing", True
        ),
    )

    result = await client.vehicle_clusters_async(
        filter_=filt, detail_level=DetailLevel.CLUSTER_ONLY
    )
    return result.vehicle_clusters


# --------------------------------------------------------------------- notify
def _send(url: str, data: bytes | None = None, headers: dict | None = None) -> int:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def notify(title: str, body_lines: list[str], url: str) -> None:
    """Verstuurt naar elk geconfigureerd kanaal (ntfy en/of Telegram)."""
    sent = False

    # --- ntfy ---------------------------------------------------------------
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {
            # ntfy-headers moeten ASCII zijn; de body mag wel UTF-8 zijn.
            "Title": title.encode("ascii", "ignore").decode() or "Athlon Flex",
            "Priority": "high",
            "Tags": "red_car",
            "Click": url,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if os.environ.get("NTFY_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
        body = "\n".join(body_lines) + f"\n\n{url}"
        try:
            _send(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers)
            sent = True
            print(f"ntfy: bericht verstuurd naar topic '{topic}'")
        except Exception as exc:  # noqa: BLE001
            print(f"ntfy: MISLUKT ({exc})", file=sys.stderr)

    # --- Telegram -----------------------------------------------------------
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        safe = "\n".join(html.escape(line) for line in body_lines)
        text = (
            f"<b>{html.escape(title)}</b>\n\n{safe}"
            f'\n\n<a href="{html.escape(url)}">Open de showroom</a>'
        )
        data = urllib.parse.urlencode(
            {
                "chat_id": tg_chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode()
        try:
            _send(f"https://api.telegram.org/bot{tg_token}/sendMessage", data=data)
            sent = True
            print("telegram: bericht verstuurd")
        except Exception as exc:  # noqa: BLE001
            print(f"telegram: MISLUKT ({exc})", file=sys.stderr)

    if not sent:
        print(
            "WAARSCHUWING: geen notificatiekanaal geconfigureerd. Zet NTFY_TOPIC "
            "en/of TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.",
            file=sys.stderr,
        )


# ----------------------------------------------------------------------- main
def format_line(cluster) -> str:
    price = cluster.minPriceInEuroPerMonth
    if price:
        price_str = f"vanaf \u20ac{price:,.0f}/mnd".replace(",", ".")
    else:
        price_str = "prijs onbekend"
    count = getattr(cluster, "vehicleCount", None)
    count_str = f" \u2014 {count} beschikbaar" if count else ""
    year = getattr(cluster, "latestModelYear", None)
    year_str = f" ({year})" if year else ""
    return f"{cluster.make} {cluster.model}{year_str} \u00b7 {price_str}{count_str}"


async def main_async() -> int:
    cfg = load_watchlist()
    watches = cfg.get("watches", [])
    if not watches:
        print(
            "Verlanglijst is leeg \u2014 niets om op te letten. Vul watchlist.json.",
            file=sys.stderr,
        )
        return 0
    pricing = cfg.get("pricing", {})

    try:
        clusters = await fetch_clusters(pricing)
    except Exception as exc:  # noqa: BLE001
        # Tijdelijke fout (netwerk/site): niet luid falen, gewoon volgende keer.
        print(f"Kon de Athlon API niet bereiken: {exc}", file=sys.stderr)
        return 0

    matched = [c for c in clusters if any(cluster_matches(c, w) for w in watches)]
    present = {key(c) for c in matched}
    previous = load_state()
    new = present - previous

    print(
        f"{len(clusters)} clusters opgehaald, {len(matched)} match(es), "
        f"{len(new)} nieuw t.o.v. vorige controle."
    )

    if new:
        new_clusters = sorted(
            (c for c in matched if key(c) in new),
            key=lambda c: (c.minPriceInEuroPerMonth or 1e9),
        )
        lines = [format_line(c) for c in new_clusters]
        title = (
            "Nieuwe auto in de Athlon showroom!"
            if len(lines) == 1
            else f"{len(lines)} nieuwe auto's in de Athlon showroom!"
        )
        notify(title, lines, SHOWROOM_URL)
    else:
        print("Geen nieuwe gewenste auto's.")

    save_state(present)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
