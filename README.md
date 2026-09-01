# Snapchat Second Ad Account Headline Monitor — Public-Safe Setup

This is a separate copy of the verified multi-Ad-Squad headline bot for a **second
Snapchat Ad Account**. It is designed for a new GitHub repository, so it cannot mix the
first bot's targets, credentials, or retry history with the second account.

The OAuth app can be created in a seller organization you control. The authenticated
Snapchat user may then access an Ad Account in a different partner organization, provided
that user already has the required role there. The partner does not share its client
secret or refresh token. Snapchat API access mirrors only what the authenticated user can
already do in Ads Manager. Read `CROSS_ORGANIZATION_SETUP.md` before creating the GitHub
repository.

## Safety design

- Snapchat credentials and the OpenAI API key are GitHub **Secrets**.
- The Ad Account UUID, Ad Squad UUIDs, and product context are also Secrets.
- The Run Workflow form shows only `target_1` through `target_5`; it never displays an
  Ad Squad UUID.
- Logs hide UUIDs, internal Campaign/Ad Squad names, exact old/new headlines, API request
  IDs, and API response bodies.
- Persistent history is authenticated and encrypted as `state.json.enc` with AES-256-GCM. Plaintext
  `state.json` exists only on the temporary GitHub runner and is ignored by Git.
- The workflow is not triggered by pull requests, so pull requests from strangers do not
  receive repository Secrets.

Use a **new** public repository. Do not change an old private bot repository to public:
its history and old Actions logs may already contain plaintext `state.json`, UUIDs,
internal names, or headlines.

## Bot behavior retained

- Selects 1–20 Ad Squads within one configured Snapchat Ad Account.
- Touches only rejected Ads whose Creative is disapproved and belongs entirely to the
  selected target.
- Never edits an approved headline and waits while an Ad or Creative is under review.
- If Snapchat rejects an edited headline again, it generates another globally fresh
  headline after review completion is confirmed.
- It has no per-Creative retry limit. `max_updates` limits only the number changed in one
  status check.
- A live overnight job checks every 60 seconds for up to 330 minutes.
- The scheduled fallback checks every five minutes when `BOT_ENABLED=true`.

No headline can guarantee approval. A rejection caused by the product, video, landing
page, advertiser documentation, or another policy issue cannot be repaired by changing
only the headline.

## 1. Upload this package

1. Extract the ZIP on your computer.
2. Create a **new public** GitHub repository without a README, `.gitignore`, or license.
3. On the empty repository page, select **uploading an existing file**.
4. Upload everything **inside** the extracted `snapchat-second-ad-account-bot` folder.
5. Commit the upload to `main`.
6. Confirm GitHub shows this exact path:

   `.github/workflows/snapchat-headline-editor.yml`

Never upload an old `state.json`, `.env`, token file, or downloaded Actions log.

## 2. Add repository Secrets

Open **Settings → Secrets and variables → Actions → Secrets → New repository secret**.
Add these exact names:

| Secret | What to enter |
| --- | --- |
| `SNAP_CLIENT_ID` | Snapchat OAuth Client ID |
| `SNAP_CLIENT_SECRET` | Matching Snapchat OAuth Client Secret |
| `SNAP_REFRESH_TOKEN` | Matching Snapchat refresh token |
| `OPENAI_API_KEY` | OpenAI API key |
| `SNAP_AD_ACCOUNT_ID` | Exact Snapchat Ad Account UUID |
| `PRODUCT_CONTEXT` | Truthful product/market facts; no internal Campaign or Ad Squad names |
| `STATE_ENCRYPTION_KEY` | Random key produced in the next section |
| `SNAP_TARGET_1` | One Ad Squad UUID, or up to 20 comma-separated UUIDs |
| `SNAP_TARGET_2` | Optional second private target/list |
| `SNAP_TARGET_3` | Optional third private target/list |
| `SNAP_TARGET_4` | Optional fourth private target/list |
| `SNAP_TARGET_5` | Optional fifth private target/list |

All three Snapchat credentials must belong to the same OAuth app. Create that app in an
organization you control; it does not need to belong to the partner that owns the target
Ad Account. During OAuth authorization, sign in as the Snapchat user that can edit the
target account. If that account is missing from the helper's visible-account list, stop
and correct the invitation or role before running the bot.

Optional: create `SNAP_AD_SQUAD_IDS` as a Secret only if you want a fallback
comma-separated target before any manual live run has saved an active target.

One target slot can hold one squad or a comma-separated group. Example structure only:

```text
SNAP_TARGET_1 = first-squad-uuid
SNAP_TARGET_2 = second-squad-uuid
SNAP_TARGET_3 = third-squad-uuid,fourth-squad-uuid
```

Do not paste real values into a public issue, workflow input, source file, screenshot, or
chat.

## 3. Generate the encryption key

On your Windows computer, open PowerShell inside the extracted folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\generate_state_key.ps1
```

The helper copies the generated value to your Windows clipboard without displaying it.
Paste it directly into the GitHub Secret `STATE_ENCRYPTION_KEY`. Keep that key unchanged.
If it is deleted or replaced, the bot cannot decrypt its saved history.

## 4. Add the three non-secret Variables

Open **Settings → Secrets and variables → Actions → Variables** and add:

| Variable | Initial value |
| --- | --- |
| `OPENAI_MODEL` | `gpt-5.4-nano` |
| `RUN_MODE` | `test` |
| `BOT_ENABLED` | `false` |

Then open **Settings → Actions → General → Workflow permissions**, select **Read and
write permissions**, and save. Write permission is needed only to commit the encrypted
`state.json.enc` file.

## 5. Test one private target

Open **Actions → Snapchat Second Ad Account Headline Monitor → Run workflow** and select:

```text
target_slot: target_1
run_mode: test
max_updates: 30
monitoring: one_check
```

The run must finish successfully and report counts. Public-safe logs intentionally do not
show the Account UUID, Ad Squad UUID, internal names, or headline text.

## 6. Make one live edit

Run the same `target_1` again with:

```text
run_mode: live
max_updates: 1
monitoring: one_check
```

Check Snapchat directly to confirm the correct rejected Creative changed and entered
review. GitHub should also create `state.json.enc`; that file is encrypted.

## 7. Start the monitor

After the one-edit check succeeds, run:

```text
run_mode: live
max_updates: 30
monitoring: overnight
```

Do not start another overnight run while one is active. To switch targets, cancel the
running job and manually start the new target slot. A manual live start stores that slot's
complete target list in encrypted state; scheduled runs continue that list.

After testing, set Variables `RUN_MODE=live` and `BOT_ENABLED=true` to enable the
five-minute scheduled fallback. Set `BOT_ENABLED=false` whenever you want scheduled work
to stop.

## Public-repository rules

- Protect the `main` branch and do not give unknown people write access.
- Review code changes before merging them; a malicious workflow change could read
  Secrets during a later scheduled or manual run.
- Never add `pull_request_target` to this workflow.
- Rotate any credential that has ever been pasted into chat, committed, or shown in a
  screenshot.
- A real GitHub account billing/payment hold may still need to be resolved even when the
  repository is public.
