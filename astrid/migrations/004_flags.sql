-- Replies Jordan flagged with /meh. Kept across resets: this is training and test material.
create table if not exists astrid.flags (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  chat_id bigint not null,
  user_text text,
  reply text not null,
  note text,
  mood text,
  state jsonb
);
