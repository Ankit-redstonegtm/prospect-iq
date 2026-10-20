// Public read API for the outreach dashboard + token-gated deal stage update.
// GET  -> dashboard JSON (aggregates, replies, deals; no lead email lists)
// POST {deal_id, stage, notes?, token} -> updates one deal via update_deal_stage() (token hash checked in DB)
import { createClient } from "jsr:@supabase/supabase-js@2";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type, x-admin-token, authorization, apikey",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};
const json = (b: unknown, s = 200) =>
  new Response(JSON.stringify(b), { status: s, headers: { ...cors, "Content-Type": "application/json", "Cache-Control": "no-store" } });

const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, { auth: { persistSession: false } });

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    if (req.method === "POST") {
      const b = await req.json();
      const { data, error } = await sb.rpc("update_deal_stage", {
        p_id: Number(b.deal_id), p_stage: String(b.stage), p_notes: b.notes ?? null, p_token: String(b.token ?? req.headers.get("x-admin-token") ?? ""),
      });
      if (error) return json({ error: error.message.includes("unauthorized") ? "unauthorized" : error.message }, error.message.includes("unauthorized") ? 401 : 400);
      return json({ ok: true, deal: data });
    }
    const url = new URL(req.url);
    if (url.searchParams.get("view") === "inbox") {
      const tok = req.headers.get("x-admin-token") ?? "";
      const { data: ok } = await sb.rpc("check_admin_token", { p_token: tok });
      if (!ok) return json({ error: "unauthorized" }, 401);
      const { data, error } = await sb.from("replies")
        .select("id,channel,campaign_id,person_name,title,email,linkedin_url,company_name,company_domain,subject,message_text,thread,received_at,sentiment,sentiment_source")
        .order("received_at", { ascending: false }).limit(2000);
      if (error) return json({ error: error.message }, 500);
      return json({ replies: data, generated_at: new Date().toISOString() });
    }
    const [camps, reps, deals, daily, runs] = await Promise.all([
      sb.from("outreach_campaigns").select("id,channel,name,status,leads_total,contacted,sent,replies,positives,bounces,created_at,synced_at").order("created_at", { ascending: false }),
      sb.from("replies").select("channel,sentiment,received_at"),
      sb.from("deals").select("id,reply_id,channel,person_name,company_name,company_domain,stage,notes,created_at,updated_at").order("created_at", { ascending: false }),
      sb.from("outreach_daily_metrics").select("*").order("day"),
      sb.from("outreach_sync_runs").select("source,synced_at").order("synced_at", { ascending: false }).limit(20),
    ]);
    const last: Record<string, string> = {};
    (runs.data ?? []).forEach((r: any) => { if (!last[r.source]) last[r.source] = r.synced_at; });
    // Public payload: per-day aggregates (IST dates) + sentiment mix; no message bodies or email addresses.
    return json({ daily: daily.data ?? [], campaigns: camps.data ?? [], reply_mix: reps.data ?? [], deals: deals.data ?? [],
      last_synced: last, generated_at: new Date().toISOString() });
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});
