-- Long-term memory: things that happened, how they felt, how strongly they stick, and what they link to.
create table if not exists astrid.memories (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  kind text not null default 'fact',            -- fact | moment | loose_end | episode | gist
  text text not null,
  gist text,                                    -- the hazy version she'd have once the details fade
  importance real not null default 3,           -- 0..10
  emotion text,
  valence real not null default 0,              -- -1 bad .. +1 good
  explicit boolean not null default false,
  strength real not null default 1,
  permanent boolean not null default false,     -- strong moments never fade
  recalled_count integer not null default 0,
  last_recalled_at timestamptz,
  embedding real[],
  embed_model text,
  links bigint[] not null default '{}',
  status text not null default 'active',        -- active | merged | faded
  resolved boolean not null default false       -- for loose ends
);
create index if not exists memories_status_idx on astrid.memories (status);

-- A small, rewritable summary of Jordan. Old wordings are kept in history.
create table if not exists astrid.profile_facts (
  key text primary key,
  text text not null,
  importance real not null default 5,
  updated_at timestamptz not null default now(),
  history jsonb not null default '[]'::jsonb,
  removed boolean not null default false
);
