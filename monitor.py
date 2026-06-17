#!/usr/bin/env python3
"""
Athlon Flex showroom-monitor (v4).

Haalt de beschikbare auto's uit de Athlon Flex showroom, vergelijkt ze met
jouw verlanglijst (watchlist.json) en stuurt een pushbericht (ntfy en/of
Telegram) zodra een gewenste UITVOERING nieuw verschijnt — ook binnen een
model dat al in de showroom stond.

Per nieuwe uitvoering krijg je de specifieke naam (bijv.
"CLA 180 DCT Business Line 4D 100kW"), het maandbedrag, de bijtelling % en
de fiscale (catalogus)waarde, plus een foto en een directe link naar die auto.
Daarnaast: storing-waarschuwing als de API onbereikbaar is, en een wekelijkse
heartbeat.

Gebouwd op de (onofficiele) athlon-flex-client package:
  https://pypi.org/project/athlon-flex-client/
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode
import urllib.request

from athlon_flex_client.client import AthlonFlexClient, DetailLevel
from athlon_flex_client.models.filters.vehicle_cluster_filter import (
    VehicleClusterFilter,
)

ROOT = Path(__file__).parent
WATCHLIST_PATH = ROOT / "watchlist (1).json"
STATE_PATH = ROOT / "state.json"

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


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ------------------------------------------------------------------- matching
def brand_matches(cluster, watch: dict) -> bool:
    make = (watch.get("make") or "").strip().lower()
    model = (watch.get("model") or "").strip().lower()
    if make and make not in (cluster.make or "").lower():
        return False
    if model and model not in (cluster.model or "").lower():
        return False
    return True


def cluster_matches(cluster, watch: dict) -> bool:
    if not brand_matches(cluster, watch):
        return False
    max_price = watch.get("max_price")
    if max_price is not None and cluster.minPriceInEuroPerMonth is not None:
        if cluster.minPriceInEuroPerMonth > float(max_price):
            return False
    return True


def price_cap_for(cluster, watches: list[dict]) -> float | None:
    caps: list[float] = []
    for w in watches:
        if brand_matches(cluster, w):
            mp = w.get("max_price")
            if mp is None:
                return None
            caps.append(float(mp))
    return max(caps) if caps else None


def vkey(cluster, v) -> str:
    """Stabiele sleutel per uitvoering: merk|model|type (niet de instance-id)."""
    make = v.make or cluster.make
    model = v.model or cluster.model
    typ = (v.type or "").strip()
    return f"{make}|{model}|{typ}"


# ------------------------------------------------------------------- helpers
def deeplink(make: str, model: str, vehicle_id) -> str:
    if not vehicle_id:
        return SHOWROOM_URL
    seg = lambda s: quote(str(s), safe="")
    return f"https://flex.athlon.com/app/showroom/{seg(make)}/{seg(model)}/{seg(vehicle_id)}"


def absolutize(uri: str | None) -> str | None:
    if not uri:
        return None
    if uri.startswith("http"):
        return uri
    if uri.startswith("//"):
        return "https:" + uri
    if uri.startswith("/"):
        return "https://flex.athlon.com" + uri
    return None


def euro(value, suffix: str = "") -> str:
    return (f"\u20ac{value:,.0f}{suffix}").replace(",", ".")


# --------------------------------------------------------------------- athlon
def make_client() -> AthlonFlexClient:
    username = os.environ.get("ATHLON_USERNAME") or None
    password = os.environ.get("ATHLON_PASSWORD") or None
    income = os.environ.get("GROSS_YEARLY_INCOME")
    kwargs: dict = {}
    if username and password:
        kwargs["email"] = username
        kwargs["password"] = password
        if income:
            kwargs["gross_yearly_income"] = float(income)
    return AthlonFlexClient(**kwargs)


async def fetch_clusters(client: AthlonFlexClient, pricing: dict):
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


def variant_info(cluster, v) -> dict:
    return {
        "make": v.make or cluster.make,
        "model": v.model or cluster.model,
        "type": (v.type or "").strip(),
        "price": v.priceInEuroPerMonth
        if v.priceInEuroPerMonth is not None
        else cluster.minPriceInEuroPerMonth,
        "addition": v.additionPercentage
        if v.additionPercentage is not None
        else getattr(cluster, "additionPercentage", None),
        "fiscal": v.fiscalValueInEuro
        if v.fiscalValueInEuro is not None
        else getattr(cluster, "fiscalValueInEuro", None),
        "year": v.modelYear or getattr(cluster, "latestModelYear", None),
        "image": absolutize(v.imageUri) or absolutize(getattr(cluster, "imageUri", None)),
        "link": deeplink(v.make or cluster.make, v.model or cluster.model, v.id),
        "is_electric": v.isElectric,
    }


# --------------------------------------------------------------------- notify
def _send(url: str, data: bytes | None = None, headers: dict | None = None) -> int:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def notify(
    title: str,
    body_lines: list[str],
    url: str | None,
    image_url: str | None = None,
    tags: str = "red_car",
    priority: str = "high",
) -> None:
    sent = False
    quiet = priority in ("low", "min")

    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {
            "Title": title.encode("ascii", "ignore").decode().strip() or "Athlon Flex",
            "Priority": priority,
            "Tags": tags,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if url:
            headers["Click"] = url
        if image_url:
            headers["Attach"] = image_url
        if os.environ.get("NTFY_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
        body = "\n".join(body_lines) + (f"\n\n{url}" if url else "")
        try:
            _send(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers)
            sent = True
            print(f"ntfy: verstuurd ({title.strip()})")
        except Exception as exc:  # noqa: BLE001
            print(f"ntfy: MISLUKT ({exc})", file=sys.stderr)

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        caption = f"<b>{html.escape(title.strip())}</b>\n\n" + "\n".join(
            html.escape(line) for line in body_lines
        )
        if url:
            caption += f'\n\n<a href="{html.escape(url)}">Bekijk in de showroom</a>'
        text_payload = {
            "chat_id": tg_chat,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "disable_notification": "true" if quiet else "false",
        }
        try:
            if image_url:
                photo_payload = {
                    "chat_id": tg_chat,
                    "photo": image_url,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "disable_notification": "true" if quiet else "false",
                }
                _send(
                    f"https://api.telegram.org/bot{tg_token}/sendPhoto",
                    data=urlencode(photo_payload).encode(),
                )
            else:
                _send(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    data=urlencode(text_payload).encode(),
                )
            sent = True
            print(f"telegram: verstuurd ({title.strip()})")
        except Exception as exc:  # noqa: BLE001
            try:
                _send(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    data=urlencode(text_payload).encode(),
                )
                sent = True
                print(f"telegram: foto faalde, tekst verstuurd ({title.strip()})")
            except Exception as exc2:  # noqa: BLE001
                print(f"telegram: MISLUKT ({exc2})", file=sys.stderr)

    if not sent:
        print(
            "WAARSCHUWING: geen notificatiekanaal geconfigureerd. Zet NTFY_TOPIC "
            "en/of TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.",
            file=sys.stderr,
        )


# ----------------------------------------------------------------- berichten
def variant_message(info: dict) -> tuple[str, list[str]]:
    title = f"Nieuw: {info['make']} {info['model']}"
    variant = f"{info['model']} {info['type']}".strip()
    lines = [variant]

    bits: list[str] = []
    if info.get("price"):
        bits.append(euro(info["price"], "/mnd"))
    addition = info.get("addition")
    if addition is not None:
        pct = addition * 100 if addition <= 1 else addition
        bits.append(f"bijtelling {pct:.0f}%")
    if info.get("year"):
        bits.append(str(info["year"]))
    if bits:
        lines.append(" \u00b7 ".join(bits))

    if info.get("fiscal"):
        lines.append(f"Fiscale waarde: {euro(info['fiscal'])}")
    return title, lines


def summary_line(info: dict) -> str:
    name = f"{info['make']} {info['model']} {info['type']}".strip()
    if info.get("price"):
        name += f" \u00b7 {euro(info['price'], '/mnd')}"
    return name


# ----------------------------------------------------------------------- main
def heartbeat_due(last: str | None, days: int) -> bool:
    if not last:
        return False
    try:
        return (date.today() - date.fromisoformat(last)).days >= days
    except ValueError:
        return False


async def main_async() -> int:
    cfg = load_watchlist()
    watches = cfg.get("watches", [])
    pricing = cfg.get("pricing", {})
    settings = cfg.get("settings", {})
    max_individual = int(settings.get("max_individual_notifications", 8))
    heartbeat_days = int(settings.get("heartbeat_days", 7))
    outage_threshold = int(settings.get("outage_threshold", 3))
    exclude_ev = bool(settings.get("exclude_electric", False))

    if not watches:
        print("Verlanglijst is leeg \u2014 vul watchlist.json.", file=sys.stderr)
        return 0

    state = load_state()
    client = make_client()

    try:
        # ---- ophalen (storing-detectie) -----------------------------------
        try:
            clusters = await fetch_clusters(client, pricing)
        except Exception as exc:  # noqa: BLE001
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            print(
                f"Athlon API onbereikbaar (poging {state['consecutive_failures']}): {exc}",
                file=sys.stderr,
            )
            if state["consecutive_failures"] >= outage_threshold and not state.get(
                "outage_alerted"
            ):
                mins = state["consecutive_failures"] * 15
                notify(
                    "\u26a0\ufe0f Athlon-monitor: mogelijke storing",
                    [
                        f"De showroom-API reageert al {state['consecutive_failures']} "
                        f"checks niet (\u00b1{mins} min).",
                        "Je krijgt geen auto-meldingen tot het herstelt.",
                    ],
                    SHOWROOM_URL,
                    tags="warning",
                )
                state["outage_alerted"] = True
            save_state(state)
            return 0

        # ---- succes -------------------------------------------------------
        was_outage = bool(state.get("outage_alerted"))
        state["consecutive_failures"] = 0
        state["outage_alerted"] = False

        matched = [c for c in clusters if any(cluster_matches(c, w) for w in watches)]
        previous = set(state.get("present", []))
        seeding = state.get("present_kind") != "variant"

        # ---- uitvoeringen per matchend model ophalen ----------------------
        current: dict[str, dict] = {}
        carried: set[str] = set()
        for cluster in matched:
            prefix = f"{cluster.make}|{cluster.model}|"
            cap = price_cap_for(cluster, watches)
            try:
                vehicles = await client.vehicles_async(
                    cluster.make, cluster.model, filter_vehicles_by_profile=False
                )
            except Exception as exc:  # noqa: BLE001
                # Tijdelijke fout: behoud de bekende uitvoeringen van dit model,
                # zodat ze niet als "nieuw" terugkomen bij de volgende controle.
                print(
                    f"  (uitvoeringen niet geladen voor {cluster.make} {cluster.model}: {exc})",
                    file=sys.stderr,
                )
                carried.update(k for k in previous if k.startswith(prefix))
                continue
            for v in vehicles:
                if exclude_ev and v.isElectric:
                    continue
                if (
                    cap is not None
                    and v.priceInEuroPerMonth is not None
                    and v.priceInEuroPerMonth > cap
                ):
                    continue
                current[vkey(cluster, v)] = variant_info(cluster, v)

        present = set(current) | carried
        new_keys = present - previous

        print(
            f"{len(clusters)} clusters, {len(matched)} match(es), "
            f"{len(present)} uitvoering(en) gevolgd, {len(new_keys)} nieuw."
        )

        if was_outage:
            notify(
                "\u2705 Athlon-monitor weer online",
                ["De verbinding met de showroom is hersteld; je krijgt weer meldingen."],
                SHOWROOM_URL,
                tags="white_check_mark",
                priority="low",
            )

        if seeding:
            # Eerste run of overstap van een eerdere versie: alles vastleggen,
            # geen stortvloed aan meldingen.
            notify(
                "\u2705 Athlon-monitor actief",
                [
                    f"{len(present)} uitvoering(en) van je merken nu in beeld.",
                    "Je krijgt voortaan een melding zodra er een nieuwe verschijnt.",
                ],
                SHOWROOM_URL,
                tags="white_check_mark",
                priority="low",
            )
            print("Seed-run: uitvoeringen vastgelegd, geen losse meldingen.")
        elif new_keys:
            new_infos = sorted(
                (current[k] for k in new_keys), key=lambda i: (i.get("price") or 1e9)
            )
            if len(new_infos) <= max_individual:
                for info in new_infos:
                    title, lines = variant_message(info)
                    notify(title, lines, info["link"], image_url=info["image"])
            else:
                lines = [summary_line(i) for i in new_infos]
                notify(
                    f"{len(new_infos)} nieuwe uitvoeringen in de showroom",
                    lines,
                    SHOWROOM_URL,
                )
        else:
            print("Geen nieuwe uitvoeringen.")

        # ---- heartbeat ----------------------------------------------------
        today = date.today().isoformat()
        if state.get("last_heartbeat") is None:
            state["last_heartbeat"] = today
        elif not seeding and heartbeat_due(state["last_heartbeat"], heartbeat_days):
            notify(
                "\u2705 Athlon-monitor draait",
                [
                    f"Alles werkt. {len(present)} uitvoering(en) van je merken "
                    f"nu beschikbaar.",
                    "Laatste check OK.",
                ],
                SHOWROOM_URL,
                tags="white_check_mark",
                priority="low",
            )
            state["last_heartbeat"] = today

        state["present"] = sorted(present)
        state["present_kind"] = "variant"
        save_state(state)
        return 0
    finally:
        try:
            session = getattr(client, "session", None)
            if session is not None:
                await session.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
