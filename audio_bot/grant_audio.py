"""
The grant engine behind the /verify slash command.

Given a Roblox audio asset id (or a link), it looks the asset up live, confirms it
is Audio and owned by a Group in the configured SUPPORTED_GROUP_IDS list, then
adds the configured GRANT_UNIVERSE_ID to that audio's permission list so the game
is allowed to use it. The WHOLE point is to let you grant permissions on the
audios your group(s) publish, without opening the Creator Dashboard each time.

Everything is synchronous urllib (matching verify_audio) and meant to run through
asyncio.to_thread.

Endpoints (both confirmed against the live API):

  1. asset details -- https://economy.roblox.com/v2/assets/{id}/details
     Requires the logged-in cookie (unauthenticated it 400s). Returns Name,
     AssetTypeId, PriceInRobux, IsForSale and the Creator block
     {Id, Name, CreatorType, CreatorTargetId}. For a Group owner the authoritative
     group id is CreatorTargetId (Creator.Id is a different internal number).

  2. grant           -- https://apis.roblox.com/asset-permissions-api/v1/assets/check-permissions
     POST, content-type application/json-patch+json, x-csrf-token + cookie.
     Body: {"requests":[{"subject":{"subjectType":"Universe","subjectId":UNIVERSE},
                          "action":"Use","assetId":ASSET}]}
     This is the call the Creator Dashboard fires when you allow/associate an
     experience (universe) with an audio asset. A fresh CSRF token is fetched
     per request because the captured one expires.

Config (read from env inside this module):
  ROBLOX_COOKIE       -- the .ROBLOSECURITY=...; rbxas=... cookie of the account
                         (or group) that owns the audios. REQUIRED.
  SUPPORTED_GROUP_IDS -- comma-separated group ids the bot is allowed to grant
                         from. REQUIRED (empty = nothing is allowed).
  GRANT_UNIVERSE_ID   -- the experience/universe id granted access to each audio.
                         Defaults to 10577282025.
"""

import json
import os
import urllib.error
import urllib.request

import verify_audio  # for extract_asset_id + AssetError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0 Safari/537.36 RobloxApp")

DETAILS_URL = "https://economy.roblox.com/v2/assets/{id}/details"
GRANT_URL = "https://apis.roblox.com/asset-permissions-api/v1/assets/check-permissions"

AUDIO_TYPE_ID = 3
GROUPS_URL = "https://groups.roblox.com/v1/groups/{id}"
SUPPORTED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "supported_groups.json")
# Verified-audio cache: ids confirmed usable by /verify. Persisted here (survives
# bot restarts) and published to the relay so every game server mirrors it and any
# player gets instant playback without re-checking on a later play.
VERIFIED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "verified_audios.json")

SUPPORTED_GROUP_IDS = {
    int(x) for x in os.getenv("SUPPORTED_GROUP_IDS", "").split(",") if x.strip().isdigit()
}
GRANT_UNIVERSE_ID = int(os.getenv("GRANT_UNIVERSE_ID", "10577282025"))
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE", "")

# Local relay the supported-groups list is published to so the in-game Groups tab
# can mirror it live (GroupSync polls this key /supported_groups). Best-effort; a
# down relay must never break an add.
RELAY_BASE = os.getenv("SUPPORTED_GROUPS_RELAY", "http://127.0.0.1:8765")

# Public Roblox Groups API join endpoint. CSRF is elicited on the same host.
JOIN_GROUP_URL = "https://groups.roblox.com/v1/groups/{id}/join-users"


class GrantError(Exception):
    """Raised with a user-facing reason when the grant can't be carried out."""


def _cookie_header(cookie):
    """Return a valid Cookie header value.

    The env var may hold just the .ROBLOSECURITY *token* (what a browser/dev
    tool hands you) or already a full 'name=value' cookie string. The Cookie
    header needs the cookie NAME, so wrap a bare token in '.ROBLOSECURITY='.
    """
    if not cookie:
        return ""
    if cookie.lstrip().startswith(".ROBLOSECURITY="):
        return cookie
    return ".ROBLOSECURITY=" + cookie


