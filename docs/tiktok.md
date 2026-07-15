# TikTok publishing

MoneyPrinterTurbo supports two TikTok providers: TikTok's official Content Posting API and Upload-Post.

## Start the application

Run `webui.bat`. It starts the local API on port `8080` when necessary and then opens Streamlit. The API must remain running for OAuth callbacks and scheduled jobs.

## Official TikTok API

1. Create an application or Sandbox in TikTok for Developers.
2. Add Login Kit **Desktop** and Content Posting API. Do not use Login Kit Web for the local callback.
3. Request the scopes `user.info.basic`, `video.upload`, and `video.publish`.
4. Configure this exact redirect URI in TikTok Developers:

   `http://127.0.0.1:8080/api/v1/tiktok/callback`

5. In Sandbox settings, add the TikTok account that will authorize the app as a target user and apply the changes.
6. Open the TikTok panel in the Web UI.
7. Select `TikTok API oficial`.
8. Enter the Client Key and Client Secret from the same Sandbox or Production revision.
9. Enable TikTok and save settings.
10. Click `Autorizar TikTok` and complete authorization in the browser. Always use the newest link.
11. Refresh creator options before choosing privacy.

Desktop Login Kit accepts an HTTP loopback callback when the host is `localhost` or `127.0.0.1` and a port is present. The redirect URI must match `config.toml` exactly. MoneyPrinterTurbo generates TikTok Desktop's hexadecimal SHA-256 PKCE challenge automatically.

TikTok can restrict unaudited applications to private visibility and target users. Public direct posting requires TikTok approval. Sandbox can be used without Production approval, but configuration changes can take time to become active.

MoneyPrinterTurbo uses `push_by_file` (`FILE_UPLOAD`), so domain verification for `pull_by_url` is not required.

## Upload-Post

1. Create an Upload-Post account and connect TikTok there.
2. Select `Upload-Post` in the TikTok panel.
3. Enter the Upload-Post API Key and username.
4. Enable TikTok and save settings.

## Local scheduling

TikTok does not expose YouTube's `publishAt` behavior through the Content Posting API. Scheduled jobs are stored in `storage/tiktok_schedule.json` and executed locally. Keep MoneyPrinterTurbo running at the scheduled time. Overdue jobs resume when the API starts again.

Upload records are stored separately from YouTube in `storage/tiktok_upload_log.json`. OAuth tokens are stored in `storage/tiktok_token.json`; do not commit or share these files.

Client credentials can be supplied through `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` instead of storing them in `config.toml`. If credentials have been shared or copied into logs, rotate the client secret in TikTok Developers.

The API binds to localhost by default. Remote TikTok management is disabled. If it is explicitly enabled, `TIKTOK_API_TOKEN` must be configured and sent in the `X-TikTok-API-Token` header.
