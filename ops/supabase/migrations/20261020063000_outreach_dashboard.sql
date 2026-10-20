-- Outreach dashboard tables (Smartlead email + HeyReach LinkedIn)
create extension if not exists pgcrypto;

create table if not exists outreach_campaigns (
  id text primary key,                 -- 'smartlead:4144702' / 'heyreach:656658'
  channel text not null check (channel in ('email','linkedin')),
  external_id text not null,
  name text, status text,
  leads_total int default 0, contacted int default 0, sent int default 0,
  replies int default 0, positives int default 0, bounces int default 0,
  stats jsonb default '{}'::jsonb,
  created_at timestamptz, synced_at timestamptz default now()
);

create table if not exists outreach_leads (
  id text primary key,                 -- channel:campaign:email|linkedin
  channel text not null check (channel in ('email','linkedin')),
  campaign_id text references outreach_campaigns(id) on delete cascade,
  person_name text, title text, email text, linkedin_url text,
  company_name text, company_domain text,
  company_id uuid references companies(id) on delete set null,
  contact_id bigint references contacts(id) on delete set null,
  status text, contacted boolean default false, contacted_at timestamptz,
  bounced boolean default false, replied boolean default false,
  raw jsonb, updated_at timestamptz default now()
);
create index if not exists outreach_leads_email_idx on outreach_leads (lower(email));
create index if not exists outreach_leads_domain_idx on outreach_leads (company_domain);

create table if not exists replies (
  id text primary key,                 -- stable external id
  channel text not null check (channel in ('email','linkedin')),
  campaign_id text references outreach_campaigns(id) on delete set null,
  lead_id text references outreach_leads(id) on delete set null,
  person_name text, email text, linkedin_url text, company_name text, company_domain text,
  company_id uuid references companies(id) on delete set null,
  contact_id bigint references contacts(id) on delete set null,
  subject text, message_text text, received_at timestamptz,
  sentiment text check (sentiment in ('positive','neutral','negative','ooo')),
  sentiment_source text, sentiment_manual boolean default false,
  raw jsonb, updated_at timestamptz default now()
);
create index if not exists replies_received_idx on replies (received_at desc);

create table if not exists deals (
  id bigserial primary key,
  reply_id text unique references replies(id) on delete set null,
  channel text, person_name text, email text, linkedin_url text,
  company_name text, company_domain text,
  company_id uuid references companies(id) on delete set null,
  contact_id bigint references contacts(id) on delete set null,
  stage text not null default 'New' check (stage in ('New','Call booked','Proposal','Won','Lost')),
  stage_manual boolean default false, notes text,
  created_at timestamptz default now(), updated_at timestamptz default now()
);

create table if not exists outreach_daily (
  day date not null, channel text not null, touches int default 0, replies int default 0,
  primary key (day, channel)
);

create table if not exists outreach_sync_runs (
  id bigserial primary key, source text not null, synced_at timestamptz default now(), details jsonb
);

create table if not exists dashboard_admin_tokens (
  id serial primary key, token_sha256 text not null unique, label text, created_at timestamptz default now()
);

-- Lock down: RLS on, no anon/auth policies. Frontend reads via the outreach-dashboard edge function.
alter table outreach_campaigns enable row level security;
alter table outreach_leads enable row level security;
alter table replies enable row level security;
alter table deals enable row level security;
alter table outreach_daily enable row level security;
alter table outreach_sync_runs enable row level security;
alter table dashboard_admin_tokens enable row level security;

-- Link leads/replies/deals to companies & contacts by email / domain / LinkedIn URL
create or replace function link_outreach() returns void language sql security definer set search_path=public,extensions as $$
  update outreach_leads l set contact_id = c.id, company_id = coalesce(l.company_id, c.company_id)
    from contacts c where l.contact_id is null and l.email is not null and lower(c.email)=lower(l.email);
  update outreach_leads l set contact_id = c.id, company_id = coalesce(l.company_id, c.company_id)
    from contacts c where l.contact_id is null and l.linkedin_url is not null and c.linkedin_url is not null
     and rtrim(lower(regexp_replace(c.linkedin_url,'^https?://(www\.)?','')),'/') = rtrim(lower(regexp_replace(l.linkedin_url,'^https?://(www\.)?','')),'/');
  update outreach_leads l set company_id = co.id
    from companies co where l.company_id is null and l.company_domain is not null and lower(co.domain)=lower(l.company_domain);
  update outreach_leads l set company_domain = co.domain from companies co where l.company_domain is null and co.id=l.company_id;
  update replies r set contact_id = coalesce(r.contact_id,l.contact_id), company_id = coalesce(r.company_id,l.company_id),
     company_domain = coalesce(r.company_domain,l.company_domain)
    from outreach_leads l where l.id = r.lead_id;
  update replies r set company_id = co.id from companies co where r.company_id is null and r.company_domain is not null and lower(co.domain)=lower(r.company_domain);
  update deals d set contact_id = coalesce(d.contact_id,r.contact_id), company_id = coalesce(d.company_id,r.company_id),
     company_domain = coalesce(d.company_domain,r.company_domain) from replies r where r.id=d.reply_id;
$$;

-- Create a deal for every positive reply; never touches existing deals (manual stages preserved)
create or replace function ensure_deals() returns int language plpgsql security definer set search_path=public,extensions as $$
declare n int;
begin
  insert into deals (reply_id, channel, person_name, email, linkedin_url, company_name, company_domain, company_id, contact_id, created_at)
  select r.id, r.channel, r.person_name, r.email, r.linkedin_url, r.company_name, r.company_domain, r.company_id, r.contact_id, coalesce(r.received_at, now())
  from replies r where r.sentiment='positive'
    -- one deal per person: skip if this lead already has a deal from another reply
    and not exists (select 1 from deals d join replies r2 on r2.id=d.reply_id where r2.lead_id is not distinct from r.lead_id and r.lead_id is not null)
  on conflict (reply_id) do nothing;
  get diagnostics n = row_count; return n;
end $$;

-- Narrow, token-gated deal update used by the edge function
create or replace function update_deal_stage(p_id bigint, p_stage text, p_notes text, p_token text)
returns deals language plpgsql security definer set search_path=public,extensions as $$
declare d deals;
begin
  if not exists (select 1 from dashboard_admin_tokens where token_sha256 = encode(digest(coalesce(p_token,''),'sha256'),'hex')) then
    raise exception 'unauthorized';
  end if;
  if p_stage not in ('New','Call booked','Proposal','Won','Lost') then raise exception 'bad stage'; end if;
  update deals set stage=p_stage, stage_manual=true, notes=coalesce(left(p_notes,2000),notes), updated_at=now()
   where id=p_id returning * into d;
  return d;
end $$;
revoke all on function update_deal_stage(bigint,text,text,text) from public, anon, authenticated;
revoke all on function link_outreach() from public, anon, authenticated;
revoke all on function ensure_deals() from public, anon, authenticated;