def _get(url, cookie=ROBLOX_COOKIE, headers=None, timeout=15, return_headers=False):
    """GET a URL, return (status, text[, headers]). Never raises on HTTP errors."""
    h = {"User-Agent": UA, "Accept": "*/*",
         "Referer": "https://create.roblox.com/", "Origin": "https://create.roblox.com"}
    if cookie:
        h["Cookie"] = _cookie_header(cookie)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            if return_headers:
                return resp.status, body, dict(resp.headers)
            return resp.status, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        if return_headers:
            return e.code, body, dict(e.headers)
        return e.code, body
    except Exception as e:
        if return_headers:
            return None, str(e), {}
        return None, str(e)


def _post_json(url, payload, headers, cookie=ROBLOX_COOKIE, timeout=15,
               content_type="application/json-patch+json"):
    """POST a JSON body; return (status, text, headers)."""
    h = {"User-Agent": UA, "Accept": "*/*",
         "Referer": "https://create.roblox.com/", "Origin": "https://create.roblox.com",
         "Content-Type": content_type,
         "Content-Length": str(len(payload))}
    if cookie:
        h["Cookie"] = _cookie_header(cookie)
    h.update(headers)
    data = payload.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body, dict(e.headers)
    except Exception as e:
        return None, str(e), {}


def _header(headers, name):
    """Case-insensitive response-header lookup."""
    low = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == low:
            return v
    return None


def _require_config():
    if not ROBLOX_COOKIE:
        raise GrantError("ROBLOX_COOKIE isn't set in .env -- the grant account "
                         "(.ROBLOSECURITY=...; rbxas=...) is required for both the "
                         "asset lookup and the grant.")
    if not SUPPORTED_GROUP_IDS:
        raise GrantError("SUPPORTED_GROUP_IDS isn't set in .env -- at least one "
                         "group id the bot is allowed to grant from is required.")


def get_csrf_token(cookie=ROBLOX_COOKIE):
    """Fetch a fresh CSRF token (the captured one expires).

    Roblox issues CSRF via 403-elicitation: send a rejected request and it returns
    the current valid token in the X-CSRF-Token response header. We use a bogus
    token AND a non-existent asset id, so nothing can be mutated even if the token
    were somehow accepted -- the request always fails CSRF validation first.
    """
    payload = json.dumps({
        "requests": [{"subject": {"subjectType": "Universe", "subjectId": "0"},
                      "action": "Use", "assetId": 0}]
    }, separators=(",", ":"))
    status, _text, headers = _post_json(
        GRANT_URL, payload, {"x-csrf-token": "BOGUS_TOKEN_123"}, cookie=cookie)
    token = _header(headers, "X-CSRF-Token") or ""
    if not token:
        raise GrantError(f"Could not get a CSRF token (status {status}). Check that "
                         "ROBLOX_COOKIE is a valid session for an account that owns "
                         "a supported group.")
    return token


def resolve_asset(asset_id: int, cookie=ROBLOX_COOKIE):
    """Look up an asset. Returns {'name', 'asset_type_id', 'creator', 'group_id',
    'creator_type', 'creator_name', 'price', 'for_sale'} or raises GrantError."""
    if asset_id <= 0 or asset_id > 1_000_000_000_000_000_000:
        raise GrantError("That asset id is out of range.")

    status, text = _get(DETAILS_URL.format(id=asset_id), cookie=cookie)
    if status != 200 or not text:
        raise GrantError(
            "Couldn't read details for that asset (status %s). Double-check the id "
            "-- it may be private, deleted, or never existed." % status)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise GrantError("Roblox returned an unreadable response for that id.")

    creator = data.get("Creator") or {}
    creator_type = (creator.get("CreatorType") or "").lower()
    # For a Group owner, CreatorTargetId is the group id (Creator.Id is internal).
    group_id = creator.get("CreatorTargetId") if creator_type == "group" else None

    return {
        "name": data.get("Name") or "(unnamed)",
        "asset_type_id": data.get("AssetTypeId"),
        "asset_type_name": "Audio" if data.get("AssetTypeId") == AUDIO_TYPE_ID
                           else "type %s" % data.get("AssetTypeId"),
        "creator_name": creator.get("Name") or "unknown",
        "creator_type": creator_type or "user",
        "group_id": group_id,
        "price": data.get("PriceInRobux") or 0,
        "for_sale": bool(data.get("IsForSale")),
        "raw": data,
    }


