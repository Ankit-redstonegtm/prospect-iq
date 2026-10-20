-- Per-event dates for range filtering + LinkedIn funnel + full reply threads
alter table outreach_leads add column if not exists messaged boolean default false,
  add column if not exists first_messaged_at timestamptz,
  add column if not exists connection_requested_at timestamptz,
  add column if not exists connection_accepted boolean default false,
  add column if not exists bounced_at timestamptz;
alter table replies add column if not exists title text, add column if not exists thread jsonb;
alter table outreach_daily add column if not exists sends int default 0,
  add column if not exists conn_requests int default 0, add column if not exists conn_accepted int default 0,
  add column if not exists messages_sent int default 0, add column if not exists bounces int default 0;

-- One row per day: first-contact counts (unique people), sends, LinkedIn funnel, replies & positives by reply date
create or replace view outreach_daily_metrics as
with em as (
  select (min(contacted_at) at time zone 'Asia/Kolkata')::date d from outreach_leads
   where channel='email' and contacted group by lower(email)),
li as (
  select (min(first_messaged_at) at time zone 'Asia/Kolkata')::date d from outreach_leads
   where channel='linkedin' and messaged and first_messaged_at is not null group by linkedin_url),
rp as (
  select channel, (received_at at time zone 'Asia/Kolkata')::date d, sentiment from replies),
days as (
  select d from em union select d from li union select d from rp union select day from outreach_daily)
select days.d as day,
  (select count(*) from em where em.d=days.d)::int emailed,
  coalesce((select sum(sends) from outreach_daily o where o.day=days.d and channel='email'),0)::int email_sends,
  coalesce((select sum(bounces) from outreach_daily o where o.day=days.d and channel='email'),0)::int bounces,
  (select count(*) from rp where rp.d=days.d and channel='email')::int email_replies,
  (select count(*) from rp where rp.d=days.d and channel='email' and sentiment='positive')::int email_positives,
  coalesce((select sum(conn_requests) from outreach_daily o where o.day=days.d and channel='linkedin'),0)::int li_requests,
  coalesce((select sum(conn_accepted) from outreach_daily o where o.day=days.d and channel='linkedin'),0)::int li_accepted,
  (select count(*) from li where li.d=days.d)::int li_messaged,
  (select count(*) from rp where rp.d=days.d and channel='linkedin')::int li_replies,
  (select count(*) from rp where rp.d=days.d and channel='linkedin' and sentiment='positive')::int li_positives
from days where days.d is not null order by 1;
revoke all on outreach_daily_metrics from anon, authenticated;

create or replace function check_admin_token(p_token text) returns boolean language sql security definer set search_path=public,extensions as $$
  select exists (select 1 from dashboard_admin_tokens where token_sha256 = encode(digest(coalesce(p_token,''),'sha256'),'hex'));
$$;
revoke all on function check_admin_token(text) from public, anon, authenticated;

create or replace function link_reply_titles() returns void language sql security definer set search_path=public as $$
  update replies r set title = c.title from contacts c where r.title is null and c.id = r.contact_id;
  update replies r set email = c.email from contacts c where r.email is null and c.id = r.contact_id and c.email is not null;
$$;
revoke all on function link_reply_titles() from public, anon, authenticated;
