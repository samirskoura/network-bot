#!/usr/bin/env python3
"""Low-cost GitHub Actions worker for reviewing rejected Snapchat ad headlines.

The worker is deliberately conservative:
- each manual live start selects one or more ad squads for the active job;
- scheduled runs continue the complete selected ad-squad list;
- it selects Ads whose review status is REJECTED;
- after a PATCH, it waits for review and can safely recover if a brief PENDING
  transition occurred while the worker was offline;
- each Ad/Creative is handled independently, so another pending Ad does not block it;
- it skips creatives shared with ads outside the selected ad squads;
- it never changes a Creative connected to an approved or pending Ad;
- it keeps retrying confirmed rejections while the workflow remains enabled.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from uuid import UUID

import requests


_ORIGINAL_PRINT = builtins.print
_PUBLIC_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _sanitize_public_log(message: str) -> str:
    """Remove account routing, internal names, headlines, and API details from logs."""
    if message.startswith("Verified target:"):
        return "Verified private target scope; identifiers and internal names hidden."
    if message.startswith("WOULD UPDATE "):
        return "WOULD UPDATE one eligible Creative; private details hidden."
    if message.startswith("UPDATED squad="):
        return "UPDATED one eligible Creative; submitted for re-review."
    if message.startswith("SKIP model item with unknown creative_id:"):
        return "SKIP model item with an unknown private Creative identifier."
    if "OpenAI declined the headline request:" in message:
        return "ERROR: OpenAI declined the headline request; response text hidden."
    message = _PUBLIC_UUID_RE.sub("[private-id]", message)
    message = re.sub(
        r"request_id=[^;\s]+",
        "request_id=[private-request]",
        message,
        flags=re.IGNORECASE,
    )
    if "detail=" in message:
        message = message.split("detail=", 1)[0] + "detail=[hidden]"
    if "must be a valid UUID:" in message:
        message = message.split("must be a valid UUID:", 1)[0] + "must be a valid UUID."
    return message


def _public_print(
    *values: object,
    sep: str = " ",
    end: str = "\n",
    file: Any = None,
    flush: bool = False,
) -> None:
    rendered = sep.join(str(value) for value in values)
    _ORIGINAL_PRINT(
        _sanitize_public_log(rendered),
        end=end,
        file=file,
        flush=flush,
    )


if os.getenv("PUBLIC_SAFE_LOGS", "").strip().lower() in {"1", "true", "yes", "on"}:
    builtins.print = _public_print


SNAP_API = "https://adsapi.snapchat.com/v1"
SNAP_TOKEN_URL = "https://accounts.snapchat.com/login/oauth2/access_token"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
STATE_PATH = Path(__file__).with_name("state.json")
BLOCKED_HEADLINE_HASHES_PATH = Path(__file__).with_name("blocked_headline_hashes.txt")
HEADLINE_POOL_PATH = Path(__file__).with_name("headline_pool.txt")
STATE_VERSION = 9
HARD_MAX_UPDATES_PER_RUN = 30
HARD_MAX_AD_SQUADS = 20
HEADLINE_OPTIONS_PER_CREATIVE = 5
MAX_HEADLINE_GENERATION_ROUNDS = 2
NEAR_DUPLICATE_RATIO = 0.85
MIN_REVIEW_PROPAGATION_SECONDS = 60
MISSED_PENDING_RECOVERY_SECONDS = 300
MAX_SNAP_READ_ATTEMPTS = 4
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
TIMEOUT = 45

INTERNAL_NAME_FIELDS = (
    "campaign_name",
    "ad_squad_name",
    "ad_name",
    "creative_name",
)
INTERNAL_NAME_STOPWORDS = {
    "active",
    "ad",
    "ads",
    "adset",
    "approved",
    "campaign",
    "copy",
    "creative",
    "image",
    "ksa",
    "new",
    "pending",
    "product",
    "rejected",
    "saudi",
    "snap",
    "snapchat",
    "squad",
    "test",
    "video",
    "اعلان",
    "اعلانات",
    "اختبار",
    "السعودية",
    "المتجر",
    "المنتج",
    "تجربة",
    "جديد",
    "جديدة",
    "حملة",
    "سناب",
    "سنابشات",
    "صنف",
    "صورة",
    "فيديو",
    "مجموعة",
    "متجرنا",
    "مرفوض",
    "مرفوضة",
    "منتج",
    "نسخة",
    "نشط",
    "نشطة",
}
LATIN_NAME_DIGRAPHS = {
    "sh": "ش",
    "ch": "تش",
    "kh": "خ",
    "gh": "غ",
    "th": "ث",
    "dh": "ذ",
    "ph": "ف",
}
LATIN_NAME_CHARACTERS = {
    "a": "ا",
    "b": "ب",
    "c": "ك",
    "d": "د",
    "e": "ي",
    "f": "ف",
    "g": "ج",
    "h": "ه",
    "i": "ي",
    "j": "ج",
    "k": "ك",
    "l": "ل",
    "m": "م",
    "n": "ن",
    "o": "و",
    "p": "ب",
    "q": "ق",
    "r": "ر",
    "s": "س",
    "t": "ت",
    "u": "و",
    "v": "ف",
    "w": "و",
    "x": "كس",
    "y": "ي",
    "z": "ز",
}


class BotError(RuntimeError):
    pass


def env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def required_env(name: str) -> str:
    value = env(name)
    if not value:
        raise BotError(f"Missing required GitHub secret or variable: {name}")
    return value


def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise BotError(f"{name} must be a whole number, received: {raw!r}") from exc
    return max(minimum, min(value, maximum))


def parse_ad_squad_ids(raw: str, label: str) -> list[str]:
    """Parse comma, semicolon, whitespace, or newline separated UUIDs."""
    values: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;]+", raw.strip()):
        candidate = token.strip()
        if not candidate:
            continue
        try:
            normalized = str(UUID(candidate))
        except ValueError as exc:
            raise BotError(
                f"{label} contains an invalid Ad Squad UUID: {candidate!r}"
            ) from exc
        if normalized not in seen:
            values.append(normalized)
            seen.add(normalized)
    if len(values) > HARD_MAX_AD_SQUADS:
        raise BotError(
            f"{label} contains {len(values)} Ad Squads; the safe maximum is "
            f"{HARD_MAX_AD_SQUADS}."
        )
    return values


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_headline(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def headline_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean_headline(value)).lower()
    normalized: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if character == "ـ" or category.startswith("M"):
            continue
        normalized.append(character if category[0] in {"L", "N"} else " ")
    return " ".join("".join(normalized).split())


def latin_name_to_arabic(value: str) -> str:
    """Create a conservative Arabic transliteration used only for name blocking."""
    token = re.sub(r"[^a-z]", "", value.lower())
    output: list[str] = []
    position = 0
    while position < len(token):
        pair = token[position : position + 2]
        if pair in LATIN_NAME_DIGRAPHS:
            output.append(LATIN_NAME_DIGRAPHS[pair])
            position += 2
            continue
        output.append(LATIN_NAME_CHARACTERS.get(token[position], ""))
        position += 1
    return "".join(output)


def candidate_internal_name_terms(candidate: dict[str, Any]) -> set[str]:
    """Return internal resource-name terms that must never enter a headline."""
    terms: set[str] = set()
    for field in INTERNAL_NAME_FIELDS:
        normalized_name = headline_key(candidate.get(field))
        if not normalized_name:
            continue
        if len(normalized_name) >= 4 and normalized_name not in INTERNAL_NAME_STOPWORDS:
            terms.add(normalized_name)
        for token in normalized_name.split():
            if len(token) < 4 or token in INTERNAL_NAME_STOPWORDS:
                continue
            terms.add(token)
            if re.fullmatch(r"[a-z0-9]+", token):
                transliterated = headline_key(latin_name_to_arabic(token))
                if len(transliterated) >= 3:
                    terms.add(transliterated)
    return terms


def headline_contains_internal_name(
    headline: str, candidate: dict[str, Any]
) -> bool:
    key = headline_key(headline)
    tokens = set(key.split())
    for term in candidate_internal_name_terms(candidate):
        if " " in term:
            if term in key:
                return True
            continue
        if term in tokens:
            return True
        if len(term) >= 5 and any(
            len(token) >= 5 and (term in token or token in term) for token in tokens
        ):
            return True
    return False


def openai_candidate_payload(
    candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Expose only opaque IDs; never send Snapchat resource names to OpenAI."""
    return [
        {"creative_id": str(candidate.get("creative_id") or "")}
        for candidate in candidates
    ]


