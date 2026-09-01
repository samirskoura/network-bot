# Start here — second Ad Account

The partner organization does **not** give you its client secret or refresh token. Create
your own OAuth app, then authenticate the Snapchat user that already has permission to
edit the second Ad Account.

1. Read `CROSS_ORGANIZATION_SETUP.md`.
2. In a seller organization you control, create a new Snapchat OAuth app with this exact
   Redirect URI:

   ```text
   https://example.com/
   ```

3. Copy its Client ID and Client Secret immediately. Never post them in chat.
4. Create a **new GitHub repository** for Bot 2. Do not reuse Bot 1's repository or state.
5. Upload everything inside `snapchat-second-ad-account-bot` and confirm this path exists:

   ```text
   .github/workflows/snapchat-headline-editor.yml
   ```

6. Add `SNAP_CLIENT_ID` and `SNAP_CLIENT_SECRET` under **Settings → Secrets and
   variables → Actions → Secrets**.
7. From the extracted folder, run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\get_snap_token.ps1
   ```

8. When Snapchat opens, sign in as the user that can edit the partner's second Ad
   Account. The target account must appear in the helper's account list.
9. The helper copies `SNAP_REFRESH_TOKEN` to your clipboard. Paste it immediately into
   that GitHub Secret, then return to PowerShell and press Enter.
10. Paste the exact COD Partner Ad Account ID, then choose one to twenty displayed Ad
    Squads by number. Save the two final results `SNAP_AD_ACCOUNT_ID` and
    `SNAP_TARGET_1` privately.
11. Add the remaining GitHub Secrets:
   - `OPENAI_API_KEY`
   - `SNAP_AD_ACCOUNT_ID`
   - `PRODUCT_CONTEXT`
   - at least `SNAP_TARGET_1`
12. Generate a new Bot 2 encryption key locally and save it as the GitHub Secret
    `STATE_ENCRYPTION_KEY`:

    ```powershell
    powershell -ExecutionPolicy Bypass -File .\generate_state_key.ps1
    ```

    The helper copies the key to your clipboard without displaying it. Paste it directly
    into the GitHub Secret.

13. Under **Variables**, add `OPENAI_MODEL=gpt-5.4-nano`, `RUN_MODE=test`, and
    `BOT_ENABLED=false`.
14. Enable **Settings → Actions → General → Read and write permissions**.
15. Run `target_1` in `test` + `one_check` mode.
16. Run `target_1` in `live` + `max_updates=1` + `one_check` mode.
17. After checking the exact Creative in Snapchat, run `live` + `max_updates=30` +
    `overnight`.

The form exposes only a target slot. Credentials, account identifiers, target identifiers,
product context, and the encryption key remain GitHub Secrets. Retry history is committed
only as encrypted `state.json.enc`.
