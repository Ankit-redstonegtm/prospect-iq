# Outreach sync — how to refresh the dashboard

Live dashboard: https://ankit-redstonegtm.github.io/prospect-iq/  (repo `Ankit-redstonegtm/prospect-iq`, local clone `/workspace/site`)
Data API: Edge Function `outreach-dashboard` (Supabase project `vsxgszlevcygijgulnzm`). The page has no keys in it; the function
reads with the service role on the server. All outreach tables have RLS on with **no** anon policies.

Tables: `outreach_campaigns`, `outreach_leads`, `replies`, `deals`, `outreach_daily`, `outreach_sync_runs`, `dashboard_admin_tokens`.
Schema: `supabase/migrations/20261020063000_outreach_dashboard.sql`. Edge function source: `supabase/functions/outreach-dashboard/index.ts`.

## 1. Refresh HeyReach (LinkedIn) — agent via the `user-heyreach` MCP connector
HeyReach has no API key on the box, so an agent pulls it through MCP and dumps raw JSON into `data/heyreach/`
(always use `write_to_file: true`, then copy the JSON body — strip the wrapper text before the first `{` / after the last `}`):

| file | MCP call |
|---|---|
| `data/heyreach/campaigns.json` | `get_all_campaigns {}` |
| `data/heyreach/overall_stats.json` | `get_overall_stats {}` (all-time; gives `byDayStats` for the chart) |
| `data/heyreach/conversations.json` | `get_conversations_v2 {offset:0, limit:100}` — page with offset+=100 until `items` < 100, merge into one `{"totalCount":N,"items":[...]}` |
| `data/heyreach/leads.json` | for **every** campaign id in campaigns.json: `get_leads_from_campaign {campaignId, offset, limit:100}` paging until `totalCount` reached; save as `{"<campaignId>": [items...], ...}` |

Helper to convert a written tool file to clean JSON:
```python
import json; t=open(PATH).read(); d=json.loads(t[t.index('{'):t.rindex('}')+1])
```

## 2. Run the sync (Smartlead via API + load HeyReach JSON)
```bash
cd /workspace/gtm-system
python3 sync_outreach.py                  # both channels
python3 sync_outreach.py --skip-heyreach  # Smartlead only (no MCP needed)
python3 sync_outreach.py --skip-smartlead # just reload HeyReach JSON
```
Needs env `SUPABASE_SERVICE_KEY` (already set on the box). Smartlead key is read from `/workspace/.secrets/smartlead_api_key` (never print/commit).
Optional `GEMINI_API_KEY` → sentiment via `gemini-3.1-flash-lite`; on any Gemini error it falls back to rules (as of 2026-10-20 the key returns 401, so rules are used).

What it does: upserts campaigns/leads/replies/daily touches (idempotent, stable ids), drops mail-delivery failures from replies
(counted as bounces), keeps existing sentiment labels unless the text changed (and never overrides `sentiment_manual=true`),
then calls `link_outreach()` (links to `companies`/`contacts` by email, LinkedIn URL, domain) and `ensure_deals()`
(inserts a `New` deal for each new positive reply, one per lead; existing deals/stages/notes are never touched).

The dashboard updates immediately (no redeploy needed). "Last synced" = latest row in `outreach_sync_runs`.

## Metric definitions (what the dashboard shows)
- Range filter (7/14/28/30/50 days, all time) and channel toggle are applied client-side to the per-day view `outreach_daily_metrics` (IST dates) returned by the edge function.
- **Email**: leads emailed = unique leads by first send date (`outreach_leads.contacted_at`); emails sent and bounces are per-send counts from Smartlead `/statistics` (bounce is dated by its send). Email rates = replies or positives / leads emailed.
- **LinkedIn**: connection requests and accepts = HeyReach `byDayStats.connectionsSent` / `connectionsAccepted`. People messaged = unique profiles that got at least one DM from us (`first_messaged_at` = our first message in `get_conversations_v2`, or lead `leadMessageStatus` MessageSent/MessageReply). LinkedIn rates = replies or positives / people messaged.
- **Combined** base = emailed + LinkedIn messaged. Replies and positives are dated by when they arrived.

## Inbox page (`inbox.html`)
Full replies (name, title, company, email, LinkedIn URL, full body, thread) come from `GET outreach-dashboard?view=inbox` with header
`x-admin-token: <token>`. Without a valid token it returns 401. The page asks for the token once and keeps it in localStorage.
The public `GET outreach-dashboard` returns only aggregates, campaigns, deals and the sentiment mix: no bodies and no email addresses.

## 3. Moving deals between stages
The board is read-only until you click **Unlock editing** and paste the admin token from `/workspace/.secrets/dashboard_admin_token`
(stored in your browser's localStorage only). The edge function calls `update_deal_stage()`, which checks the token's SHA-256 against
`dashboard_admin_tokens` and only allows changing `stage`/`notes` of one deal. Manual stage changes set `stage_manual=true`.
Rotate: insert a new hash into `dashboard_admin_tokens` and delete the old row.
To fix a sentiment by hand: `update replies set sentiment='positive', sentiment_manual=true where id='…'; select ensure_deals();`

## 4. Redeploy (only if code changed)
- Edge function: Supabase MCP `deploy_edge_function` name `outreach-dashboard`, `verify_jwt=false` (it does its own token check for writes).
- Frontend: `cd /workspace/site && git commit -am "…" && git push` (GitHub Pages serves `main`).