def configured_blocked_headline_hashes() -> set[str]:
    if not BLOCKED_HEADLINE_HASHES_PATH.exists():
        return set()
    try:
        lines = BLOCKED_HEADLINE_HASHES_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BotError(f"Cannot read {BLOCKED_HEADLINE_HASHES_PATH.name}: {exc}") from exc
    hashes = {
        line.strip().lower()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }
    if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
        raise BotError(f"{BLOCKED_HEADLINE_HASHES_PATH.name} contains an invalid hash")
    return hashes


def headline_hash_from_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def configured_headline_pool() -> list[str]:
    if not HEADLINE_POOL_PATH.exists():
        return []
    return [
        clean_headline(line)
        for line in HEADLINE_POOL_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def manual_suggestions(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pool = configured_headline_pool()
    return [
        {"creative_id": candidate["creative_id"], "action": "UPDATE", "headlines": pool}
        for candidate in candidates
    ]


def remember_headlines(state: dict[str, Any], values: list[Any]) -> None:
    history = state.setdefault("global_headline_history", [])
    if not isinstance(history, list):
        history = []
    known_keys = {headline_key(item) for item in history if headline_key(item)}
    for value in values:
        headline = clean_headline(value)
        key = headline_key(headline)
        if headline and key and key not in known_keys:
            history.append(headline)
            known_keys.add(key)
    # Keep the complete history. There is no per-Creative attempt limit, so dropping
    # old entries could eventually allow a previously used headline to be reused.
    state["global_headline_history"] = history


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        data: dict[str, Any] = {
            "version": STATE_VERSION,
            "active_ad_account_id": "",
            "active_ad_squad_ids": [],
            "active_jobs": {},
            "creatives": {},
            "global_headline_history": [],
        }
    else:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BotError(f"Cannot read {STATE_PATH.name}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("creatives"), dict):
        raise BotError(f"{STATE_PATH.name} has an invalid structure")
    active_ids = data.get("active_ad_squad_ids")
    if active_ids is not None and not isinstance(active_ids, list):
        raise BotError(f"{STATE_PATH.name} has an invalid active_ad_squad_ids structure")
    active_jobs = data.get("active_jobs")
    if active_jobs is not None and not isinstance(active_jobs, dict):
        raise BotError(f"{STATE_PATH.name} has an invalid active_jobs structure")

    # Migrate the previous one-Ad-Squad state without deleting attempt history.
    if not active_ids:
        legacy_active_job = data.get("active_job")
        if isinstance(legacy_active_job, dict):
            legacy_id = str(legacy_active_job.get("ad_squad_id") or "").strip()
            if legacy_id:
                active_ids = parse_ad_squad_ids(legacy_id, "legacy active_job")
                data["active_ad_squad_ids"] = active_ids
                data["active_jobs"] = {
                    legacy_id: {
                        "ad_squad_id": legacy_id,
                        "started_at": legacy_active_job.get("started_at") or utc_now(),
                    }
                }
    legacy_headlines: list[Any] = []
    for record in data["creatives"].values():
        if not isinstance(record, dict):
            continue
        history = record.get("headline_history", [])
        if isinstance(history, list):
            legacy_headlines.extend(history)
        legacy_headlines.append(record.get("last_headline"))
    remember_headlines(data, legacy_headlines)
    data["version"] = STATE_VERSION
    data.setdefault("active_ad_account_id", "")
    data.setdefault("active_ad_squad_ids", [])
    data.setdefault("active_jobs", {})
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def seconds_since(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def parse_single_uuid(raw: str, label: str) -> str:
    candidate = raw.strip()
    try:
        return str(UUID(candidate))
    except ValueError as exc:
        raise BotError(f"{label} must be a valid UUID: {candidate!r}") from exc


def select_ad_account(
    state: dict[str, Any],
    configured_account_raw: str,
) -> str:
    """Select and persist the one Ad Account allowed for this repository."""
    if not configured_account_raw:
        raise BotError(
            "No Ad Account was configured. Set the GitHub Variable SNAP_AD_ACCOUNT_ID."
        )
    account_id = parse_single_uuid(configured_account_raw, "SNAP_AD_ACCOUNT_ID")
    saved_account = str(state.get("active_ad_account_id") or "").strip()
    if saved_account:
        saved_account = parse_single_uuid(saved_account, "saved active Ad Account")
    if saved_account and saved_account != account_id:
        raise BotError(
            "Safety check failed: state.json belongs to a different Ad Account. "
            "Use a separate repository for each Ad Account."
        )
    state["active_ad_account_id"] = account_id
    print(f"Using configured Ad Account: {account_id}")
    return account_id


def select_active_ad_squads(
    state: dict[str, Any], requested_raw: str, fallback_raw: str
) -> list[str]:
    requested = parse_ad_squad_ids(requested_raw, "ad_squad_ids") if requested_raw else []
    existing = [
        value
        for value in state.get("active_ad_squad_ids", [])
        if isinstance(value, str) and value.strip()
    ]
    existing = parse_ad_squad_ids(",".join(existing), "saved active Ad Squads") if existing else []
    fallback = parse_ad_squad_ids(fallback_raw, "SNAP_AD_SQUAD_IDS") if fallback_raw else []
    selected = requested or existing or fallback
    if not selected:
        raise BotError(
            "No Ad Squad was selected. Enter one or more UUIDs in ad_squad_ids."
        )

    jobs = state.setdefault("active_jobs", {})
    if requested:
        now = utc_now()
        previous = set(existing)
        state["active_ad_squad_ids"] = selected
        state["active_jobs"] = {
            ad_squad_id: {
                "ad_squad_id": ad_squad_id,
                "started_at": (
                    jobs.get(ad_squad_id, {}).get("started_at")
                    if isinstance(jobs.get(ad_squad_id), dict)
                    else None
                )
                or now,
                "last_manual_start_at": now,
            }
            for ad_squad_id in selected
        }
        action = "Continuing" if set(selected) == previous else "Selected"
        print(f"{action} {len(selected)} active Ad Squad job(s):")
    else:
        state["active_ad_squad_ids"] = selected
        print(f"Scheduled run is continuing {len(selected)} active Ad Squad job(s):")

    for ad_squad_id in selected:
        print(f"  - {ad_squad_id}")
    return selected


def response_json(response: requests.Response, label: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok:
        request_id = response.headers.get("x-request-id")
        if not request_id and isinstance(payload, dict):
            request_id = payload.get("request_id")
        detail = payload or response.text[:500] or "no response body"
        if response.status_code == 403:
            raise BotError(
                f"{label} returned 403 Forbidden. Check the Snapchat user's ad-account "
                f"permission and OAuth app scope. request_id={request_id}; detail={detail}"
            )
        raise BotError(
            f"{label} failed with HTTP {response.status_code}. "
            f"request_id={request_id}; detail={detail}"
        )
    if not isinstance(payload, dict):
        raise BotError(f"{label} returned an unexpected response")
    return payload


def refresh_snap_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    response = requests.post(
        SNAP_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=TIMEOUT,
    )
    payload = response_json(response, "Snapchat token refresh")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise BotError("Snapchat token response did not contain access_token")
    return access_token


class SnapClient:
    def __init__(self, access_token: str) -> None:
        self.headers = {"Authorization": f"Bearer {access_token}"}

    def get(self, path_or_url: str, label: str) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{SNAP_API}{path_or_url}"
        for attempt in range(1, MAX_SNAP_READ_ATTEMPTS + 1):
            try:
                response = requests.get(url, headers=self.headers, timeout=TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == MAX_SNAP_READ_ATTEMPTS:
                    raise BotError(
                        f"{label} failed after {attempt} attempts because Snapchat reset "
                        f"the connection: {exc}"
                    ) from exc
                delay = min(2 ** (attempt - 1), 8)
                print(
                    f"RETRY {label}: temporary network error on attempt "
                    f"{attempt}/{MAX_SNAP_READ_ATTEMPTS}; waiting {delay}s."
                )
                time.sleep(delay)
                continue

            if (
                response.status_code in TRANSIENT_HTTP_STATUSES
                and attempt < MAX_SNAP_READ_ATTEMPTS
            ):
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(1, min(int(float(retry_after)), 30))
                except (TypeError, ValueError):
                    delay = min(2 ** (attempt - 1), 8)
                print(
                    f"RETRY {label}: Snapchat returned HTTP {response.status_code} on "
                    f"attempt {attempt}/{MAX_SNAP_READ_ATTEMPTS}; waiting {delay}s."
                )
                time.sleep(delay)
                continue

            return response_json(response, label)

        raise BotError(f"{label} failed after transient retries")

    def get_all(self, path: str, collection: str, entity_key: str) -> list[dict[str, Any]]:
        url = f"{SNAP_API}{path}"
        entities: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        while url and url not in seen_urls:
            seen_urls.add(url)
            payload = self.get(url, f"Snapchat list {collection}")
            wrappers = payload.get(collection, [])
            if not isinstance(wrappers, list):
                raise BotError(f"Snapchat response field {collection!r} was not an array")
            for wrapper in wrappers:
                if not isinstance(wrapper, dict):
                    continue
                entity = wrapper.get(entity_key, wrapper)
                if isinstance(entity, dict):
                    entities.append(entity)
            paging = payload.get("paging") or {}
            next_link = paging.get("next_link") if isinstance(paging, dict) else None
            url = urljoin(url, next_link) if isinstance(next_link, str) and next_link else ""
        return entities

    def one(self, path: str, collection: str, entity_key: str, label: str) -> dict[str, Any]:
        payload = self.get(path, label)
        wrappers = payload.get(collection, [])
        if not isinstance(wrappers, list) or not wrappers:
            raise BotError(f"{label} returned no {entity_key}")
        wrapper = wrappers[0]
        entity = wrapper.get(entity_key, wrapper) if isinstance(wrapper, dict) else None
        if not isinstance(entity, dict):
            raise BotError(f"{label} returned an invalid {entity_key}")
        return entity

    def patch_headline(self, ad_account_id: str, creative_id: str, headline: str) -> dict[str, Any]:
        url = f"{SNAP_API}/adaccounts/{ad_account_id}/creatives/{creative_id}"
        headers = {
            **self.headers,
            "Content-Type": "application/json-patch+json",
        }
        body = [{"op": "replace", "path": "/headline", "value": headline}]
        response = requests.patch(url, headers=headers, json=body, timeout=TIMEOUT)
        return response_json(response, f"Snapchat PATCH creative {creative_id}")


def verify_scope(
    snap: SnapClient,
    ad_account_id: str,
    ad_squad_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ad_squad = snap.one(
        f"/adsquads/{ad_squad_id}", "adsquads", "adsquad", "Get selected ad squad"
    )
    campaign_id = str(ad_squad.get("campaign_id") or "")
    if not campaign_id:
        raise BotError("Selected Ad Squad did not contain a Campaign ID")
    campaign = snap.one(
        f"/campaigns/{campaign_id}",
        "campaigns",
        "campaign",
        "Get parent campaign",
    )
    if str(campaign.get("id") or campaign_id) != campaign_id:
        raise BotError("Safety check failed: the Ad Squad parent Campaign was not returned")
    if str(campaign.get("ad_account_id") or "") != ad_account_id:
        raise BotError(
            "Safety check failed: selected Ad Squad is not inside SNAP_AD_ACCOUNT_ID"
        )
    print(
        "Verified target: "
        f"ad_account={ad_account_id!r}; "
        f"campaign={campaign.get('name', campaign_id)!r}; "
        f"ad_squad={ad_squad.get('name', ad_squad_id)!r}"
    )
    return campaign, ad_squad


def creative_stays_inside_selected_squads(
    creative_id: str,
    selected_ad_squad_ids: set[str],
    all_account_ads: list[dict[str, Any]],
) -> bool:
    linked_squads = {
        str(ad.get("ad_squad_id") or "")
        for ad in all_account_ads
        if ad.get("creative_id") == creative_id and not ad.get("deleted", False)
    }
    linked_squads.discard("")
    return not linked_squads or linked_squads.issubset(selected_ad_squad_ids)


def collect_candidates(
    ad_squad_id: str,
    selected_ad_squad_ids: set[str],
    state: dict[str, Any],
    max_updates: int,
    all_account_ads: list[dict[str, Any]],
    creative_by_id: dict[str, dict[str, Any]],
    globally_seen: set[str],
) -> tuple[list[dict[str, Any]], Counter[str], int]:
    selected_ads = [
        ad
        for ad in all_account_ads
        if str(ad.get("ad_squad_id") or "") == ad_squad_id
    ]
    live_ads = [ad for ad in selected_ads if not ad.get("deleted", False)]
    status_counts = Counter(
        str(ad.get("review_status", "")).upper() or "UNKNOWN"
        for ad in live_ads
    )
    print(
        f"Ad review statuses for {ad_squad_id}: "
        + ", ".join(f"{status}={count}" for status, count in sorted(status_counts.items()))
    )

    state_creatives = state["creatives"]
    for ad in live_ads:
        creative_id = str(ad.get("creative_id") or "")
        record = state_creatives.get(creative_id)
        if not isinstance(record, dict):
            continue
        ad_status = str(ad.get("review_status", "")).upper()
        if ad_status in {"PENDING", "PENDING_REVIEW"} and record.get("awaiting_review"):
            record.setdefault("pending_seen_at", utc_now())
            record["last_observed_review_status"] = ad_status
        elif ad_status == "APPROVED" and (
            record.get("awaiting_review") or record.get("last_review_outcome") != "APPROVED"
        ):
            record["awaiting_review"] = False
            record["last_review_outcome"] = "APPROVED"
            record["last_review_completed_at"] = utc_now()
        elif record.get("awaiting_review"):
            record["last_observed_review_status"] = ad_status or "UNKNOWN"

    rejected = [
        ad
        for ad in live_ads
        if str(ad.get("review_status", "")).upper() == "REJECTED"
        and ad.get("creative_id")
    ]
    print(
        f"Selected Ad Squad {ad_squad_id} contains {len(live_ads)} Ads; "
        f"{len(rejected)} are REJECTED."
    )

    if not rejected:
        return [], status_counts, len(live_ads)
    if max_updates <= 0:
        print(
            f"DEFER {ad_squad_id}: this check reached the {HARD_MAX_UPDATES_PER_RUN}-"
            "creative total limit; rejected Ads remain eligible for the next check."
        )
        return [], status_counts, len(live_ads)

    candidates: list[dict[str, Any]] = []

    for ad in rejected:
        creative_id = str(ad["creative_id"])
        if creative_id in globally_seen:
            continue
        globally_seen.add(creative_id)

        linked_selected_ads = [
            linked_ad
            for linked_ad in all_account_ads
            if linked_ad.get("creative_id") == creative_id
            and not linked_ad.get("deleted", False)
            and str(linked_ad.get("ad_squad_id") or "") in selected_ad_squad_ids
        ]
        linked_statuses = {
            str(linked_ad.get("review_status", "")).upper() or "UNKNOWN"
            for linked_ad in linked_selected_ads
        }
        if "APPROVED" in linked_statuses:
            print(
                f"SKIP {creative_id}: this Creative is connected to an APPROVED Ad; "
                "the approved headline will not be touched."
            )
            continue
        if linked_statuses.intersection({"PENDING", "PENDING_REVIEW"}):
            print(
                f"WAIT {creative_id}: this Creative is still connected to an Ad "
                "under review."
            )
            continue
        if linked_statuses != {"REJECTED"}:
            print(
                f"SKIP {creative_id}: linked Ad status is "
                f"{', '.join(sorted(linked_statuses)) or 'UNKNOWN'}."
            )
            continue

        record = state_creatives.setdefault(creative_id, {})
        if record.get("last_review_outcome") == "APPROVED":
            print(
                f"SKIP {creative_id}: this Creative has already reached an APPROVED "
                "review result; it will never be edited again."
            )
            continue
        attempts = int(record.get("attempts", 0))
        if not creative_stays_inside_selected_squads(
            creative_id, selected_ad_squad_ids, all_account_ads
        ):
            print(
                f"SKIP {creative_id}: this creative is also connected to an ad outside "
                "the selected Ad Squad list."
            )
            continue

        creative = creative_by_id.get(creative_id)
        if not creative:
            print(
                f"SKIP {creative_id}: linked Creative was not returned by the Ad Account."
            )
            continue
        current_headline = clean_headline(creative.get("headline"))
        if record.get("patch_in_flight"):
            planned_headline = clean_headline(record.get("planned_headline"))
            elapsed = seconds_since(record.get("patch_started_at"))
            if planned_headline and current_headline == planned_headline:
                # A previous run may have lost its connection after Snapchat accepted
                # the PATCH. Reconstruct the successful edit from the observed headline
                # before waiting for its review transition.
                previous_headline = clean_headline(
                    record.get("planned_previous_headline")
                )
                history = record.setdefault("headline_history", [])
                if previous_headline and previous_headline not in history:
                    history.append(previous_headline)
                if planned_headline not in history:
                    history.append(planned_headline)
                record["headline_history"] = history
                remember_headlines(state, [previous_headline, planned_headline])
                record["attempts"] = int(record.get("attempts", 0)) + 1
                record["last_headline"] = planned_headline
                record["last_patch_at"] = record.get("patch_started_at") or utc_now()
                record["awaiting_review"] = True
                record["last_review_outcome"] = "PENDING_REVIEW"
                record.pop("pending_seen_at", None)
                record.pop("patch_in_flight", None)
                record.pop("planned_headline", None)
                record.pop("planned_previous_headline", None)
                record.pop("patch_started_at", None)
                print(
                    f"RECOVERED PATCH {creative_id}: Snapchat already has the planned "
                    "headline; waiting for its review transition."
                )
            else:
                wait_seconds = MIN_REVIEW_PROPAGATION_SECONDS
                if elapsed is not None and elapsed >= wait_seconds:
                    wait_seconds = 60
                print(
                    f"WAIT {creative_id}: the previous PATCH result is uncertain; "
                    f"checking again in about {wait_seconds}s before any new edit."
                )
                continue
        creative_status = str(creative.get("review_status", "")).upper()
        if creative_status == "PENDING_REVIEW":
            if record.get("awaiting_review"):
                record.setdefault("pending_seen_at", utc_now())
                record["last_observed_review_status"] = "PENDING_REVIEW"
            print(f"WAIT {creative_id}: creative is still PENDING_REVIEW.")
            continue
        if creative_status == "APPROVED":
            record["awaiting_review"] = False
            record["last_review_outcome"] = "APPROVED"
            record["last_review_completed_at"] = utc_now()
            record["last_review_completion_evidence"] = "CREATIVE_APPROVED"
            print(
                f"SKIP {creative_id}: the Creative is APPROVED; its headline will "
                "never be edited."
            )
            continue
        if creative_status != "DISAPPROVED":
            print(f"SKIP {creative_id}: creative status is {creative_status or 'UNKNOWN'}.")
            continue

        if record.get("awaiting_review"):
            elapsed = seconds_since(record.get("last_patch_at"))
            if elapsed is None or elapsed < MIN_REVIEW_PROPAGATION_SECONDS:
                remaining = (
                    MIN_REVIEW_PROPAGATION_SECONDS
                    if elapsed is None
                    else int(MIN_REVIEW_PROPAGATION_SECONDS - elapsed)
                )
                print(
                    f"WAIT {creative_id}: the headline was just submitted; "
                    f"allow about {max(1, remaining)} more second(s) for status propagation."
                )
                continue

            completion_evidence = "PENDING_OBSERVED"
            if not record.get("pending_seen_at"):
                last_submitted_headline = clean_headline(record.get("last_headline"))
                if not last_submitted_headline or current_headline != last_submitted_headline:
                    print(
                        f"WAIT {creative_id}: PENDING was not observed and the current "
                        "headline does not match the bot's last submitted headline; no "
                        "automatic edit is safe."
                    )
                    continue
                if elapsed < MISSED_PENDING_RECOVERY_SECONDS:
                    remaining = max(
                        1, int(MISSED_PENDING_RECOVERY_SECONDS - elapsed)
                    )
                    print(
                        f"WAIT {creative_id}: PENDING was not observed. The Ad is "
                        "REJECTED and the Creative is DISAPPROVED, but the bot will "
                        f"wait about {remaining} more second(s) before recovering the "
                        "missed transition."
                    )
                    continue
                completion_evidence = "FINAL_STATUSES_AFTER_MISSED_PENDING"
                record["missed_pending_recovered_at"] = utc_now()
                print(
                    f"MISSED PENDING RECOVERED {creative_id}: the submitted headline "
                    "is present and Snapchat now reports final REJECTED/DISAPPROVED "
                    "statuses after the safety delay."
                )
            record["awaiting_review"] = False
            record["last_review_outcome"] = "REJECTED"
            record["last_review_completed_at"] = utc_now()
            record["last_review_completion_evidence"] = completion_evidence
            record.pop("pending_seen_at", None)
            print(
                f"REJECTED AGAIN {creative_id}: review completion is confirmed; "
                "generating the next headline immediately."
            )

        candidates.append(
            {
                "creative_id": creative_id,
                "ad_id": str(ad.get("id") or ""),
                "ad_name": str(ad.get("name") or ""),
                "ad_squad_id": ad_squad_id,
                "creative_name": str(creative.get("name") or ""),
                "current_headline": current_headline,
                "ad_review_reasons": (
                    ad.get("review_status_reasons")
                    or ad.get("review_status_reason")
                    or []
                ),
                "creative_review_reasons": (
                    creative.get("review_status_reasons")
                    or creative.get("review_status_reason")
                    or creative.get("review_status_details")
                    or []
                ),
                "attempts_used": attempts,
                "previous_headlines": record.get("headline_history", []),
            }
        )
        if len(candidates) >= max_updates:
            break

    return candidates, status_counts, len(live_ads)


def extract_openai_text(payload: dict[str, Any]) -> str:
    texts: list[str] = []
    refusals: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                texts.append(content["text"])
            if content.get("type") == "refusal" and isinstance(content.get("refusal"), str):
                refusals.append(content["refusal"])
    if refusals:
        raise BotError(f"OpenAI declined the headline request: {'; '.join(refusals)}")
    text = "".join(texts).strip()
    if not text:
        raise BotError("OpenAI response did not contain structured output text")
    return text


def generate_headlines(
    api_key: str,
    model: str,
    product_context: str,
    candidates: list[dict[str, Any]],
    forbidden_headlines: list[str],
) -> list[dict[str, Any]]:
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "creative_id": {"type": "string"},
                        "action": {"type": "string", "enum": ["UPDATE"]},
                        "headlines": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 34},
                            "minItems": HEADLINE_OPTIONS_PER_CREATIVE,
                            "maxItems": HEADLINE_OPTIONS_PER_CREATIVE,
                        },
                    },
                    "required": ["creative_id", "action", "headlines"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    instructions = """
Generate five genuinely different, natural Arabic Snapchat ad headline options for every
supplied creative_id. The bot will choose the first option that has never been used.

Goal: create a neutral, natural headline that can suit any product or niche while
accurately representing the actual product. Use wording that reduces avoidable ad-review
problems, but never promise or guarantee approval.

Rules:
- Return one item for every supplied creative_id.
- Return exactly five headline options inside headlines for every item.
- Use Arabic only.
- Maximum 34 Unicode characters, including spaces and punctuation.
- Prefer 3 to 6 words and approximately 18 to 28 characters.
- Match product_context, the advertisement, and the landing page.
- Write in clear, natural White Arabic suitable for Saudi Arabia; avoid robotic,
  over-poetic, vague, or translated-sounding wording.
- Prefer clear wording about the product, availability, discovery, details, or ordering.
  Mention delivery, payment, location, price, or an offer only when product_context
  explicitly confirms that exact fact.
- Across the five options, use different angles. Do not make all five about payment,
  cash on delivery, shipping, or the same call to action.
- Never invent benefits, guarantees, medical results, discounts, delivery terms, or facts.
- Never hide or misrepresent the product category.
- Avoid exaggerated claims, before-and-after claims, pressure tactics, and approval promises.
- Avoid empty quality claims such as "الأفضل" or "جودة مضمونة" and avoid awkward filler.
- Never use a Campaign name, Ad Squad name, Ad name, Creative name, internal label,
  resource ID, SKU, file name, brand name, or transliteration of any such name.
- Never infer a public product name from creative_id. Treat every creative_id as an opaque
  routing value that must be copied only into the JSON creative_id field.
- Preferred tone examples are: "متوفر الآن داخل السعودية",
  "اطلب بسهولة داخل السعودية", "اكتشف التفاصيل الآن", and "لمسة فاخرة ليومك".
  Use their concise, natural tone.
- Generate fresh wording. Exact and near-duplicate history is enforced locally after
  generation and is intentionally not exposed to you.
- Make the five options meaningfully different from one another. Changing only punctuation,
  one small word, or word order is not a fresh headline.
- Never reuse an option for a different creative_id in this response.
- Always return action UPDATE with five fresh, truthful, non-empty options.
- Follow the required JSON schema and return no additional text.
""".strip()
    user_input = json.dumps(
        {
            "product_context": product_context,
            "blocked_headline_count": len(forbidden_headlines),
            # Resource names, ad names, prior headlines, and review metadata are
            # deliberately excluded so internal labels cannot leak into copy.
            "ads": openai_candidate_payload(candidates),
        },
        ensure_ascii=False,
    )
    body = {
        "model": model,
        "store": False,
        "reasoning": {"effort": "low"},
        "max_output_tokens": 8000,
        "input": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_input},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "snapchat_headline_result",
                "strict": True,
                "schema": schema,
            }
        },
    }
    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=90,
    )
    payload = response_json(response, "OpenAI Responses API")
    try:
        parsed = json.loads(extract_openai_text(payload))
    except json.JSONDecodeError as exc:
        raise BotError(f"OpenAI returned invalid JSON: {exc}") from exc
    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        raise BotError("OpenAI structured output did not contain an items array")
    return [item for item in items if isinstance(item, dict)]


def validate_suggestions(
    candidates: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
    forbidden_headlines: list[str],
) -> list[tuple[dict[str, Any], str]]:
    candidate_by_id = {item["creative_id"]: item for item in candidates}
    accepted: list[tuple[dict[str, Any], str]] = []
    handled: set[str] = set()
    forbidden_keys = {
        headline_key(headline)
        for headline in forbidden_headlines
        if headline_key(headline)
    }
    forbidden_hashes = configured_blocked_headline_hashes()
    for candidate in candidates:
        forbidden_keys.add(headline_key(candidate.get("current_headline")))
        forbidden_keys.update(
            headline_key(headline)
            for headline in candidate.get("previous_headlines", [])
            if headline_key(headline)
        )

    for suggestion in suggestions:
        creative_id = suggestion.get("creative_id")
        if not isinstance(creative_id, str) or creative_id not in candidate_by_id:
            print(f"SKIP model item with unknown creative_id: {creative_id!r}")
            continue
        if creative_id in handled:
            print(f"SKIP duplicate model item for {creative_id}")
            continue
        handled.add(creative_id)
        if suggestion.get("action") != "UPDATE":
            print(f"SKIP {creative_id}: model determined headline-only correction is unsuitable.")
            continue
        options = suggestion.get("headlines")
        if not isinstance(options, list):
            print(f"RETRY {creative_id}: model did not return headline options.")
            continue
        candidate = candidate_by_id[creative_id]
        chosen = ""
        for option in options:
            if not isinstance(option, str):
                continue
            headline = clean_headline(option)
            key = headline_key(headline)
            if not headline or len(headline) > 34 or not key:
                continue
            if headline_contains_internal_name(headline, candidate):
                continue
            if key in forbidden_keys or headline_hash_from_key(key) in forbidden_hashes:
                continue
            if any(
                SequenceMatcher(None, key, old_key).ratio() >= NEAR_DUPLICATE_RATIO
                for old_key in forbidden_keys
                if old_key
            ):
                continue
            chosen = headline
            forbidden_keys.add(key)
            break
        if not chosen:
            print(
                f"RETRY {creative_id}: all generated options were already used "
                "or too similar to headline history, or contained an internal name."
            )
            continue
        accepted.append((candidate, chosen))
    return accepted


def record_update(
    state: dict[str, Any], candidate: dict[str, Any], new_headline: str, result: dict[str, Any]
) -> None:
    creative_id = candidate["creative_id"]
    records = state["creatives"]
    record = records.setdefault(creative_id, {})
    history = record.setdefault("headline_history", [])
    current = candidate.get("current_headline")
    if current and current not in history:
        history.append(current)
    if new_headline not in history:
        history.append(new_headline)
    record["headline_history"] = history
    remember_headlines(state, [current, new_headline])
    record["attempts"] = int(record.get("attempts", 0)) + 1
    record["last_ad_id"] = candidate.get("ad_id")
    record["last_ad_squad_id"] = candidate.get("ad_squad_id")
    record["last_headline"] = new_headline
    record["last_patch_at"] = utc_now()
    record["last_result_request_id"] = result.get("request_id")
    record["awaiting_review"] = True
    record["last_review_outcome"] = "PENDING_REVIEW"
    record.pop("pending_seen_at", None)
    record.pop("patch_in_flight", None)
    record.pop("planned_headline", None)
    record.pop("planned_previous_headline", None)
    record.pop("patch_started_at", None)


def clear_update_reservation(state: dict[str, Any], creative_id: str) -> None:
    record = state["creatives"].get(creative_id)
    if not isinstance(record, dict):
        return
    record.pop("patch_in_flight", None)
    record.pop("planned_headline", None)
    record.pop("planned_previous_headline", None)
    record.pop("patch_started_at", None)
    record["awaiting_review"] = False


def main() -> int:
    run_mode = env("RUN_MODE", "test").lower()
    dry_run = run_mode != "live"
    max_updates = bounded_int("MAX_UPDATES", 30, 1, HARD_MAX_UPDATES_PER_RUN)

    client_id = required_env("SNAP_CLIENT_ID")
    client_secret = required_env("SNAP_CLIENT_SECRET")
    refresh_token = required_env("SNAP_REFRESH_TOKEN")
    configured_ad_account_id = env("SNAP_AD_ACCOUNT_ID")
    requested_ad_squad_ids = env("REQUESTED_AD_SQUAD_IDS") or env(
        "REQUESTED_AD_SQUAD_ID"
    )
    fallback_ad_squad_ids = env("SNAP_AD_SQUAD_IDS") or env("SNAP_AD_SQUAD_ID")
    product_context = required_env("PRODUCT_CONTEXT")
    headline_source = env("HEADLINE_SOURCE", "manual_then_openai").lower()
    if headline_source not in {"manual", "openai", "manual_then_openai"}:
        raise BotError("HEADLINE_SOURCE must be manual, openai, or manual_then_openai")
    manual_pool = configured_headline_pool()
    if headline_source in {"manual", "manual_then_openai"} and not manual_pool:
        raise BotError("headline_pool.txt is empty; add fresh generic headlines first")
    openai_api_key = (
        required_env("OPENAI_API_KEY")
        if headline_source in {"openai", "manual_then_openai"}
        else ""
    )
    openai_model = env("OPENAI_MODEL", "gpt-5.4-nano")

    state = load_state()
    ad_account_id = select_ad_account(
        state,
        configured_ad_account_id,
    )
    ad_squad_ids = select_active_ad_squads(
        state, requested_ad_squad_ids, fallback_ad_squad_ids
    )

    print(
        f"Mode={'TEST (no Snapchat edits)' if dry_run else 'LIVE'}; "
        f"ad_account={ad_account_id}; "
        f"ad_squads={len(ad_squad_ids)}; max_updates_per_check={max_updates}; "
        f"attempt_limit=NONE; headline_source={headline_source}; model={openai_model}"
    )

    access_token = refresh_snap_access_token(client_id, client_secret, refresh_token)
    snap = SnapClient(access_token)

    verified_scopes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for ad_squad_id in ad_squad_ids:
        verified_scopes[ad_squad_id] = verify_scope(
            snap, ad_account_id, ad_squad_id
        )

    all_account_ads = snap.get_all(
        f"/adaccounts/{ad_account_id}/ads?limit=1000&read_deleted_entities=true",
        "ads",
        "ad",
    )
    account_creatives = snap.get_all(
        f"/adaccounts/{ad_account_id}/creatives?limit=1000",
        "creatives",
        "creative",
    )
    creative_by_id = {
        str(creative.get("id")): creative
        for creative in account_creatives
        if creative.get("id")
    }
    remember_headlines(
        state,
        [creative.get("headline") for creative in account_creatives],
    )
    creative_status_counts = Counter(
        str(creative.get("review_status", "")).upper() or "UNKNOWN"
        for creative in account_creatives
    )
    print(
        f"Fetched {len(all_account_ads)} account Ad(s) and "
        f"{len(account_creatives)} Creative(s). Creative review statuses: "
        + ", ".join(
            f"{status}={count}"
            for status, count in sorted(creative_status_counts.items())
        )
    )

    selected_set = set(ad_squad_ids)
    globally_seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    overall_status_counts: Counter[str] = Counter()
    all_selected_complete = True

    for ad_squad_id in ad_squad_ids:
        print(f"--- Checking Ad Squad {ad_squad_id} ---")
        remaining_capacity = max_updates - len(candidates)
        squad_candidates, status_counts, _live_count = collect_candidates(
            ad_squad_id,
            selected_set,
            state,
            max(0, remaining_capacity),
            all_account_ads,
            creative_by_id,
            globally_seen,
        )
        campaign, ad_squad = verified_scopes[ad_squad_id]
        for candidate in squad_candidates:
            candidate["campaign_name"] = str(campaign.get("name") or "")
            candidate["ad_squad_name"] = str(ad_squad.get("name") or "")
        candidates.extend(squad_candidates)
        overall_status_counts.update(status_counts)
        if any(status != "APPROVED" for status in status_counts):
            all_selected_complete = False

    print(
        "Overall selected Ad statuses: "
        + (
            ", ".join(
                f"{status}={count}"
                for status, count in sorted(overall_status_counts.items())
            )
            or "NO_LIVE_ADS=0"
        )
    )
    if not dry_run:
        save_state(state)
    if not candidates:
        print("Nothing to update.")
        print(f"MONITOR_COMPLETE={'true' if all_selected_complete else 'false'}")
        return 0

    forbidden_headlines = list(state.get("global_headline_history", []))
    accepted: list[tuple[dict[str, Any], str]] = []
    accepted_ids: set[str] = set()
    remaining = candidates

    generation_rounds = 1 if headline_source == "manual" else MAX_HEADLINE_GENERATION_ROUNDS
    for generation_round in range(1, generation_rounds + 1):
        if headline_source == "manual":
            suggestions = manual_suggestions(remaining)
        elif headline_source == "manual_then_openai" and generation_round == 1:
            suggestions = manual_suggestions(remaining)
        else:
            suggestions = generate_headlines(
                openai_api_key, openai_model, product_context, remaining, forbidden_headlines
            )
        round_accepted = validate_suggestions(
            remaining,
            suggestions,
            forbidden_headlines,
        )
        accepted.extend(round_accepted)
        for candidate, headline in round_accepted:
            accepted_ids.add(candidate["creative_id"])
            forbidden_headlines.append(headline)
        remaining = [
            candidate
            for candidate in remaining
            if candidate["creative_id"] not in accepted_ids
        ]
        if not remaining:
            break
        if generation_round < MAX_HEADLINE_GENERATION_ROUNDS:
            print(
                f"RETRY OpenAI: requesting fresh alternatives for "
                f"{len(remaining)} creative(s)."
            )

    if not accepted:
        print("No globally fresh headline corrections were generated; nothing was updated.")
        print(f"MONITOR_COMPLETE={'true' if all_selected_complete else 'false'}")
        return 0
    for candidate in remaining:
        print(
            f"SKIP {candidate['creative_id']}: no globally fresh headline was found "
            "after two generation rounds."
        )

    if dry_run:
        for candidate, headline in accepted:
            print(
                f"WOULD UPDATE squad={candidate.get('ad_squad_id')} "
                f"creative={candidate['creative_id']} "
                f"from={candidate.get('current_headline')!r} to={headline!r}"
            )
        print("Test mode finished. Snapchat was not changed.")
        print(f"MONITOR_COMPLETE={'true' if all_selected_complete else 'false'}")
        return 0

    updates_succeeded = 0
    for candidate, headline in accepted:
        creative_id = candidate["creative_id"]
        reservation_started_at = utc_now()
        record = state["creatives"].setdefault(creative_id, {})
        record["patch_in_flight"] = True
        record["planned_headline"] = headline
        record["planned_previous_headline"] = candidate.get("current_headline")
        record["patch_started_at"] = reservation_started_at
        record["last_patch_at"] = reservation_started_at
        record["awaiting_review"] = True
        record["last_review_outcome"] = "PENDING_REVIEW"
        save_state(state)
        result = snap.patch_headline(ad_account_id, creative_id, headline)
        wrappers = result.get("creatives", [])
        if wrappers and isinstance(wrappers[0], dict):
            sub_status = str(wrappers[0].get("sub_request_status", "SUCCESS")).upper()
            if sub_status != "SUCCESS":
                print(f"FAILED {creative_id}: Snapchat sub-request status={sub_status}")
                clear_update_reservation(state, creative_id)
                save_state(state)
                continue
        record_update(state, candidate, headline, result)
        save_state(state)
        updates_succeeded += 1
        print(
            f"UPDATED squad={candidate.get('ad_squad_id')} creative={creative_id}: "
            f"{headline!r}; submitted for re-review."
        )

    print(f"Live run finished: {updates_succeeded} creative(s) updated.")
    print(f"MONITOR_COMPLETE={'true' if all_selected_complete else 'false'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        if os.getenv("PUBLIC_SAFE_LOGS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            print("UNEXPECTED ERROR: private diagnostic details hidden.", file=sys.stderr)
            raise SystemExit(1)
        raise
