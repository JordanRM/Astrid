create schema if not exists astrid;

create table if not exists astrid.settings (
    key   text primary key,
    value text not null
);

create table if not exists astrid.messages (
    id         bigserial primary key,
    chat_id    bigint not null,
    role       text not null check (role in ('user', 'assistant')),
    content    text not null,
    channel    text not null default 'telegram',
    created_at timestamptz not null default now()
);
create index if not exists messages_chat_time_idx on astrid.messages (chat_id, id desc);

-- Her current inner state. One row. All numbers run 0 to 10.
create table if not exists astrid.state (
    id                 int primary key default 1 check (id = 1),
    trust              double precision not null default 2 check (trust between 0 and 10),
    rapport            double precision not null default 2 check (rapport between 0 and 10),
    irritation         double precision not null default 1 check (irritation between 0 and 10),
    energy             double precision not null default 6 check (energy between 0 and 10),
    vulnerability      double precision not null default 3 check (vulnerability between 0 and 10),
    arousal            double precision not null default 1 check (arousal between 0 and 10),
    mood               text not null default 'neutral',
    unresolved_tension text,
    directive          text,
    updated_at         timestamptz not null default now()
);
insert into astrid.state (id) values (1) on conflict (id) do nothing;

create table if not exists astrid.state_history (
    id     bigserial primary key,
    at     timestamptz not null default now(),
    cause  text not null,
    deltas jsonb not null default '{}'::jsonb,
    state  jsonb not null,
    mood   text,
    note   text
);
create index if not exists state_history_at_idx on astrid.state_history (at desc);
