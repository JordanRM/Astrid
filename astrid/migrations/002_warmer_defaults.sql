-- Fresh-state defaults: friendly and engaged, not guarded. Existing rows are untouched;
-- `python -m astrid reset --yes` applies the new baseline to a running install.
alter table astrid.state alter column trust set default 4;
alter table astrid.state alter column rapport set default 3;

-- A brand-new install still holds the row 001 created. Move it to the new baseline,
-- but only if nothing has touched it yet.
update astrid.state
   set trust = 4, rapport = 3
 where id = 1 and trust = 2 and rapport = 2 and mood = 'neutral'
   and unresolved_tension is null and directive is null
   and not exists (select 1 from astrid.state_history);
