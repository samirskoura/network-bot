# Snapchat complete headline bot — 80 Creatives

This package contains the full single-account bot, workflow, helper scripts and
instructions. It supports 1–20 selected Ad Squads within one Ad Account. A squad
can belong to any Campaign in that account. Use a separate repository for another
Ad Account.

**Updating your existing bot? Read `UPGRADE_EXISTING_BOT.txt`.** Keep its encrypted
state, encryption key, credentials and customized headline files. There is no need
to generate another refresh token merely to install this update.

## What the bot changes

The bot changes only a Creative's headline, up to 34 characters. An edit requires
all linked live Ads to be `REJECTED`, the Creative to be `DISAPPROVED`, and every
linked Ad to belong to your selected squads. Approved, pending and unknown review
statuses block the edit. Creatives shared with Ads outside the selection are skipped.

Before each edit, the bot refreshes the account's Ad list, then reads each linked
Ad and the Creative again. It sends conditional tests for the Creative's review
status and exact old headline in the same PATCH as the headline replacement.
Failed or incomplete responses stop processing. It does not remove these tests
or retry an unguarded edit.

Snapchat documents that a Creative edit affects all associated Ads. Its documented
PATCH conditions apply to one entity; the linked-Ad reads remain separate requests.
The guards cannot guarantee protection against every concurrent Ad status or link
change. See [Creative PATCH](https://developers.snap.com/marketing-api/Ads-API/creatives#patch-a-creative-patch)
and [conditional operations](https://developers.snap.com/marketing-api/Ads-API/api-patterns#supported-operations).

After an edit, the bot waits for review. A later confirmed rejection can become
eligible for a fresh headline. A recorded approval permanently protects that
Creative while the saved state is retained. The bot checks exact and near-duplicate
headlines against its stored history and validates generated alternatives.

## Limits and timing

| Setting | Behavior |
| --- | --- |
| `max_updates` | Up to 80 unique eligible Creatives across all selected squads per check |
| Manual form default | 80; enter 60, 1, or another value within 1–80 |
| Scheduled fallback | 30 per check |
| Per-Creative attempt limit | None; confirmed rejections can be retried while running |
| Overnight mode | Waits 60 seconds after each completed check, for roughly 330 minutes |
| Scheduled mode | Requests a run every five minutes when enabled; start times can be delayed |

The ceiling is not a guaranteed edit count. Eligibility and fresh-headline
availability determine the actual count. Status reads take time, particularly in
large accounts. Checks therefore do not start exactly every minute. OpenAI
generation requests contain at most 30 candidates each; 80 candidates use three
requests when all need generation.

## New bot setup

1. Extract this ZIP into a folder. Create a GitHub repository for this Ad Account.
2. Upload the files and folders inside the extracted folder. Confirm that
   `snap_headline_bot.py`, `state_crypto.py` and `requirements.txt` are at the
   repository root.
3. Confirm the workflow exists at
   `.github/workflows/snapchat-headline-editor.yml`. A YAML file at the repository
   root will not run. If browser upload did not include the folder, use **Add file
   → Create new file**, enter that full path, paste the supplied YAML and commit.
   See [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
4. Open **Settings → Secrets and variables → Actions → Secrets**. Add the values
   listed below. Credentials are entered here, not in the source files.

| Secret | Value |
| --- | --- |
| `SNAP_CLIENT_ID` | Your Snapchat OAuth Client ID |
| `SNAP_CLIENT_SECRET` | The matching OAuth Client Secret |
| `SNAP_REFRESH_TOKEN` | A token for that app and a user with access to the target account |
| `SNAP_AD_ACCOUNT_ID` | The one Ad Account UUID this bot will use |
| `SNAP_TARGET_1` | One Ad Squad UUID, or up to 20 separated by commas |
| `PRODUCT_CONTEXT` | Truthful product, market and offer facts |
| `OPENAI_API_KEY` | Your OpenAI API key |
| `STATE_ENCRYPTION_KEY` | A newly generated key for this new bot |

`SNAP_TARGET_1` is an Ad Squad selection: the equivalent of an ad-set selection.
Example structure: `first-squad-uuid,second-squad-uuid`. It is not a Campaign ID.
Optional `SNAP_TARGET_2` through `SNAP_TARGET_5` store other squad selections in
the same account. One workflow run selects one slot, which may contain many squads.

If you already have working Snapchat credentials, use them. Run `get_snap_ids.ps1`
only if you need the target IDs. `CROSS_ORGANIZATION_SETUP.md` explains how your own
OAuth app can authenticate a user with access to a partner account.

5. For a new bot, open PowerShell inside the extracted folder and run:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\generate_state_key.ps1
   ```

   The key is copied to your clipboard. Paste it directly into the
   `STATE_ENCRYPTION_KEY` Secret. Preserve this key once encrypted history exists.

6. Under **Actions → Variables**, add these initial values:

   | Variable | Value |
   | --- | --- |
   | `OPENAI_MODEL` | `gpt-5.4-nano` |
   | `RUN_MODE` | `test` |
   | `BOT_ENABLED` | `false` |

7. In **Settings → Actions → General → Workflow permissions**, enable **Read and
   write permissions** so the workflow can commit its encrypted history.
8. Open **Actions → Snapchat Second Ad Account Headline Monitor → Run workflow**:

   ```text
   target_slot: target_1
   run_mode: test
   max_updates: 80
   monitoring: one_check
   ```

   Confirm the intended squad count and `State encryption preflight passed.`
   If candidates exist, the log also reports the fresh-check protection and
   eligible `WOULD UPDATE` previews. Test mode does not send a Creative PATCH.

9. Run the same target in `live`, `max_updates=1`, `monitoring=one_check`.
   Verify the one changed Creative in Snapchat and that `state.json.enc` was
   saved. This exercises the conditional PATCH, which a preview cannot validate.
10. After that succeeds, use `live`, `max_updates=60` or `80`, and
    `monitoring=overnight`.

For scheduled continuation, set `RUN_MODE=live` and `BOT_ENABLED=true`. Scheduled
runs continue the squad IDs saved by the last manual live start. A pending run
uses its selected slot only when it starts. Avoid queueing multiple manual runs.
Setting `BOT_ENABLED=false` prevents scheduled work from starting; it does not
stop an already-running overnight job. Let that job finish before updating files
or switching targets.

## History and existing data

The workflow verifies encryption before Snapchat access. It uses AES-256-GCM to
save `state.json.enc` after each cycle, including cycles that report an error.
The Python worker also saves a local reservation before each PATCH. An ambiguous
PATCH result keeps that reservation and blocks automatic re-editing until the
worker can confirm what happened. Do not delete history to bypass this guard.

The ZIP has no account-specific state or credentials. A new bot creates its own
state on a live run. An existing bot must retain its current `state.json.enc` and
`STATE_ENCRYPTION_KEY`. Keep customized `headline_pool.txt` and
`blocked_headline_hashes.txt` when upgrading; the supplied versions are defaults.

## Files

| File | Purpose |
| --- | --- |
| `snap_headline_bot.py` | Complete worker, status guards, generation and history |
| `.github/workflows/snapchat-headline-editor.yml` | Manual runs, scheduling, encryption and persistence |
| `state_crypto.py` | Encrypt/decrypt state |
| `requirements.txt` | Python dependencies installed by GitHub Actions |
| `headline_pool.txt` | 100 starting Arabic headlines; use only lines accurate for your ads |
| `blocked_headline_hashes.txt` | Additional previously blocked headline hashes |
| `get_snap_ids.ps1` | Select displayed squads using an existing refresh token |
| `get_snap_token.ps1` | Optional OAuth authorization and token helper |
| `generate_state_key.ps1` | Generate and copy a new state encryption key |
| `START_HERE.txt` | Choose the existing-bot or new-bot instructions |
| `UPGRADE_EXISTING_BOT.txt` | Install into the existing repository without resetting history |
| `CROSS_ORGANIZATION_SETUP.md` | Authentication for a partner account |
| `VERSION.txt` | Release and validation notes |

The ID helpers list up to 1,000 Campaigns and up to 1,000 squads per Campaign;
they do not paginate those setup lists. If your intended squad is missing, obtain
its exact ID from Ads Manager. The Python worker does paginate its account reads
and independently verifies the selected target scope.

No headline guarantees approval. Product, video, landing-page or account-policy
issues may require corrections beyond a headline. The existing worker and guards
passed 14 offline integration tests. The full package was checked for source
completeness, syntax and encryption compatibility; live account authorization and
Windows helper execution must be verified in your environment.
