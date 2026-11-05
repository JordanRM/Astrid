-- A loose end she has already asked him about (she only follows up once).
alter table astrid.memories add column if not exists followed_up boolean not null default false;
