# Cross-organization authentication for Bot 2

## What Snapchat's email means

You do not need the COD Partner's client secret or refresh token. The solution has two
separate parts:

1. **Your OAuth app** proves which software is asking Snapchat for access.
2. **The Snapchat user authorization** decides which Organizations and Ad Accounts that
   software can access.

Create the OAuth app in a seller Organization you control. During authorization, use the
Snapchat login that already has access to the partner's target Ad Account. The resulting
token mirrors that user's existing permissions; it does not give extra permission.

You need Organization Admin access only in the seller Organization where you create your
OAuth app. You do not need Organization Admin access in the COD Partner's Organization.
The partner must only have invited your Snapchat user and assigned a role that permits the
required Creative changes.

## Before starting

Log in to Snapchat Ads Manager as the user you will authorize. Confirm all three points:

- The second Ad Account appears in the account selector.
- You can open its Campaigns and Ad Squads.
- You can manually edit a rejected ad's Creative/headline.

If you cannot do this manually, an OAuth token cannot bypass the missing role. Ask the
partner only to correct your user access; do not ask them for keys.

## Step 1 — Create your own OAuth app

1. Open Snap Business Manager and select a seller Organization where you are Organization
   Admin.
2. Open **Business Details**.
3. Under **OAuth Apps**, choose **+ OAuth App**.
4. Give it a clear name, for example `Samir Headline Bot 2`.
5. Enter this exact Redirect URI:

   ```text
   https://example.com/
   ```

6. Accept the displayed developer/business terms and create the app.
7. Copy the Client ID and Client Secret immediately. Snapchat displays the Client Secret
   only at creation. Store both privately.

Recommended: use a new OAuth app for Bot 2. Then it can be revoked without stopping Bot 1.

Official setup references:

- <https://developers.snap.com/marketing-api/Ads-API/authentication>
- <https://businesshelp.snapchat.com/s/article/api-apply?language=en_GB>

## Step 2 — Authenticate the correct Snapchat user

Create the new Bot 2 GitHub repository first and open its
**Settings → Secrets and variables → Actions** page. Add your Client ID and Client Secret
there so the repository is ready to receive the refresh token.

Open PowerShell inside the extracted package folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\get_snap_token.ps1
```

Use the default Redirect URI shown by the helper. Paste your own Client ID and Client
Secret only into that PowerShell window.

When the browser opens, carefully check which Snapchat user is signed in. It must be the
user that has access to the partner's second Ad Account. Approve access, then follow the
helper instructions. The helper exchanges the one-time code and copies the Refresh Token
directly to your Windows clipboard without displaying or saving it. Paste it immediately
into the GitHub Secret `SNAP_REFRESH_TOKEN`, save it, return to PowerShell, and press Enter.

The helper then lists every Organization and Ad Account visible to that authorized user.
The target second Ad Account must appear in that list. If it does not appear, stop and
check:

- Was the correct Snapchat user authorized?
- Was the Organization invitation accepted from email?
- Does the user have a role on that exact Ad Account?
- Did the partner grant Creative-edit access rather than reporting-only access?

Do not start the GitHub bot until the account appears.

## Step 3 — Collect the private target IDs

Continue inside `get_snap_token.ps1`. It loads all Campaigns and Ad Squads from the exact
target Ad Account. Select one to twenty Ad Squads by their displayed list numbers. The
helper produces these exact values:

```text
SNAP_AD_ACCOUNT_ID
SNAP_TARGET_1
```

Every Ad Squad stored in one target slot must belong to the configured Ad Account.

## Step 4 — Keep Bot 2 independent

Create a new GitHub repository from this package. Use new Bot 2 values for:

- Snapchat Client ID
- Snapchat Client Secret
- Snapchat Refresh Token
- Ad Account and Ad Squad target IDs
- State encryption key

The OpenAI API key may be the same if you want, but all Snapchat state and targeting must
remain separate. Never copy Bot 1's `state.json.enc` into Bot 2.

## MCP is not a replacement for this editing bot

Snapchat Ads MCP currently provides read-only access and its onboarding still requires
organization approval. It can help an agent inspect Ads Manager data, but it cannot run
this headline-update loop. Bot 2 therefore uses the Snapchat Marketing API OAuth flow.

Official MCP reference:

- <https://developers.snap.com/marketing-api/Ads-MCP/Introduction>
