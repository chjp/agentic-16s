-- "--other" can also sit inside a SILVA name ("Rubrivivax--other+Ideonella"), not only at the end.
create or replace function analysis.clean_taxon(name text) returns text
language sql immutable as $$
  select nullif(regexp_replace(btrim(coalesce(name, '')), '--other', '', 'g'), '')
$$;

refresh materialized view analysis.genus_counts;
