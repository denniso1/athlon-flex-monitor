#!/usr/bin/env python3
"""
Athlon Flex showroom-monitor (v5).

Wijzigingen t.o.v. v4:
  * LOOP-MODUS. Een enkele draai blijft uren doorchecken met een korte
    tussenpoos. Daardoor hangt de meetfrequentie niet meer af van de GitHub
    cron, die in de praktijk maar een fractie van de geplande runs start.
  * MELDING BEVESTIGD VOOR OPSLAG. Een uitvoering wordt pas als "gezien"
    weggeschreven nadat de melding echt verstuurd is. Mislukt het versturen,
    dan blijft de auto nieuw en probeert de volgende ronde het opnieuw.
  * VOORRANGSAUTO'S. Trefwoorden uit de watchlist krijgen ntfy-prioriteit
    urgent, zodat je telefoon ze ook op stil doorlaat.
  * DEAD MAN SWITCH. Optionele ping naar healthchecks.io na elke geslaagde
    ronde, zodat stilte zelf alarm geeft.

Gebouwd op de (onofficiele) athlon-flex-client package.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
import urllib.request

from athlon_flex_client.client import AthlonFlexClient, DetailLevel
from athlon_flex_client.models.filters.vehicle_cluster_filter import (
    VehicleClusterFilter,
)

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state.json"


def watchlist_path() -> Path:
    """Accepteer zowel de nette naam als de oude naam met spatie."""
    for name in ("watchlist.json", "watchlist (1).json"):
        p = ROOT / name
        if p.exists():
            return p
    raise FileNotFoundError("Geen watchlist.json gevonden naast monitor.py")


SHOWROOM_URL = os.environ.get(
    "SHOWROOM_URL",
    "https://flex.athlon.com/app/showroom"
    "?includeTaxInPrices=false&numberOfKmPerMonth=1000"
    "&includeMileageCostsInPricing=true&includeFuelCostsInPricing=true",
)


def log(msg: str, err: bool = False) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", file=sys.stderr if err else sys.stdout, flush=True)


# --------------------------------------------------------------------- config
def load_watchlist() -> dict:
    with open(watchlist_path(), encoding="utf-8") as f:
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


def commit_state() -> None:
    """Push state.json terug naar de repo. Alleen actief in CI."""
    if os.environ.get("GIT_PERSIST") != "1":
        return
    try:
        changed = subprocess.run(
            ["git", "status", "--porcelain", "state.json"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        if not changed:
            return
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

        def run(*args):
            return subprocess.run(
                args, cwd=ROOT, capture_output=True, text=True, timeout=120, env=env
            )

        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email",
            "github-actions[bot]@users.noreply.github.com")
        run("git", "add", "state.json")
        run("git", "commit", "-m", "Update showroom-status [skip ci]")
        run("git", "pull", "--rebase", "--autostash", "--quiet")
        res = run("git", "push", "--quiet")
        if res.returncode != 0:
            log(f"git push mislukt: {res.stderr.strip()[:200]}", err=True)
    except Exception as exc:  # noqa: BLE001
        log(f"git persist mislukt: {exc}", err=True)


def ping_healthcheck(suffix: str = "") -> None:
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        urllib.request.urlopen(url.rstrip("/") + suffix, timeout=15).close()
    except Exception:  # noqa: BLE001
        pass


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


def is_priority(key: str, keywords: list[str]) -> bool:
    low = key.lower()
    return any(all(part in low for part in kw.lower().split()) for kw in keywords)


# ------------------------------------------------------------------- helpers
def deeplink(make: str, model: str, vehicle_id) -> str:
    if not vehicle_id:
        return SHOWROOM_URL

    def seg(s):
        return quote(str(s), safe="")

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
) -> bool:
    """Verstuurt de melding. Geeft True terug als minstens een kanaal lukte."""
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
        if image_url and image_url.isascii():
            headers["Attach"] = image_url
        if os.environ.get("NTFY_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
        body = "\n".join(body_lines) + (f"\n\n{url}" if url else "")
        try:
            _send(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers)
            sent = True
            log(f"ntfy: verstuurd ({title.strip()})")
        except Exception as exc:  # noqa: BLE001
            log(f"ntfy: MISLUKT ({type(exc).__name__}: {exc})", err=True)

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
            log(f"telegram: verstuurd ({title.strip()})")
        except Exception:  # noqa: BLE001
            try:
                _send(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    data=urlencode(text_payload).encode(),
                )
                sent = True
                log(f"telegram: foto faalde, tekst verstuurd ({title.strip()})")
            except Exception as exc2:  # noqa: BLE001
                log(f"telegram: MISLUKT ({exc2})", err=True)

    if not sent:
        log(
            "WAARSCHUWING: melding NIET bezorgd. Controleer NTFY_TOPIC en/of "
            "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.",
            err=True,
        )
    return sent


# ----------------------------------------------------------------- berichten
def variant_message(info: dict, urgent: bool = False) -> tuple[str, list[str]]:
    prefix = "TOPPER" if urgent else "Nieuw"
    title = f"{prefix}: {info['make']} {info['model']}"
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
    if urgent:
        lines.append("Staat op je verlanglijst. Nu boeken.")
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


async def check_once(client: AthlonFlexClient, cfg: dict) -> None:
    """Een enkele controleronde. Werkt state bij en verstuurt meldingen."""
    watches = cfg.get("watches", [])
    pricing = cfg.get("pricing", {})
    settings = cfg.get("settings", {})
    max_individual = int(settings.get("max_individual_notifications", 8))
    heartbeat_days = int(settings.get("heartbeat_days", 7))
    outage_threshold = int(settings.get("outage_threshold", 3))
    exclude_ev = bool(settings.get("exclude_electric", False))
    priority_keywords = settings.get("priority_keywords", []) or []
    interval_min = max(1, int(os.environ.get("INTERVAL_SECONDS", "120")) // 60)

    if not watches:
        log("Verlanglijst is leeg, vul watchlist.json.", err=True)
        return

    state = load_state()

    # ---- ophalen (storing-detectie) ---------------------------------------
    try:
        clusters = await fetch_clusters(client, pricing)
    except Exception as exc:  # noqa: BLE001
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        log(f"Athlon API onbereikbaar (poging {state['consecutive_failures']}): {exc}",
            err=True)
        if state["consecutive_failures"] >= outage_threshold and not state.get(
            "outage_alerted"
        ):
            mins = state["consecutive_failures"] * interval_min
            if notify(
                "Athlon-monitor: mogelijke storing",
                [
                    f"De showroom-API reageert al {state['consecutive_failures']} "
                    f"checks niet (circa {mins} min).",
                    "Je krijgt geen auto-meldingen tot het herstelt.",
                ],
                SHOWROOM_URL,
                tags="warning",
            ):
                state["outage_alerted"] = True
        save_state(state)
        commit_state()
        return

    # ---- succes ------------------------------------------------------------
    was_outage = bool(state.get("outage_alerted"))
    state["consecutive_failures"] = 0
    state["outage_alerted"] = False

    matched = [c for c in clusters if any(cluster_matches(c, w) for w in watches)]
    previous = set(state.get("present", []))
    seeding = state.get("present_kind") != "variant"

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
            log(f"  (uitvoeringen niet geladen voor {cluster.make} "
                f"{cluster.model}: {exc})", err=True)
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

    log(f"{len(clusters)} clusters, {len(matched)} match(es), "
        f"{len(present)} uitvoering(en) gevolgd, {len(new_keys)} nieuw.")

    if was_outage:
        notify(
            "Athlon-monitor weer online",
            ["De verbinding met de showroom is hersteld."],
            SHOWROOM_URL, tags="white_check_mark", priority="low",
        )

    # ---- meldingen, en pas daarna opslaan ----------------------------------
    undelivered: set[str] = set()

    if seeding:
        notify(
            "Athlon-monitor actief",
            [
                f"{len(present)} uitvoering(en) van je merken nu in beeld.",
                "Je krijgt voortaan een melding zodra er een nieuwe verschijnt.",
            ],
            SHOWROOM_URL, tags="white_check_mark", priority="low",
        )
        log("Seed-run: uitvoeringen vastgelegd, geen losse meldingen.")
    elif new_keys:
        new_infos = sorted(
            ((k, current[k]) for k in new_keys),
            key=lambda kv: (not is_priority(kv[0], priority_keywords),
                            kv[1].get("price") or 1e9),
        )
        if len(new_infos) <= max_individual:
            for key, info in new_infos:
                urgent = is_priority(key, priority_keywords)
                title, lines = variant_message(info, urgent)
                ok = notify(
                    title, lines, info["link"], image_url=info["image"],
                    tags="rotating_light" if urgent else "red_car",
                    priority="urgent" if urgent else "high",
                )
                if not ok:
                    undelivered.add(key)
        else:
            urgent_keys = [k for k, _ in new_infos if is_priority(k, priority_keywords)]
            lines = [summary_line(i) for _, i in new_infos]
            ok = notify(
                f"{len(new_infos)} nieuwe uitvoeringen in de showroom",
                lines, SHOWROOM_URL,
                tags="rotating_light" if urgent_keys else "red_car",
                priority="urgent" if urgent_keys else "high",
            )
            if not ok:
                undelivered.update(k for k, _ in new_infos)
    else:
        log("Geen nieuwe uitvoeringen.")

    if undelivered:
        log(f"{len(undelivered)} melding(en) niet bezorgd, blijven nieuw voor de "
            f"volgende ronde.", err=True)

    # ---- heartbeat ---------------------------------------------------------
    today = date.today().isoformat()
    if state.get("last_heartbeat") is None:
        state["last_heartbeat"] = today
    elif not seeding and heartbeat_due(state["last_heartbeat"], heartbeat_days):
        if notify(
            "Athlon-monitor draait",
            [f"Alles werkt. {len(present)} uitvoering(en) van je merken beschikbaar."],
            SHOWROOM_URL, tags="white_check_mark", priority="low",
        ):
            state["last_heartbeat"] = today

    # Niet bezorgde auto's blijven buiten present, zodat ze nieuw blijven.
    state["present"] = sorted(present - undelivered)
    state["present_kind"] = "variant"
    save_state(state)
    commit_state()
    ping_healthcheck()


async def main_async() -> int:
    loop_minutes = int(os.environ.get("LOOP_MINUTES", "0"))
    interval = max(30, int(os.environ.get("INTERVAL_SECONDS", "120")))
    deadline = time.monotonic() + loop_minutes * 60

    client = make_client()
    rounds = 0
    try:
        while True:
            rounds += 1
            try:
                await check_once(client, load_watchlist())
            except Exception as exc:  # noqa: BLE001
                log(f"Ronde {rounds} viel om: {type(exc).__name__}: {exc}", err=True)
            if loop_minutes <= 0:
                break
            if time.monotonic() + interval >= deadline:
                log(f"Loop klaar na {rounds} ronde(s). De volgende run neemt het over.")
                break
            await asyncio.sleep(interval)
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
