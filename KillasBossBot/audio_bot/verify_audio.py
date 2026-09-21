"""
The verification engine behind the /verify slash command.

Given a Roblox asset id, it asks Roblox for the asset's metadata and decides
whether that audio asset can actually be used (referenced as rbxassetid://..)
from a game, and returns a plain-text reason whenever it can't.

Failure reasons the game's audio player cares about:
  * not a valid asset id
  * asset is banned / under moderation
  * asset is owned by a group (group audio isn't supported)
  * asset isn't audio at all (it's some other asset type)
  * paid / for-sale (can't be streamed by id without owning it)
  * archived / removed (Roblox refuses to serve it)
  * couldn't be found

Endpoints used (all optional, each produces a different signal):

  1. economy details  -- https://economy.roblox.com/v2/assets/{id}/details
     The PRIMARY source. Reliable for creator-served audio (which the classic
     marketplace endpoint doesn't cover). Returns AssetTypeId, Name, Creator
     {Id,Name,CreatorType:"User"/"Group"}, PriceInRobux, IsForSale. CreatorType
     is the authoritative way to tell a group-owned asset from a user one.

  2. getassetdetails  -- https://www.roblox.com/assetmarketplace/getassetdetails
     Optional, and the ONLY source that reliably reports IsBanned. It returns
     empty for creator-gallery audio, so we use it only to add/replace the
     IsBanned verdict, never as the sole existence check.

  3. asset delivery   -- https://assetdelivery.roblox.com/v1/asset?id=..
     Best-effort probe. The feed 403s for any audio that needs Roblox's apiKey
     even when it's perfectly playable, so a bare 4xx is NEVER a failure. We only
     reject when the body explicitly says the asset is banned/archived/removed.

Everything is synchronous urllib (matching the rest of the bot) and is meant to
be called through asyncio.to_thread so it never blocks the event loop.
"""

import json
import re
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 RobloxApp")
DETAILS_URL = "https://www.roblox.com/assetmarketplace/getassetdetails?id={id}"
ECONOMY_URL = "https://economy.roblox.com/v2/assets/{id}/details"
DELIVERY_URL = "https://assetdelivery.roblox.com/v1/asset?id={id}"

AUDIO_TYPE_ID = 3  # AssetTypeId for Audio
TYPE_NAMES = {3: "Audio", 2: "Decal", 12: "Audio", 11: "Model"}  # just for nicer text


class AssetError(Exception):
    """Raised with a user-facing reason when the asset can't be used."""


def _get(url, timeout=10):
    """Fetch a URL, return (status, text). Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception:
        return None, ""


def extract_asset_id(raw: str) -> int:
    """Turn a numeric id, a URL, or 'rbxassetid://123' into an int."""
    raw = (raw or "").strip().strip("<>@")
    m = re.search(r"rbxasset(?:id)?://(\d+)", raw)
    if m:
        return int(m.group(1))
    m = re.search(r"roblox\.com/[a-z0-9_-]+/(\d+)", raw)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"\d+", raw):
        return int(raw)
    raise AssetError("That isn't a valid Roblox asset id. Pass a plain number, "
                     "a rbxassetid:// url, or a roblox.com asset link.")


def _economy(id_):
    """Primary metadata dict from the economy API, or {} if unreachable."""
    status, text = _get(ECONOMY_URL.format(id=id_))
    if status == 200 and text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
    return {}


def _marketplace(id_):
    """Optional dict from getassetdetails, or {} (used only for IsBanned)."""
    status, text = _get(DETAILS_URL.format(id=id_))
    if status != 200 or not text:
        return {}
    try:
        arr = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if isinstance(arr, list):
        return arr[0] if arr else {}
    return arr if isinstance(arr, dict) else {}


def _delivery_verdict(id_) -> str:
    """Return an explicit reason ONLY when the delivery feed says so, else ''."""
    status, text = _get(DELIVERY_URL.format(id=id_))
    if status is None:
        return ""  # network blip -- don't fail on this
    low = (text or "").lower()
    if "banned" in low or "moderat" in low:
        return "This audio asset is banned/under moderation, so Roblox won't play it."
    if "archiv" in low or "removed" in low or "deleted" in low:
        return "This asset is archived/removed, so Roblox won't serve it."
    # A bare 4xx (usually "User is not authorized to access Asset") happens for
    # audio that needs Roblox's apiKey and is NOT a verdict -- ignore it.
    return ""


def _type_name(asset_type_id):
    return TYPE_NAMES.get(asset_type_id, f"type {asset_type_id}")


def verify_asset(id_: int) -> dict:
    """Run all checks. Returns {'ok': bool, 'message': str, **meta}."""
    if id_ <= 0 or id_ > 1_000_000_000_000_000_000:
        raise AssetError("That asset id is out of range. Roblox asset ids are "
                         "positive (audio ids can be 15+ digits).")

    econ = _economy(id_)
    mkt = _marketplace(id_)

    if not econ and not mkt:
        # No metadata at all: only reject if Roblox explicitly says it's gone.
        verdict = _delivery_verdict(id_)
        if verdict:
            raise AssetError(verdict)
        raise AssetError("Couldn't find that asset or read its details on Roblox. "
                         "Double-check the number -- it may be private, deleted, or "
                         "never existed.")

    name = (econ.get("Name") or mkt.get("Name") or "(unnamed)")
    asset_type_id = econ.get("AssetTypeId") or mkt.get("AssetTypeId")
    asset_type_name = mkt.get("AssetTypeName") or _type_name(asset_type_id)

    # Banned only comes from the marketplace endpoint (economy doesn't report it).
    if mkt.get("IsBanned"):
        raise AssetError("This audio asset is banned/under moderation, so Roblox "
                         "won't play it.")

    if asset_type_id != AUDIO_TYPE_ID:
        raise AssetError(
            f"That asset is **{asset_type_name}** (asset type {asset_type_id}), "
            f"not an **Audio** asset. Only audio assets work here."
        )

    creator = econ.get("Creator") or mkt.get("Creator") or {}
    creator_name = creator.get("Name") or "unknown"
    creator_id = creator.get("Id")
    # economy's CreatorType is a plain "User"/"Group" string -- authoritative.
    creator_type = (creator.get("CreatorType") or "").lower()
    if creator_type == "group":
        raise AssetError(
            "This audio is **group-owned** ({}) and group audio isn't supported "
            "here. Re-upload the audio on a **user** account instead.".format(creator_name)
        )

    price = econ.get("PriceInRobux")
    if price is None:
        price = mkt.get("PriceInRobux") or 0
    for_sale = bool(econ.get("IsForSale") or mkt.get("IsForSale"))
    if for_sale or (isinstance(price, (int, float)) and price > 0):
        raise AssetError(
            "This is a **paid / for-sale** asset -- it can't be streamed by id "
            "unless bought, so it wouldn't work here."
        )

    verdict = _delivery_verdict(id_)
    if verdict:
        raise AssetError(verdict)

    return {
        "ok": True,
        "message": (
            f"**{name}** is a usable audio asset."
            + (f"\n**Creator:** {creator_name} (id {creator_id})" if creator_name else "")
            + f"\n**Asset type:** {asset_type_name}"
            + (f"\n**Price:** {price} R$" if isinstance(price, (int, float)) and price else "")
        ),
        "name": name,
        "creator": creator_name,
        "creator_id": creator_id,
        "asset_type": asset_type_name,
        "price": price,
    }
