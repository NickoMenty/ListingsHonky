#!/usr/bin/env python3
"""
Hyperliquid Listing Monitor
Detects new spot/perp listings and tracks deploy auction pipeline.
"""

import argparse
import json
import os
import time
from datetime import datetime

import requests
from colorama import Fore, Style, init
from dotenv import load_dotenv

init(autoreset=True)
load_dotenv()

API_URL = "https://api.hyperliquid.xyz/info"
PARADEX_API_URL = "https://api.prod.paradex.trade/v1"
ASTER_FAPI_URL = "https://fapi.asterdex.com/fapi/v1/exchangeInfo"
ASTER_SAPI_URL = "https://sapi.asterdex.com/api/v1/exchangeInfo"
TG_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_send(message: str):
    token = os.environ.get("TG_API")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            TG_API_URL.format(token=token),
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except requests.RequestException as e:
        print(Fore.YELLOW + f"[WARN] Telegram send failed: {e}")


# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Hyperliquid listing monitor")
    p.add_argument("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
    p.add_argument("--state-file", default="state.json", help="Path to state persistence file")
    return p.parse_args()


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

DEFAULT_STATE = {
    "known_spot_universe": [],
    "known_perp_universe": [],
    "last_spot_auction": {},
    "last_perp_auction": {},
}


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {
            "known_spot_universe": set(),
            "known_perp_universe": set(),
            "last_spot_auction": {},
            "last_perp_auction": {},
            "known_paradex_live": set(),
            "known_paradex_upcoming": set(),
            "known_aster_trading": set(),
            "known_aster_pending": set(),
        }
    try:
        with open(path) as f:
            raw = json.load(f)
        return {
            "known_spot_universe": set(raw.get("known_spot_universe", [])),
            "known_perp_universe": set(raw.get("known_perp_universe", [])),
            "last_spot_auction": raw.get("last_spot_auction", {}),
            "last_perp_auction": raw.get("last_perp_auction", {}),
            "known_paradex_live": set(raw.get("known_paradex_live", [])),
            "known_paradex_upcoming": set(raw.get("known_paradex_upcoming", [])),
            "known_aster_trading": set(raw.get("known_aster_trading", [])),
            "known_aster_pending": set(raw.get("known_aster_pending", [])),
        }
    except (json.JSONDecodeError, KeyError):
        print(Fore.YELLOW + "[WARN] State file corrupted, resetting to defaults.")
        return {
            "known_spot_universe": set(),
            "known_perp_universe": set(),
            "last_spot_auction": {},
            "last_perp_auction": {},
            "known_paradex_live": set(),
            "known_paradex_upcoming": set(),
            "known_aster_trading": set(),
            "known_aster_pending": set(),
        }


def save_state(path: str, state: dict):
    tmp = path + ".tmp"
    serializable = {
        "known_spot_universe": sorted(state["known_spot_universe"]),
        "known_perp_universe": sorted(state["known_perp_universe"]),
        "last_spot_auction": state["last_spot_auction"],
        "last_perp_auction": state["last_perp_auction"],
    }
    serializable["known_paradex_live"] = sorted(state["known_paradex_live"])
    serializable["known_paradex_upcoming"] = sorted(state["known_paradex_upcoming"])
    serializable["known_aster_trading"] = sorted(state["known_aster_trading"])
    serializable["known_aster_pending"] = sorted(state["known_aster_pending"])
    with open(tmp, "w") as f:
        json.dump(serializable, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _post(payload: dict):
    try:
        r = requests.post(API_URL, json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(Fore.RED + f"[ERROR] API request failed: {e}")
        return None
    except ValueError as e:
        print(Fore.RED + f"[ERROR] JSON decode failed: {e}")
        return None


def fetch_spot_meta():
    return _post({"type": "spotMeta"})


def fetch_perp_meta():
    return _post({"type": "meta"})


def fetch_spot_auction():
    return _post({"type": "spotPairDeployAuctionStatus"})


def fetch_perp_auction():
    return _post({"type": "perpDeployAuctionStatus"})


def fetch_spot_deploy_state(wallet: str):
    return _post({"type": "spotDeployState", "user": wallet})


def fetch_paradex_markets():
    try:
        r = requests.get(f"{PARADEX_API_URL}/markets", timeout=10)
        r.raise_for_status()
        return r.json().get("results", [])
    except requests.RequestException as e:
        print(Fore.RED + f"[ERROR] Paradex API request failed: {e}")
        return None
    except ValueError as e:
        print(Fore.RED + f"[ERROR] Paradex JSON decode failed: {e}")
        return None


def fetch_aster_markets():
    symbols = []
    for url, label in [(ASTER_FAPI_URL, "futures"), (ASTER_SAPI_URL, "spot")]:
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            for s in r.json().get("symbols", []):
                s["_market_type"] = label
                symbols.append(s)
        except requests.RequestException as e:
            print(Fore.RED + f"[ERROR] AsterDex {label} API request failed: {e}")
        except ValueError as e:
            print(Fore.RED + f"[ERROR] AsterDex {label} JSON decode failed: {e}")
    return symbols or None


# ---------------------------------------------------------------------------
# Diff + display
# ---------------------------------------------------------------------------

def _fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "N/A"


def check_spot_listings(state: dict, data: dict):
    universe = data.get("universe", [])
    tokens = data.get("tokens", [])

    current_names = {item["name"] for item in universe}

    if not state["known_spot_universe"]:
        # First run — seed without alerting
        return current_names, True

    new_names = current_names - state["known_spot_universe"]
    removed_names = state["known_spot_universe"] - current_names

    for name in sorted(new_names):
        entry = next((u for u in universe if u["name"] == name), {})
        token_indices = entry.get("tokens", [])
        token_info = tokens[token_indices[0]] if token_indices and token_indices[0] < len(tokens) else {}

        token_id = token_info.get('tokenId', 'N/A')
        sz_dec = entry.get('szDecimals', 'N/A')
        canonical = token_info.get('isCanonical', 'N/A')
        detected = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        print(Fore.GREEN + Style.BRIGHT + f"\n{'='*60}")
        print(Fore.GREEN + Style.BRIGHT + f"  NEW SPOT LISTING DETECTED: {name}")
        print(Fore.GREEN + Style.BRIGHT + f"{'='*60}")
        print(Fore.GREEN + f"  Token ID   : {token_id}")
        print(Fore.GREEN + f"  sz Decimals: {sz_dec}")
        print(Fore.GREEN + f"  Canonical  : {canonical}")
        print(Fore.GREEN + f"  Detected   : {detected}")

        tg_send(
            f"🟢 <b>NEW SPOT LISTING</b>\n"
            f"Token: <b>{name}</b>\n"
            f"Token ID: <code>{token_id}</code>\n"
            f"sz Decimals: {sz_dec}\n"
            f"Canonical: {canonical}\n"
            f"Detected: {detected}"
        )

    for name in sorted(removed_names):
        print(Fore.YELLOW + f"\n[SPOT DELISTED] {name} removed from spot universe.")
        tg_send(f"🔴 <b>SPOT DELISTED</b>\nToken: <b>{name}</b>\nRemoved from spot universe.")

    return current_names, False


def check_perp_listings(state: dict, data: dict):
    universe = data.get("universe", [])

    current_names = {item["name"] for item in universe}

    if not state["known_perp_universe"]:
        return current_names, True

    new_names = current_names - state["known_perp_universe"]
    removed_names = state["known_perp_universe"] - current_names

    for name in sorted(new_names):
        entry = next((u for u in universe if u["name"] == name), {})

        max_lev = entry.get('maxLeverage', 'N/A')
        sz_dec = entry.get('szDecimals', 'N/A')
        detected = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        print(Fore.CYAN + Style.BRIGHT + f"\n{'='*60}")
        print(Fore.CYAN + Style.BRIGHT + f"  NEW PERP LISTING DETECTED: {name}")
        print(Fore.CYAN + Style.BRIGHT + f"{'='*60}")
        print(Fore.CYAN + f"  Max Leverage: {max_lev}x")
        print(Fore.CYAN + f"  sz Decimals : {sz_dec}")
        print(Fore.CYAN + f"  Detected    : {detected}")

        tg_send(
            f"🔵 <b>NEW PERP LISTING</b>\n"
            f"Token: <b>{name}</b>\n"
            f"Max Leverage: {max_lev}x\n"
            f"sz Decimals: {sz_dec}\n"
            f"Detected: {detected}"
        )

    for name in sorted(removed_names):
        print(Fore.YELLOW + f"\n[PERP DELISTED] {name} removed from perp universe.")
        tg_send(f"🔴 <b>PERP DELISTED</b>\nToken: <b>{name}</b>\nRemoved from perp universe.")

    return current_names, False


def display_spot_auction(state: dict, data: dict):
    if not data:
        return

    current_gas = str(data.get("currentGas", ""))
    prev_gas = str(state["last_spot_auction"].get("currentGas", ""))

    if current_gas == prev_gas and state["last_spot_auction"]:
        return  # No change, skip

    start_ts = data.get("startTimeSeconds", 0)
    duration = data.get("durationSeconds", 0)
    end_ts = start_ts + duration if start_ts and duration else 0

    print(Fore.YELLOW + f"\n[SPOT PAIR AUCTION STATUS CHANGED]")
    print(Fore.YELLOW + f"  Start     : {_fmt_time(start_ts)}")
    print(Fore.YELLOW + f"  End       : {_fmt_time(end_ts)}")
    print(Fore.YELLOW + f"  Start Gas : {data.get('startGas', 'N/A')}")
    print(Fore.YELLOW + f"  Current   : {current_gas}")
    print(Fore.YELLOW + f"  End Gas   : {data.get('endGas', 'N/A')}")

    state["last_spot_auction"] = data


def display_perp_auction(state: dict, data: dict):
    if not data:
        return

    current_gas = str(data.get("currentGas", ""))
    prev_gas = str(state["last_perp_auction"].get("currentGas", ""))

    if current_gas == prev_gas and state["last_perp_auction"]:
        return

    start_ts = data.get("startTimeSeconds", 0)
    duration = data.get("durationSeconds", 0)
    end_ts = start_ts + duration if start_ts and duration else 0

    print(Fore.YELLOW + f"\n[PERP DEPLOY AUCTION STATUS CHANGED]")
    print(Fore.YELLOW + f"  Start     : {_fmt_time(start_ts)}")
    print(Fore.YELLOW + f"  End       : {_fmt_time(end_ts)}")
    print(Fore.YELLOW + f"  Start Gas : {data.get('startGas', 'N/A')}")
    print(Fore.YELLOW + f"  Current   : {current_gas}")
    print(Fore.YELLOW + f"  End Gas   : {data.get('endGas', 'N/A')}")

    state["last_perp_auction"] = data


def display_deploy_state(data: dict, wallet: str):
    if not data:
        return

    states = data if isinstance(data, list) else data.get("states", [])

    if not states:
        print(Style.DIM + f"  [Deploy Pipeline] No active deployments for {wallet}")
        return

    print(Fore.WHITE + f"\n[DEPLOY PIPELINE] {len(states)} active deployment(s) for {wallet[:10]}...")
    for entry in states:
        spec = entry.get("spec", {})
        gas = entry.get("gasAuction", {})
        spots = entry.get("spots", [])

        print(Fore.WHITE + f"  Token     : {spec.get('name', 'N/A')} ({entry.get('fullName', 'N/A')})")
        print(Fore.WHITE + f"  Token ID  : {entry.get('token', 'N/A')}")
        print(Fore.WHITE + f"  Max Supply: {entry.get('maxSupply', 'N/A')}")
        print(Fore.WHITE + f"  Spot Pairs: {len(spots)}")

        if gas:
            start_ts = gas.get("startTimeSeconds", 0)
            duration = gas.get("durationSeconds", 0)
            print(Fore.WHITE + f"  Auction Start : {_fmt_time(start_ts)}")
            print(Fore.WHITE + f"  Auction End   : {_fmt_time(start_ts + duration) if start_ts and duration else 'N/A'}")
            print(Fore.WHITE + f"  Current Gas   : {gas.get('currentGas', 'N/A')}")
        print()


def check_paradex_listings(state: dict, markets: list):
    now_ms = int(time.time() * 1000)

    live = {m["symbol"] for m in markets if m.get("open_at", now_ms) <= now_ms}
    upcoming = {m["symbol"] for m in markets if m.get("open_at", now_ms) > now_ms}
    by_symbol = {m["symbol"]: m for m in markets}

    is_first_run = not state["known_paradex_live"] and not state["known_paradex_upcoming"]

    if is_first_run:
        state["known_paradex_live"] = live
        state["known_paradex_upcoming"] = upcoming
        print(Fore.WHITE + f"[INIT] Paradex: seeded {len(live)} live / {len(upcoming)} upcoming markets.")
        if upcoming:
            print(Fore.MAGENTA + f"  Upcoming markets already scheduled:")
            for sym in sorted(upcoming):
                m = by_symbol[sym]
                open_at = _fmt_time(m["open_at"] // 1000)
                print(Fore.MAGENTA + f"    {sym}  opens at {open_at}")
        return

    # New upcoming (scheduled in advance — the early signal)
    new_upcoming = upcoming - state["known_paradex_upcoming"] - state["known_paradex_live"]
    for sym in sorted(new_upcoming):
        m = by_symbol[sym]
        open_at_ts = m.get("open_at", 0) // 1000
        open_at_str = _fmt_time(open_at_ts)
        kind = m.get("asset_kind", "N/A")
        base = m.get("base_currency", "N/A")

        print(Fore.MAGENTA + Style.BRIGHT + f"\n{'='*60}")
        print(Fore.MAGENTA + Style.BRIGHT + f"  PARADEX UPCOMING LISTING: {sym}")
        print(Fore.MAGENTA + Style.BRIGHT + f"{'='*60}")
        print(Fore.MAGENTA + f"  Type      : {kind}")
        print(Fore.MAGENTA + f"  Base      : {base}")
        print(Fore.MAGENTA + f"  Opens at  : {open_at_str}")
        print(Fore.MAGENTA + f"  Detected  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        tg_send(
            f"🟣 <b>PARADEX UPCOMING LISTING</b>\n"
            f"Market: <b>{sym}</b>\n"
            f"Type: {kind} | Base: {base}\n"
            f"Opens at: {open_at_str}"
        )

    # Went live (either from upcoming or brand new)
    new_live = live - state["known_paradex_live"]
    for sym in sorted(new_live):
        m = by_symbol[sym]
        kind = m.get("asset_kind", "N/A")
        base = m.get("base_currency", "N/A")
        was_upcoming = sym in state["known_paradex_upcoming"]
        detected = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        label = "PARADEX LISTING NOW LIVE" if was_upcoming else "PARADEX NEW LISTING"
        print(Fore.GREEN + Style.BRIGHT + f"\n{'='*60}")
        print(Fore.GREEN + Style.BRIGHT + f"  {label}: {sym}")
        print(Fore.GREEN + Style.BRIGHT + f"{'='*60}")
        print(Fore.GREEN + f"  Type      : {kind}")
        print(Fore.GREEN + f"  Base      : {base}")
        print(Fore.GREEN + f"  Detected  : {detected}")

        tg_send(
            f"{'🟢' if was_upcoming else '🔵'} <b>{label}</b>\n"
            f"Market: <b>{sym}</b>\n"
            f"Type: {kind} | Base: {base}\n"
            f"Detected: {detected}"
        )

    state["known_paradex_live"] = live
    state["known_paradex_upcoming"] = upcoming


def check_aster_listings(state: dict, symbols: list):
    trading = {s["symbol"] for s in symbols if s.get("status") == "TRADING"}
    pending = {s["symbol"] for s in symbols if s.get("status") == "PENDING_TRADING"}
    by_symbol = {s["symbol"]: s for s in symbols}

    is_first_run = not state["known_aster_trading"] and not state["known_aster_pending"]

    if is_first_run:
        state["known_aster_trading"] = trading
        state["known_aster_pending"] = pending
        print(Fore.WHITE + f"[INIT] AsterDex: seeded {len(trading)} trading / {len(pending)} pending markets.")
        if pending:
            print(Fore.YELLOW + "  Pending markets already in queue:")
            for sym in sorted(pending):
                s = by_symbol[sym]
                print(Fore.YELLOW + f"    {sym}  ({s.get('_market_type', 'N/A')})")
        return

    # New PENDING_TRADING — earliest possible signal
    new_pending = pending - state["known_aster_pending"] - state["known_aster_trading"]
    for sym in sorted(new_pending):
        s = by_symbol[sym]
        base = s.get("baseAsset", "N/A")
        quote = s.get("quoteAsset", "N/A")
        kind = s.get("_market_type", "N/A")
        detected = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        print(Fore.YELLOW + Style.BRIGHT + f"\n{'='*60}")
        print(Fore.YELLOW + Style.BRIGHT + f"  ASTERDEX PENDING LISTING: {sym}")
        print(Fore.YELLOW + Style.BRIGHT + f"{'='*60}")
        print(Fore.YELLOW + f"  Type      : {kind}")
        print(Fore.YELLOW + f"  Base      : {base}  Quote: {quote}")
        print(Fore.YELLOW + f"  Detected  : {detected}")

        tg_send(
            f"🟡 <b>ASTERDEX PENDING LISTING</b>\n"
            f"Market: <b>{sym}</b>\n"
            f"Type: {kind} | Base: {base} / {quote}\n"
            f"Status: PENDING_TRADING\n"
            f"Detected: {detected}"
        )

    # Went live
    new_trading = trading - state["known_aster_trading"]
    for sym in sorted(new_trading):
        s = by_symbol[sym]
        base = s.get("baseAsset", "N/A")
        quote = s.get("quoteAsset", "N/A")
        kind = s.get("_market_type", "N/A")
        contract = s.get("contractType", "N/A")
        was_pending = sym in state["known_aster_pending"]
        detected = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        label = "ASTERDEX NOW LIVE" if was_pending else "ASTERDEX NEW LISTING"
        print(Fore.GREEN + Style.BRIGHT + f"\n{'='*60}")
        print(Fore.GREEN + Style.BRIGHT + f"  {label}: {sym}")
        print(Fore.GREEN + Style.BRIGHT + f"{'='*60}")
        print(Fore.GREEN + f"  Type      : {kind} ({contract})")
        print(Fore.GREEN + f"  Base      : {base}  Quote: {quote}")
        print(Fore.GREEN + f"  Detected  : {detected}")

        tg_send(
            f"{'🟢' if was_pending else '🔵'} <b>{label}</b>\n"
            f"Market: <b>{sym}</b>\n"
            f"Type: {kind} ({contract}) | {base}/{quote}\n"
            f"Detected: {detected}"
        )

    # Delistings
    removed = state["known_aster_trading"] - trading - pending
    for sym in sorted(removed):
        print(Fore.RED + f"\n[ASTERDEX DELISTED] {sym}")
        tg_send(f"🔴 <b>ASTERDEX DELISTED</b>\nMarket: <b>{sym}</b>")

    state["known_aster_trading"] = trading
    state["known_aster_pending"] = pending


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def poll_once(state: dict, wallet: str, state_file: str, is_init: bool):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(Style.DIM + f"\n--- [{timestamp}] Polling ---")

    spot_data = fetch_spot_meta()
    perp_data = fetch_perp_meta()
    spot_auction = fetch_spot_auction()
    perp_auction = fetch_perp_auction()
    deploy_state = fetch_spot_deploy_state(wallet)
    paradex_markets = fetch_paradex_markets()
    aster_symbols = fetch_aster_markets()

    seeded = False

    if spot_data:
        new_spot, spot_seeded = check_spot_listings(state, spot_data)
        state["known_spot_universe"] = new_spot
        if spot_seeded:
            seeded = True

    if perp_data:
        new_perp, perp_seeded = check_perp_listings(state, perp_data)
        state["known_perp_universe"] = new_perp
        if perp_seeded:
            seeded = True

    if seeded:
        n_spot = len(state["known_spot_universe"])
        n_perp = len(state["known_perp_universe"])
        print(Fore.WHITE + f"[INIT] Seeded {n_spot} spot / {n_perp} perp listings. Monitoring for new additions.")

    display_spot_auction(state, spot_auction)
    display_perp_auction(state, perp_auction)
    display_deploy_state(deploy_state, wallet)
    if paradex_markets is not None:
        check_paradex_listings(state, paradex_markets)
    if aster_symbols is not None:
        check_aster_listings(state, aster_symbols)

    save_state(state_file, state)


def main():
    args = parse_args()

    wallet = os.environ.get("HYPER_ADDR") or os.environ.get("WALLET_ADDRESS")
    if not wallet:
        print(Fore.RED + "[ERROR] No wallet address found. Set HYPER_ADDR or WALLET_ADDRESS in .env")
        raise SystemExit(1)

    state = load_state(args.state_file)
    is_init = not bool(state["known_spot_universe"])

    print(Style.BRIGHT + "Listing Monitor — Hyperliquid | Paradex | AsterDex")
    print(f"  Wallet  : {wallet}")
    print(f"  Interval: {args.interval}s")
    print(f"  State   : {args.state_file}")
    print()

    tg_send("✅ <b>Listing Monitor started</b>\nWatching: Hyperliquid | Paradex | AsterDex")

    try:
        while True:
            poll_once(state, wallet, args.state_file, is_init)
            is_init = False
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(Fore.YELLOW + "\nShutting down.")


if __name__ == "__main__":
    main()
