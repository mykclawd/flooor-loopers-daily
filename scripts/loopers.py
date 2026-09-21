#!/usr/bin/env python3
"""
flooor.fun daily vault — sign / claim driver.

The vault contract is a pure UTC clock:

    rBLOCKS   = 1440 minutes (86400s)  -> epochStart = ts - (ts % 86400)
    sDURATION =  960 minutes (57600s)  -> sign 00:00-16:00 UTC, claim 16:00-24:00 UTC

`signOrClaim(tokenId)` is the SAME function for both phases; which branch runs is
decided by `block.timestamp % 86400`. There is no public view for whether a token
has signed, so idempotency comes from an `eth_call` simulation of the real call:
the revert string tells us exactly where we stand.

    "token already signed"  -> benign, this epoch's sign is done
    "already claimed"       -> benign, this epoch's claim is done
    "token not signed"      -> we missed the sign phase; nothing to claim
    "zero share"            -> benign, pool/participants rounded to 0 this epoch
    "Not owner of tokenId"  -> the NFT left the wallet, or wrong vault

Configuration is entirely by environment (see .env.example):

    LOOPERS_VAULT         vault contract for YOUR collection (required)
    LOOPERS_NFT           the NFT contract (required)
    LOOPERS_CHAIN_ID      default 8453 (Base)
    LOOPERS_RPCS          comma-separated RPC URLs
    LOOPERS_EXPLORER      Blockscout base URL, for token discovery
    LOOPERS_STATE         state file path, default ./state.json
    LOOPERS_SIGN_SECS     override sDURATION in seconds, default 57600

    Wallet — exactly one of:
    LOOPERS_PRIVATE_KEY   sign locally with eth-account
    LOOPERS_SUBMIT_CMD    external signer; run as `<cmd> <json>`, plus
                          LOOPERS_WALLET so we know whose tokens to look for

Usage:
    python3 loopers.py status
    python3 loopers.py run              # dry run, decides and prints
    python3 loopers.py run --execute    # submits if there is something to do
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.parse
import urllib.request
import sys
import time

from web3 import Web3


def _req(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"{name} is not set; copy .env.example to .env and fill it in")
    return val


CONTRACT = Web3.to_checksum_address(_req("LOOPERS_VAULT"))
NFT = Web3.to_checksum_address(_req("LOOPERS_NFT"))
CHAIN_ID = int(os.environ.get("LOOPERS_CHAIN_ID", "8453"))

RPCS = [u.strip() for u in os.environ.get(
    "LOOPERS_RPCS",
    "https://mainnet.base.org,https://base.drpc.org,https://base.llamarpc.com",
).split(",") if u.strip()]

EXPLORER = os.environ.get("LOOPERS_EXPLORER", "https://base.blockscout.com").rstrip("/")

STATE_PATH = os.environ.get("LOOPERS_STATE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state.json")

EPOCH_SECS = 86400
SIGN_SECS = int(os.environ.get("LOOPERS_SIGN_SECS", "57600"))

SEL_SIGN_OR_CLAIM = "0x4abd3ac1"
SEL_IS_SIGN_PHASE = "0x73c87a52"
SEL_POOL = "0xd8e84946"
SEL_EPOCH_ID = "0xeacdc5ff"

# Revert substrings that mean "nothing to do", not "something broke".
BENIGN = ("token already signed", "already claimed", "token not signed", "zero share")

PRIVATE_KEY = os.environ.get("LOOPERS_PRIVATE_KEY")
SUBMIT_CMD = os.environ.get("LOOPERS_SUBMIT_CMD")


def _wallet():
    """Whose tokens are we acting on."""
    if PRIVATE_KEY:
        from eth_account import Account
        return Web3.to_checksum_address(Account.from_key(PRIVATE_KEY).address)
    explicit = os.environ.get("LOOPERS_WALLET")
    if explicit:
        return Web3.to_checksum_address(explicit)
    sys.exit("set LOOPERS_PRIVATE_KEY, or LOOPERS_SUBMIT_CMD together with LOOPERS_WALLET")


WALLET = _wallet()


class Chain:
    """RPC pool with failover.

    Public endpoints 429 without warning partway through a run, so every call
    rotates across the pool rather than binding to one provider at connect time.
    A revert is a real answer and must propagate immediately — only transport
    errors are retried.
    """

    def __init__(self, urls=None, attempts=3):
        urls = urls or RPCS
        self.providers = [Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 20}))
                          for u in urls]
        self.urls = urls
        self.attempts = attempts
        self.i = 0

    def _rotate(self):
        self.i = (self.i + 1) % len(self.providers)

    def call(self, tx):
        last = None
        for _ in range(self.attempts * len(self.providers)):
            w = self.providers[self.i]
            try:
                return w.eth.call(tx)
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                if "execution reverted" in text:
                    raise
                last = exc
                self._rotate()
                time.sleep(0.6)
        raise SystemExit(f"no RPC reachable: {last}")

    def provider(self):
        return self.providers[self.i]

    def block_number(self):
        last = None
        for _ in range(self.attempts * len(self.providers)):
            try:
                return self.providers[self.i].eth.block_number
            except Exception as exc:  # noqa: BLE001
                last = exc
                self._rotate()
                time.sleep(0.6)
        raise SystemExit(f"no RPC reachable: {last}")


def connect():
    c = Chain()
    c.block_number()
    return c


def read_uint(w, selector):
    return int.from_bytes(w.call({"to": CONTRACT, "data": selector}), "big")


def calldata(token_id):
    return SEL_SIGN_OR_CLAIM + f"{token_id:064x}"


def owner_of(w, token_id):
    raw = w.call({"to": NFT, "data": "0x6352211e" + f"{token_id:064x}"})
    return Web3.to_checksum_address(raw[-20:].hex())


def balance_of(w):
    raw = w.call({"to": NFT, "data": "0x70a08231" + f"{int(WALLET, 16):064x}"})
    return int.from_bytes(raw, "big")


def discover_tokens():
    """Find tokens held by WALLET from the explorer's instance index.

    Used when the cached list disagrees with balanceOf, since these collections
    are not ERC721Enumerable. Returns [] on any failure — callers fall back to
    the cache.
    """
    # Blockscout's v1 `tokentx` returns nothing for these collections; the v2
    # instances endpoint, scoped by holder, is the one that works.
    url = (f"{EXPLORER}/api/v2/tokens/{NFT}/instances"
           f"?holder_address_hash={WALLET}")
    seen = []
    try:
        for _ in range(5):  # bounded pagination
            req = urllib.request.Request(url, headers={"User-Agent": "flooor-loopers-daily"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
            for row in payload.get("items") or []:
                try:
                    tid = int(row["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if tid not in seen:
                    seen.append(tid)
            nxt = payload.get("next_page_params")
            if not nxt:
                break
            url = (f"{EXPLORER}/api/v2/tokens/{NFT}/instances"
                   f"?holder_address_hash={WALLET}&"
                   + urllib.parse.urlencode(nxt))
    except Exception:  # noqa: BLE001 - discovery is best effort
        return seen
    return seen


def resolve_tokens(w):
    """Authoritative list of tokens this wallet holds right now.

    ownerOf is the only source of truth. The cache just narrows the search.
    Returns (tokens, complete, balance) — complete is False when we found fewer
    than balanceOf, meaning something is held that we cannot see.
    """
    cached = load_state().get("tokens") or []
    candidates = list(dict.fromkeys(cached))
    tokens = [t for t in candidates if owner_of(w, t) == WALLET]

    want = balance_of(w)
    if len(tokens) != want:
        for tid in discover_tokens():
            if tid not in tokens and owner_of(w, tid) == WALLET:
                tokens.append(tid)

    tokens.sort()
    state = load_state()
    state["tokens"] = tokens
    save_state(state)
    return tokens, len(tokens) == want, want


def epoch_start(ts=None):
    ts = int(ts if ts is not None else time.time())
    return ts - (ts % EPOCH_SECS)


def phase(ts=None):
    ts = int(ts if ts is not None else time.time())
    return "sign" if (ts % EPOCH_SECS) < SIGN_SECS else "claim"


def simulate(w, token_id):
    """Return (ok, revert_reason)."""
    try:
        w.call({"from": WALLET, "to": CONTRACT, "data": calldata(token_id)})
        return True, None
    except Exception as exc:  # noqa: BLE001 - revert reason is the payload
        return False, str(exc)


def load_state():
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"epochs": {}}


def save_state(state):
    state.setdefault("epochs", {})
    with open(STATE_PATH, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


def record(action, token_id, tx_hash, note=None):
    state = load_state()
    key = str(epoch_start())
    entry = state.setdefault("epochs", {}).setdefault(key, {}).setdefault(str(token_id), {})
    entry[action] = {"tx": tx_hash, "at": int(time.time()), "note": note}
    save_state(state)


def submit_local(chain, token_id):
    """Sign and send with a local key."""
    from eth_account import Account

    acct = Account.from_key(PRIVATE_KEY)
    w3 = chain.provider()
    tx = {
        "to": CONTRACT,
        "value": 0,
        "data": calldata(token_id),
        "chainId": CHAIN_ID,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "from": acct.address,
    }
    try:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.25)
    except Exception as exc:  # noqa: BLE001
        return False, f"gas estimation failed: {exc}"
    try:
        fees = w3.eth.fee_history(5, "latest")
        base = fees["baseFeePerGas"][-1]
        tip = w3.eth.max_priority_fee
        tx["maxPriorityFeePerGas"] = tip
        tx["maxFeePerGas"] = base * 2 + tip
    except Exception:  # noqa: BLE001 - chain may be legacy-gas only
        tx["gasPrice"] = w3.eth.gas_price

    # `from` is needed for estimate_gas but older eth-account rejects it as an
    # unrecognized field at signing time. Drop it once estimation is done.
    tx.pop("from", None)
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    try:
        tx_hash = w3.eth.send_raw_transaction(raw).hex()
    except Exception as exc:  # noqa: BLE001
        return False, f"broadcast failed: {exc}"
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    try:
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    except Exception as exc:  # noqa: BLE001
        return False, f"sent {tx_hash} but receipt timed out: {exc}"
    if rcpt.get("status") != 1:
        return False, f"reverted on-chain: {tx_hash}"
    return True, tx_hash


def submit_external(token_id):
    """Hand the transaction to an external signer command."""
    argv = shlex.split(SUBMIT_CMD)
    binary = shutil.which(argv[0]) or argv[0]
    payload = json.dumps(
        {"to": CONTRACT, "chainId": CHAIN_ID, "value": "0", "data": calldata(token_id)}
    )
    try:
        proc = subprocess.run([binary, *argv[1:], payload],
                              capture_output=True, text=True, timeout=180)
    except (FileNotFoundError, OSError) as exc:
        return False, f"submit command not executable ({binary}): {exc}"
    except subprocess.TimeoutExpired:
        return False, "submit command timed out after 180s; check the explorer before retrying"

    out = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"0x[0-9a-fA-F]{64}", out)
    if proc.returncode == 0 and match:
        return True, match.group(0)
    return False, out.strip()[:500] or f"submit command exited {proc.returncode} with no output"


def submit(chain, token_id):
    if PRIVATE_KEY:
        return submit_local(chain, token_id)
    return submit_external(token_id)


def status(w):
    ts = int(time.time())
    remaining = (SIGN_SECS if phase(ts) == "sign" else EPOCH_SECS) - (ts % EPOCH_SECS)
    tokens, complete, want = resolve_tokens(w)
    pool = read_uint(w, SEL_POOL)
    signers = read_uint(w, "0x" + Web3.keccak(text="partCount(uint256)")[:4].hex()
                        + f"{epoch_start(ts):064x}")
    per = (pool / signers / 1e18) if signers else 0.0
    return {
        "utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)),
        "wallet": WALLET,
        "phase": phase(ts),
        "phase_ends_in": f"{remaining // 3600}h{(remaining % 3600) // 60:02d}m",
        "epoch_start": epoch_start(ts),
        "epoch_id": read_uint(w, SEL_EPOCH_ID),
        "pool_eth": pool / 1e18,
        "signers": signers,
        "per_nft_eth": per,
        "your_take_eth": per * len(tokens),
        "tokens": tokens,
        "balance_of": want,
        "tokens_complete": complete,
        "per_token": {t: simulate(w, t) for t in tokens},
    }


def cmd_status(args):
    w = connect()
    st = status(w)
    per_token = st.pop("per_token")
    for k, v in st.items():
        print(f"{k:16} {v}")
    for tid, (ok, reason) in per_token.items():
        short = "ACTIONABLE" if ok else (reason or "").split("'")[1:2] or [reason]
        print(f"  token {tid:<6} {short if ok else short[0]}")
    return 0


def cmd_run(args):
    chain = connect()
    st = status(chain)
    ph = st["phase"]
    print(f"[{st['utc']} UTC] phase={ph} ends in {st['phase_ends_in']} "
          f"pool={st['pool_eth']:.6f} ETH signers={st['signers']} "
          f"tokens={st['tokens']}")

    if not st["tokens"]:
        print(f"ALERT: {WALLET} holds no tokens in {NFT} (balanceOf={st['balance_of']}). "
              "Nothing to do.")
        return 2

    if not st["tokens_complete"]:
        print(f"WARNING: balanceOf={st['balance_of']} but only resolved "
              f"{len(st['tokens'])} token(s): {st['tokens']}. Proceeding with those; "
              "a held token may be going unsigned.")

    failed, missed, acted, skipped = [], [], [], []

    for tid, (ok, reason) in st["per_token"].items():
        if ok:
            if not args.execute:
                print(f"WOULD {ph.upper()} token {tid} (dry run; pass --execute)")
                acted.append(tid)
                continue
            sub_ok, result = submit(chain, tid)
            if sub_ok:
                print(f"{ph.upper()} OK token={tid} tx={result}")
                record(ph, tid, result)
                acted.append(tid)
            else:
                print(f"{ph.upper()} FAILED token={tid}: {result}")
                record(ph, tid, None, note=result[:200])
                failed.append(tid)
            continue

        reason = reason or ""
        hit = next((b for b in BENIGN if b in reason), None)
        if hit == "token not signed":
            print(f"token {tid}: missed this epoch's sign — nothing to claim")
            missed.append(tid)
        elif hit:
            print(f"token {tid}: nothing to do — {hit}")
            skipped.append(tid)
        else:
            print(f"token {tid}: UNEXPECTED REVERT: {reason}")
            failed.append(tid)

    print(f"summary: acted={acted} skipped={skipped} missed={missed} failed={failed}")

    # Worst outcome wins: a hard failure outranks a missed sign, which outranks
    # an incomplete token list.
    if failed:
        return 1
    if missed:
        return 3
    if not st["tokens_complete"]:
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    run = sub.add_parser("run")
    run.add_argument("--execute", action="store_true",
                     help="actually submit; without it the run is a dry run")
    run.set_defaults(fn=cmd_run)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
