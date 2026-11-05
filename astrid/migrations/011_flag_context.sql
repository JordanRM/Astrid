-- /meh keeps the conversation around the reply and what she was shown, so it can be debugged without screenshots.
alter table astrid.flags add column if not exists context jsonb;
alter table astrid.flags add column if not exists trace jsonb;
