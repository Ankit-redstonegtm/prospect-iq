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

async function count(channel: string, extra: (q: any) => any = (q) => q) {
  const { count } = await extra(sb.from("outreach_leads").select("id", { count: "exact", head: true }).eq("channel", channel));
  return count ?? 0;
}

async function distinctLinkedIn() {
  const urls = new Set<string>(); let from = 0;
  while (true) {
    const { data } = await sb.from("outreach_leads").select("linkedin_url").eq("channel", "linkedin").eq("contacted", true).range(from, from + 999);
    (data ?? []).forEach((r: any) => urls.add(r.linkedin_url)); if (!data || data.length < 1000) break; from += 1000;
  }
  return urls.size;
}

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
    const [camps, reps, deals, daily, runs] = await Promise.all([
      sb.from("outreach_campaigns").select("id,channel,name,status,leads_total,contacted,sent,replies,positives,bounces,created_at,synced_at").order("created_at", { ascending: false }),
      sb.from("replies").select("id,channel,campaign_id,person_name,company_name,company_domain,subject,message_text,received_at,sentiment,sentiment_source").order("received_at", { ascending: false }).limit(1000),
      sb.from("deals").select("id,reply_id,channel,person_name,company_name,company_domain,stage,notes,created_at,updated_at").order("created_at", { ascending: false }),
      sb.from("outreach_daily").select("day,channel,touches,replies").order("day"),
      sb.from("outreach_sync_runs").select("source,synced_at").order("synced_at", { ascending: false }).limit(20),
    ]);
    const emailContacted = await count("email", (q) => q.eq("contacted", true));
    const liContacted = await distinctLinkedIn();
    const R = reps.data ?? [];
    const by = (ch: string, s?: string) => R.filter((r: any) => r.channel === ch && (!s || r.sentiment === s)).length;
    const C = camps.data ?? [];
    const last: Record<string, string> = {};
    (runs.data ?? []).forEach((r: any) => { if (!last[r.source]) last[r.source] = r.synced_at; });
    const kpis = {
      email_contacted: emailContacted, linkedin_contacted: liContacted, total_contacted: emailContacted + liContacted,
      replies: R.length, replies_email: by("email"), replies_linkedin: by("linkedin"),
      positives: by("email", "positive") + by("linkedin", "positive"),
      positives_email: by("email", "positive"), positives_linkedin: by("linkedin", "positive"),
      positive_rate_email: emailContacted ? by("email", "positive") / emailContacted : 0,
      positive_rate_linkedin: liContacted ? by("linkedin", "positive") / liContacted : 0,
      bounces: C.filter((c: any) => c.channel === "email").reduce((a: number, c: any) => a + (c.bounces || 0), 0),
      emails_sent: C.filter((c: any) => c.channel === "email").reduce((a: number, c: any) => a + (c.sent || 0), 0),
    };
    return json({ kpis, campaigns: C, replies: R, deals: deals.data ?? [], daily: daily.data ?? [], last_synced: last, generated_at: new Date().toISOString() });
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});
