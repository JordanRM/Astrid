alter table astrid.state
    add column if not exists security double precision not null default 6 check (security between 0 and 10);

-- Views she has formed, with her reason and where each came from. history records every change of mind.
create table if not exists astrid.opinions (
    id bigserial primary key,
    topic text not null unique,
    view text not null,
    reason text,
    source text not null default 'conversation',
    confidence double precision not null default 0.5 check (confidence between 0 and 1),
    history jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
