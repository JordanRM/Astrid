alter table astrid.state
    add column if not exists affection double precision not null default 1 check (affection between 0 and 10);
