# flooor-loopers-daily

An agent skill that automates the [flooor.fun](https://flooor.fun) daily vault:
**sign** during the sign phase, **claim** during the claim phase, once per UTC day,
for every token a wallet holds.

flooor.fun routes a share of a collection's sale volume into a vault that resets at
UTC midnight. Holders who sign during the sign window split the pool when they claim
during the claim window. Both are the same contract call, and a sign only counts for
a claim **in the same UTC day** — miss the cutoff and that day's share is gone.

That makes it a chore, and chores should be scheduled.

## What's here

| File | What it is |
|---|---|
| `SKILL.md` | The skill. Drop it into any agent that loads [Agent Skills](https://docs.claude.com/en/docs/agents-and-tools/agent-skills). |
| `scripts/loopers.py` | The driver. Resolves held tokens, decides by simulation, submits. |
| `.env.example` | Configuration. Copy to `.env` and fill in. |

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env     # set LOOPERS_VAULT, LOOPERS_NFT, and a signing path

set -a && . ./.env && set +a
python3 scripts/loopers.py status         # phase, pool, whether there's anything to do
python3 scripts/loopers.py run            # dry run
python3 scripts/loopers.py run --execute  # submits — this costs gas
```

Then schedule two runs a day, one per phase:

```
10 0  * * *  cd /path/to/repo && set -a && . ./.env && set +a && python3 scripts/loopers.py run --execute
10 16 * * *  cd /path/to/repo && set -a && . ./.env && set +a && python3 scripts/loopers.py run --execute
```

`run` is the same command in both phases — the contract picks the branch from
`block.timestamp`. It is safe to run repeatedly.

## Why this isn't just a cron and a curl

Three things are non-obvious, and each one of them costs you a day's share:

**The vault address is per-collection.** A widely-circulated flooor plugin doc
hardcodes one specific collection's vault. Call it with your token and you get
`Not owner of tokenId`. Verify with `collectionId()`.

**There is no "have I signed?" view.** Idempotency comes from `eth_call`-ing the
real calldata and reading the revert string — `token already signed`, `already
claimed`, `token not signed`, `zero share`. The state file is a log for humans, not
the source of truth. Delete it and the script still behaves correctly.

**Token discovery is not on-chain.** These collections are not `ERC721Enumerable`,
so `tokenOfOwnerByIndex` reverts. `ownerOf` is the truth and a block explorer's
holder-scoped instances endpoint is the discovery path. Blockscout's v1 `tokentx`
returns nothing for these collections.

Full reasoning, the phase math, the exit codes and the rest of the pitfalls are in
[`SKILL.md`](SKILL.md).

## Signing is pluggable

Set **one** of:

- `LOOPERS_PRIVATE_KEY` — sign locally with `eth-account`. Use a burner that holds
  only gas.
- `LOOPERS_SUBMIT_CMD` — hand `{to, chainId, value, data}` to your own signer as a
  JSON argv, and the script scrapes the tx hash from stdout. Keeps keys in a CLI
  wallet, an HSM, or a custodial API instead of an env var.

No keys are stored by this repo, and `.env` and `state.json` are gitignored.

## Check the economics first

The share is `pool / partCount`, and `partCount` grows as holders wake up. Run
`status` and read `per_nft_eth` before you automate anything. Two observations on the
same vault, two days apart: 0.0202 ETH against 182 signers (~0.00011 ETH/token) on
2026-09-19, then 0.0144 ETH against 280 signers (~0.000052 ETH/token) on 2026-09-21.
Half the value in two days. Against ~69k gas that's still positive on Base, but it's
thin and trending down. On an expensive chain it's negative.

## Not automated on purpose

These vaults expose a separate call that sells the accumulated position to the
highest bidder. That's a pricing decision, not a chore. This skill will not do it and
you shouldn't schedule it either.

## License

MIT. No affiliation with flooor.fun or any collection. Read the contract yourself
before pointing a scheduler with a funded key at it.
