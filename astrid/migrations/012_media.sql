-- Links she may share in explicit chat: real ones she found herself, from sites Jordan allowed.
create table if not exists astrid.media_links (
    id bigserial primary key,
    created_at timestamptz not null default now(),
    url text not null unique,
    title text,
    domain text,
    snippet text,
    owner text not null default 'both',      -- hers, his or both: whose taste it came from
    why text,
    query text,
    shared_at timestamptz,
    times_shared int not null default 0
);