def _check_supported(meta):
    if meta["asset_type_id"] != AUDIO_TYPE_ID:
        raise GrantError(
            f"That asset is **{meta['asset_type_name']}**, not an **Audio** asset. "
            f"Only audio assets can be granted to your game."
        )
    if meta["creator_type"] != "group":
        raise GrantError(
            f"This asset is owned by a **user** ({meta['creator_name']}), not a group. "
            f"Only group-owned audio can be granted here -- upload audio on a group "
            f"account you control."
        )
    if meta["group_id"] not in supported_groups():
        raise GrantError(
            f"This audio is owned by group **{meta['creator_name']}** (id "
            f"{meta['group_id']}), which is **not** in this bot's supported group "
            f"list. Run `/verifygroup add {meta['group_id']}` to add it."
        )
    if meta["for_sale"] or (isinstance(meta["price"], (int, float)) and meta["price"] > 0):
        raise GrantError(
            "This is a paid/for-sale asset -- permissions can't be granted to an "
            "experience for a paid audio in this flow."
        )


def _load_dynamic_supported():
    """Groups added at runtime via /verifygroup, persisted next to this module."""
    try:
        with open(SUPPORTED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {int(v) for v in (data.get("group_ids", []) or [])}
    except (FileNotFoundError, ValueError, TypeError):
        return set()


def _load_dynamic_groups():
    """The runtime-added groups as [{id, name, locked}] -- the display list for the game."""
    try:
        with open(SUPPORTED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        locked_ids = {int(v) for v in (data.get("locked_ids", []) or [])}
        out = []
        for g in (data.get("groups") or []):
            if isinstance(g, dict) and g.get("id") is not None:
                gid = int(g["id"])
                out.append({"id": gid, "name": g.get("name") or "?",
                            "locked": bool(g.get("locked")) or gid in locked_ids})
        return out
    except (FileNotFoundError, ValueError, TypeError):
        return []


def publish_supported_groups():
    """POST the current supported-groups list to the local relay so the in-game
    GroupSync script can mirror it to the Groups tab. Best-effort: a down relay must
    never break an add, so any failure is swallowed.

    Live lock state is overlaid so the game hides a group the moment Roblox reports
    it locked (the same effective_locks() the /groups embed uses), not only when an
    admin manually ran /verifygroup remove.
    """
    try:
        groups = _load_dynamic_groups()
        locks = {}
        try:
            locks = effective_locks()
        except Exception:  # noqa: BLE001 -- never let a lock lookup break the publish
            locks = {}
        for g in groups:
            if g["id"] in locks:
                g["locked"] = bool(locks[g["id"]])
        payload = json.dumps({"groups": groups}, separators=(",", ":"))
        req = urllib.request.Request(
            RELAY_BASE + "/supported_groups",
            data=payload.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
    except Exception:
        pass


def load_verified_audios():
    """Return the persisted set of verified audio ids."""
    try:
        with open(VERIFIED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {int(v) for v in (data.get("audios", []) or []) if str(v).isdigit()}
    except (FileNotFoundError, ValueError, TypeError):
        return set()


def save_verified_audios(audios):
    """Persist the verified-audio set to disk (best-effort; never raise)."""
    try:
        with open(VERIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump({"audios": sorted(int(a) for a in audios)}, f)
    except Exception:  # noqa: BLE001
        pass


def mark_audio_verified(asset_id):
    """Add an audio id to the verified cache, persist it, and publish it live.

    Returns True when the id is now in the cache (whether newly added or already
    there). Never raises -- the cache is best-effort and must not break a verify.
    """
    audios = load_verified_audios()
    audios.add(int(asset_id))
    save_verified_audios(audios)
    publish_verified_audios()
    return True


def publish_verified_audios():
    """POST the verified-audio id list to the relay so every game server can mirror
    it (VerifiedAudioCache) and any player gets instant playback. Best-effort."""
    try:
        payload = json.dumps({"audios": sorted(load_verified_audios())},
                             separators=(",", ":"))
        req = urllib.request.Request(
            RELAY_BASE + "/verified_audios",
            data=payload.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
    except Exception:
        pass


def _load_locked_ids():
    """Group ids locked via /verifygroup remove (manual). These stay on the list
    so they visibly read as LOCKED, but are never grantable."""
    try:
        with open(SUPPORTED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {int(v) for v in (data.get("locked_ids", []) or [])}
    except (FileNotFoundError, ValueError, TypeError):
        return set()


def all_supported_ids():
    """Every supported group id, including locked ones (the full list to show)."""
    return SUPPORTED_GROUP_IDS | _load_dynamic_supported()


def supported_groups():
    """The current grantable set: .env seed union the runtime-added JSON store,
    minus any group that's been locked."""
    return all_supported_ids() - _manual_locked_ids()


def _manual_locked_ids():
    """Ids locked by an admin (via /verifygroup remove) -- persisted flags only."""
    locked = _load_locked_ids()
    for g in _load_dynamic_groups():
        if g.get("locked"):
            locked.add(g["id"])
    return locked


def effective_locks():
    """{gid: True} for every supported group showing as locked.

    A group counts as locked if an admin marked it via /verifygroup remove OR
    Roblox currently reports it locked (the Groups API `isLocked` flag). Lookups
    are best-effort: a group we can't reach keeps its persisted flag. This is what
    the list command uses to show the LOCKED badge, and it also re-detects a group
    that gets locked later so it shows up without any manual step.
    """
    gids = all_supported_ids()
    manual = _manual_locked_ids()
    locks = {gid: (gid in manual) for gid in gids}
    for gid in gids:
        try:
            info = get_group_info(gid)
            if info.get("is_locked"):
                locks[gid] = True
        except Exception:  # noqa: BLE001
            pass
    return locks


def get_group_info(group_id: int):
    """Public group lookup. Returns {'id', 'name', 'member_count'} or raises."""
    status, text = _get(GROUPS_URL.format(id=group_id), cookie="")
    if status != 200 or not text:
        raise GrantError(f"Couldn't find a group with id {group_id} (status {status}). "
                         f"Is that the group's numeric id?")
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        raise GrantError("Roblox returned an unreadable response for that group id.")
    return {"id": d.get("id"), "name": d.get("name") or "unknown",
            "member_count": d.get("memberCount"),
            "is_locked": bool(d.get("isLocked"))}


def _persist_supported_group(gid: int, gname: str = None):
    """Add a group id (and its display name) to support_groups.json, then publish the
    list so the in-game Groups tab updates live."""
    try:
        with open(SUPPORTED_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, TypeError):
        data = {}
    gids = set(int(v) for v in (data.get("group_ids", []) or []))
    gids.add(int(gid))
    data["group_ids"] = sorted(gids)
    groups = list(data.get("groups") or [])
    found = False
    for g in groups:
        if int(g.get("id", 0)) == int(gid):
            if gname:
                g["name"] = gname
            found = True
            break
    if not found:
        groups.append({"id": int(gid), "name": gname or "?"})
    data["groups"] = groups
    os.makedirs(os.path.dirname(SUPPORTED_FILE), exist_ok=True)
    with open(SUPPORTED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # reflect immediately so a grant right after this call works without a restart
    SUPPORTED_GROUP_IDS.add(int(gid))
    publish_supported_groups()


def add_supported_group(group_id: int):
    """Validate the group exists, then add it to the supported list permanently.
    Returns {'id', 'name'} on success; raises GrantError if it can't be added."""
    info = get_group_info(group_id)  # validates existence before persisting
    _persist_supported_group(int(group_id), info["name"])
    return {"id": info["id"], "name": info["name"]}


def _write_supported(data):
    os.makedirs(os.path.dirname(SUPPORTED_FILE), exist_ok=True)
    with open(SUPPORTED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _load_supported_data():
    try:
        with open(SUPPORTED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, TypeError):
        return {}


def mark_group_locked(group_id: int):
    """Mark a group as LOCKED so it stays listed but is no longer grantable.

    Keeps the id in the display list and the groups array (sets its 'locked' flag)
    so the group visibly reads as LOCKED instead of disappearing. This is a
    /verifygroup remove: it never contacts Roblox. Groups already locked are left
    alone. Returns {'id', 'name'} (name is None if it wasn't in the list).
    """
    gid = int(group_id)
    data = _load_supported_data()
    gids = set(int(v) for v in (data.get("group_ids", []) or []))
    gids.add(gid)  # keep it on the list so it reads as LOCKED
    data["group_ids"] = sorted(gids)
    locked = set(int(v) for v in (data.get("locked_ids", []) or []))
    locked.add(gid)
    data["locked_ids"] = sorted(locked)
    name = None
    groups = list(data.get("groups") or [])
    found = False
    for g in groups:
        if isinstance(g, dict) and int(g.get("id", 0)) == gid:
            g["locked"] = True
            name = g.get("name")
            found = True
            break
    if not found:
        groups.append({"id": gid, "name": "?", "locked": True})
    data["groups"] = groups
    _write_supported(data)
    publish_supported_groups()
    return {"id": gid, "name": name}


def unlock_supported_group(group_id: int):
    """Clear the LOCKED flag so a group becomes grantable again.

    Returns {'id', 'name'} on success; name is None if the id wasn't in the list.
    """
    gid = int(group_id)
    data = _load_supported_data()
    locked = set(int(v) for v in (data.get("locked_ids", []) or []))
    locked.discard(gid)
    data["locked_ids"] = sorted(locked)
    name = None
    groups = list(data.get("groups") or [])
    for g in groups:
        if isinstance(g, dict) and int(g.get("id", 0)) == gid:
            name = g.get("name")
            if "locked" in g:
                g["locked"] = False
            break
    data["groups"] = groups
    _write_supported(data)
    publish_supported_groups()
    return {"id": gid, "name": name}


def delete_supported_group(group_id: int):
    """Remove a group id from the supported list entirely.

    Drops it from the id set, the display-names array, and any locked flag so the
    in-game Groups tab loses it live (publish_supported_groups() is called after
    the write). This is a /verifygroup delete: it never contacts Roblox -- deleting
    by id is a no-op if the id isn't currently supported. Returns {'id', 'name'}
    (name is None if it wasn't in the list).
    """
    gid = int(group_id)
    data = _load_supported_data()
    gids = set(int(v) for v in (data.get("group_ids", []) or []))
    if gid not in gids:
        return {"id": gid, "name": None}
    gids.discard(gid)
    data["group_ids"] = sorted(gids)
    locked = set(int(v) for v in (data.get("locked_ids", []) or []))
    locked.discard(gid)
    data["locked_ids"] = sorted(locked)
    removed_name = None
    kept = []
    for g in (data.get("groups") or []):
        if isinstance(g, dict) and int(g.get("id", 0)) == gid:
            removed_name = g.get("name")
        else:
            kept.append(g)
    data["groups"] = kept
    _write_supported(data)
    # reflect immediately so a subsequent grant re-checks without a restart
    SUPPORTED_GROUP_IDS.discard(gid)
    publish_supported_groups()
    return {"id": gid, "name": removed_name}


def _friendly_error(text):
    """Pull Roblox's human-readable message out of an error body, or a generic note."""
    if not text:
        return "Roblox didn't tell us why."
    try:
        d = json.loads(text)
        msg = d.get("message") or d.get("errors", [{}])[0].get("message")
        if msg:
            return msg + "."
    except Exception:
        pass
    return (text[:120] + "...") if len(text) > 120 else text


def join_group(group_id: int, cookie=ROBLOX_COOKIE):
    """Join the cookie account to a Roblox group (so grants on its audio can go through).

    Returns {'id', 'name', 'joined', 'already_member', 'captcha', 'message'}. Hard
    failures that should stop the flow raise GrantError (no cookie / invalid session).
    """
    if not cookie:
        raise GrantError("ROBLOX_COOKIE isn't set in .env -- it's needed to join the group.")
    gid = int(group_id)
    url = JOIN_GROUP_URL.format(id=gid)
    info = get_group_info(gid)  # validates existence + gives the name

    # Elicit a CSRF token on the groups host: send a deliberately-rejected request and
    # read the fresh token back from the response header (Roblox rotates them).
    status, text, headers = _post_json(
        url, "{}", {"x-csrf-token": "BOGUS_TOKEN"}, cookie=cookie, content_type="application/json")
    csrf = _header(headers, "x-csrf-token") or ""
    if not csrf:
        raise GrantError(
            "Could not start the group join (HTTP %s). %s "
            "This usually means the .env ROBLOX_COOKIE doesn't authorize group actions "
            "on this account (it is stale or missing the rbxas companion token). Refresh "
            "it with the FULL `.ROBLOSECURITY=...; rbxas=...` value, or join the account "
            "to the group manually in a browser." % (status, _friendly_error(text)))

    status, text, _headers = _post_json(
        url, "{}", {"x-csrf-token": csrf}, cookie=cookie, content_type="application/json")
    low = (text or "").lower()

    if status in (200, 201, 204):
        return {"id": info["id"], "name": info["name"], "joined": True,
                "already_member": False, "captcha": False,
                "message": f"Joined group **{info['name']}**."}
    if "already" in low and ("member" in low or "this group" in low):
        return {"id": info["id"], "name": info["name"], "joined": True,
                "already_member": True, "captcha": False,
                "message": f"The account is already a member of **{info['name']}**."}
    if "pending" in low or ("request" in low and "approval" in low):
        return {"id": info["id"], "name": info["name"], "joined": False,
                "already_member": False, "captcha": False,
                "message": f"Join is pending approval for **{info['name']}**."}
    if status in (400, 403) and "captcha" in low:
        return {"id": info["id"], "name": info["name"], "joined": False,
                "already_member": False, "captcha": True,
                "message": f"Roblox asked for a CAPTCHA to join **{info['name']}**. "
                           "Complete it manually on the group page, then run /verifygroup add again."}
    raise GrantError("The group join was refused (HTTP %s). %s" % (status, _friendly_error(text)))


def verify_group(group_id: int):
    """One-call /verifygroup: add the group to the supported list AND try to join it.

    Always adds the group to supported_groups.json so the list is up to date even when
    the join can't be completed (stale cookie, captcha). Returns:
      {'id', 'name', 'added', 'joined', 'already_member', 'captcha', 'message'}
    Raises GrantError only if the group can't be looked up at all.
    """
    info = get_group_info(group_id)  # validates existence before persisting
    gid = int(info["id"])
    _persist_supported_group(gid, info["name"])
    try:
        join = join_group(gid)
        return {"id": info["id"], "name": info["name"], "added": True,
                "joined": join["joined"], "already_member": join["already_member"],
                "captcha": join["captcha"], "message": join["message"]}
    except GrantError as e:
        # The list is updated regardless; surface why the join didn't happen.
        return {"id": info["id"], "name": info["name"], "added": True,
                "joined": False, "already_member": False, "captcha": False,
                "message": str(e)}


def grant_permission(asset_id: int, universe_id: int = None, cookie=ROBLOX_COOKIE):
    """Validate the audio, then add universe_id to its permission list.
    Returns a dict describing the grant. Raises GrantError on any failure."""
    _require_config()
    if universe_id is None:
        universe_id = GRANT_UNIVERSE_ID

    meta = resolve_asset(asset_id, cookie=cookie)
    _check_supported(meta)

    csrf = get_csrf_token(cookie=cookie)
    payload = json.dumps({
        "requests": [{
            "subject": {"subjectType": "Universe", "subjectId": str(universe_id)},
            "action": "Use",
            "assetId": asset_id,
        }]
    }, separators=(",", ":"))

    status, text, _headers = _post_json(
        GRANT_URL, payload, {"x-csrf-token": csrf}, cookie=cookie)

    if status in (200, 201, 204):
        return {
            "ok": True,
            "asset_id": asset_id,
            "universe_id": universe_id,
            "group_name": meta["creator_name"],
            "group_id": meta["group_id"],
            "name": meta["name"],
            "raw": text,
        }

    # Surface Roblox's own message if present, else a generic one.
    try:
        err = json.loads(text)
        msg = err.get("message") or err.get("errors", [{}])[0].get("message")
    except Exception:
        msg = None
    raise GrantError(
        f"The grant was refused (HTTP {status}). " +
        (f"Roblox says: {msg}." if msg else "Roblox didn't tell us why in a readable form.") +
        " If it says the CSRF token is invalid, that just means the session rotated -- "
        "try again."
    )


def verify(asset_id: int, universe_id: int = None, cookie=ROBLOX_COOKIE):
    """One-call /verify: check the audio AND grant it when it's a group audio.

    Returns {'ok', 'granted', 'name', ...}. Raises GrantError with the reason if
    it can't be used/granted. A free user-owned audio just confirms it's usable
    (no permission is needed); a group-owned audio from a supported group is
    granted right here.
    """
    _require_config()
    if universe_id is None:
        universe_id = GRANT_UNIVERSE_ID

    meta = resolve_asset(asset_id, cookie=cookie)
    if meta["asset_type_id"] != AUDIO_TYPE_ID:
        raise GrantError(f"That asset is **{meta['asset_type_name']}**, not an **Audio** "
                         f"asset. Only audio assets can be verified for your game.")
    if meta["for_sale"] or (isinstance(meta["price"], (int, float)) and meta["price"] > 0):
        raise GrantError("This is a paid/for-sale asset -- it can't be granted to an "
                         "experience in this flow.")

    if meta["creator_type"] == "group":
        # Group-owned: must be in a supported group to grant; then grant it.
        grant = grant_permission(asset_id, universe_id, cookie=cookie)
        mark_audio_verified(asset_id)
        return {
            "ok": True, "granted": True,
            "name": meta["name"],
            "creator_name": meta["creator_name"],
            "creator_type": "group",
            "group_id": meta["group_id"],
            "universe_id": universe_id,
            "message": (f"**{meta['name']}** was verified **and granted** -- your game "
                        f"(universe **{universe_id}**) may now use this audio from group "
                        f"**{meta['creator_name']}**.")
        }

    # User-owned free audio: usable as-is, no permission grant needed.
    mark_audio_verified(asset_id)
    return {
        "ok": True, "granted": False,
        "name": meta["name"],
        "creator_name": meta["creator_name"],
        "creator_type": "user",
        "universe_id": universe_id,
        "message": (f"**{meta['name']}** is a usable audio owned by **{meta['creator_name']}** "
                    f"(user account). No permission is needed for it in your game.")
    }


# Seed the relay as soon as the module loads so the game's Groups tab reflects the
# current supported list (including a removal) even before the next /verifygroup.
publish_supported_groups()
