---
name: flooor-loopers-daily
description: Automate the flooor.fun daily vault for an NFT collection — sign during the sign phase, claim during the claim phase, once per UTC day, for every token a wallet holds. Use when asked to automate a flooor.fun vault, claim a Looper daily share, schedule signOrClaim, or debug a missed sign or claim window.
---

# flooor.fun daily vault — sign and claim

flooor.fun routes a share of a collection's sale volume into a vault that resets
every UTC day. Holders who **sign** during the sign phase split the pool when they
**claim** during the claim phase. Both actions are the same contract function, and
both must happen inside the same UTC day or that day's share is gone.

This skill automates the full loop: resolve which tokens a wallet holds, decide
whether there is anything to do, and submit one call per token.

## The clock is fixed to UTC midnight

The vault contract stores its period as minutes and derives the epoch by modulo:

```
rBLOCKS   = 1440 minutes (86400s)   epochStart = ts - (ts % 86400)
sDURATION =  960 minutes (57600s)
```

Epochs are hard-aligned to UTC midnight. Nothing an operator does moves the clock.

| Phase | Window (UTC) |
|---|---|
| Sign  | 00:00 – 16:00 |
| Claim | 16:00 – 24:00 |

The `_signed` mapping is keyed on `epochStart`, so **a sign only counts for a claim
in the same UTC day**. Miss 16:00 and the sign is worthless; miss 24:00 and the
claim is gone. Confirm `sDURATION` for your collection before trusting the table —
it is a per-deployment parameter, not a protocol constant.

## Steps

1. **Find the vault for your collection.** Do not reuse an address from a doc or a
   plugin README. Each collection gets its own deployment, and a widely-circulated
   flooor plugin doc hardcodes one specific collection's vault. Calling the wrong
   vault with your token reverts `Not owner of tokenId`. Verify by reading
   `collectionId()` on the vault and matching it to your collection.

2. **Configure the run.** Copy `.env.example` to `.env` and set `LOOPERS_VAULT`,
   `LOOPERS_NFT`, and a wallet. Confirm `python3 scripts/loopers.py status` prints
   your tokens and a non-zero `signers` count.

3. **Dry-run first.** `python3 scripts/loopers.py run` decides and prints without
   submitting. Confirm the printed action matches the phase you are in.

4. **Execute.** `python3 scripts/loopers.py run --execute` submits. This sends a
   transaction and costs gas. Confirm the printed `summary:` line lists your tokens
   under `acted=`.

5. **Schedule two runs per day**, one in each phase, with margin from the boundary:

   ```
   10 0  * * *   run --execute     # sign,  00:10 UTC
   10 16 * * *   run --execute     # claim, 16:10 UTC
   ```

   `run` is the same command in both phases — the contract picks the branch from
   `block.timestamp`. Confirm both fire by checking that the state file gains a
   `sign` and a `claim` entry under the same epoch key.

## Idempotency comes from simulation, not from stored state

There is no public view for "has this token signed". So before every submit the
script `eth_call`s the real calldata and reads the revert string:

| Revert | Meaning |
|---|---|
| *(no revert)* | actionable — submit |
| `token already signed` | benign, this epoch's sign is done |
| `already claimed` | benign, this epoch's claim is done |
| `token not signed` | the sign phase was missed; nothing to claim |
| `zero share` | benign, `pool/partCount` rounded to 0 on a quiet day |
| `Not owner of tokenId` | the token left the wallet, or wrong vault — real problem |

This makes the script safe to run repeatedly. The state file is a log for humans,
never the source of truth — delete it and the next run still behaves correctly.

Do not alert on `zero share`. The value rolls into the next epoch.

## Token discovery is not on-chain

The NFT is typically **not** `ERC721Enumerable`, so `tokenOfOwnerByIndex` reverts
and there is no on-chain way to walk a holder's tokens. `ownerOf` is the only
truth; the cached list in the state file just narrows the search. When the cache
count disagrees with `balanceOf`, the script rediscovers from a block explorer:

```
GET https://base.blockscout.com/api/v2/tokens/<NFT>/instances?holder_address_hash=<WALLET>
```

Blockscout's v1 `module=account&action=tokentx` returns nothing for these
collections — do not use it. Net effect: transfer a token in or out and the next
run picks it up with no edit.

## Multiple tokens each earn a share

One `signOrClaim` per token. The contract's only gate is `ownerOf(tokenId) ==
msg.sender` and `partCount` increments per token, so every token held earns its
own share. The "you must hold exactly 1 NFT" rule people repeat is a frontend
convention, not a contract rule.

## Signing is pluggable

`scripts/loopers.py` submits through whichever path you configure:

- `LOOPERS_PRIVATE_KEY` — sign locally with `eth-account`. Simplest, and what most
  readers want. Use a burner that holds only gas.
- `LOOPERS_SUBMIT_CMD` — hand the transaction to an external signer. The script
  runs `<cmd> <json>` where the JSON is `{to, chainId, value, data}` and scrapes a
  `0x…` hash from stdout. Use this to keep keys in a CLI wallet, an HSM, or a
  custodial API instead of in an env var.

If your signer path is an API with a restricted key, expect raw-calldata submission
to be refused — restricted keys generally cannot verify a recipient from calldata
and need an unrestricted key for this call.

## Economics — check before you automate

The share is `pool / partCount`, and `partCount` grows as holders wake up. Measure
your own collection with `status` before scheduling:

```
per_nft_eth   = pool_eth / signers
```

Two real observations, two days apart on the same vault:

| Date (UTC) | Pool | Signers | Per token |
|---|---|---|---|
| 2026-09-19 | 0.0202 ETH | 182 | ~0.00011 ETH |
| 2026-09-21 | 0.0144 ETH | 280 | ~0.000052 ETH |

Half the value in two days — the pool fell and the denominator grew at the same
time. Against ~69k gas per call this is still positive on Base, but it is thin and
trending down as holders wake up. On a chain with real gas costs it is negative.
Re-measure before you assume yesterday's number holds.

## Pitfalls

- **Cron shells do not inherit your interactive `PATH`.** If your submit command
  lives in a user-local bin dir, resolve it absolutely. Verify with a stripped
  environment: `env -i HOME=$HOME PATH=/usr/bin:/bin python3 scripts/loopers.py status`
- **Public RPCs rate-limit mid-run.** The script rotates endpoints on transport
  errors and propagates reverts immediately — a revert is an answer, not a failure
  to retry. Add your own endpoint to `LOOPERS_RPCS` if you run this often.
- **Do not automate vault settlement.** These vaults expose a separate call that
  sells the accumulated position to the highest bidder. It is a pricing decision,
  not a chore. Automating a chore is safe; automating a sale is not.
- **Do not point a scheduler at a signing command you have not dry-run.** Run
  without `--execute` at least once in each phase first.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | nothing to do, or everything submitted |
| 1 | a submit failed, or a held token could not be resolved |
| 2 | the wallet holds no tokens in the collection |
| 3 | the sign phase was missed, so nothing was claimable |

Worst outcome across all tokens wins. Alert on 1 and 2; 3 is worth a notice.
