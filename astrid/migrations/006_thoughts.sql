-- What she reads and thinks about on her own time. Private unless it fits the conversation or he asks.
create table if not exists astrid.thoughts (
    id bigserial primary key,
    created_at timestamptz not null default now(),
    kind text not null,
    question text not null,
    query text,
    sources jsonb not null default '[]'::jsonb,
    thought text not null,
    learned text,
    for_jordan text,
    next_question text,
    share double precision not null default 0.3,
    explicit boolean not null default false,
    surfaced integer not null default 0,
    followed boolean not null default false
);
create index if not exists thoughts_created_idx on astrid.thoughts (created_at desc);
