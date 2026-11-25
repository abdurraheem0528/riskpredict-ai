-- 030: full sanctions lists (OFAC SDN + consolidated non-SDN), as current state.
--
-- feeds.events keeps OFAC's daily *change set* (designations, updates, removals).
-- This table holds every entity currently listed, for name screening. It is
-- replaced, not accumulated: each complete read upserts every entry and deletes
-- the ones no longer listed, so its size tracks the list (~18k rows, a few MB).
-- Only what matching needs is kept: names, aliases, countries, programmes,
-- lists. No birth dates, ID numbers, addresses or remarks.

CREATE TABLE feeds.sanctions_entries (
    source        text        NOT NULL,                -- connector, e.g. 'ofac_sdn'
    dataset       text        NOT NULL,                -- 'sdn' | 'consolidated'
    entity_id     text        NOT NULL,                -- OFAC entity id (same as attrs.entity_id of 'ofac' events)
    entity_type   text,                                -- Individual, Entity, Vessel, Aircraft
    name          text        NOT NULL,                -- primary name, primary translation
    aliases       text[]      NOT NULL DEFAULT '{}',   -- other names and translations (all scripts)
    weak_aliases  text[]      NOT NULL DEFAULT '{}',   -- names OFAC flags as low quality ("weak a.k.a.")
    countries     text[]      NOT NULL DEFAULT '{}',   -- ISO 3166-1 alpha-3
    programs      text[]      NOT NULL DEFAULT '{}',   -- e.g. SDGT, RUSSIA-EO14024
    lists         text[]      NOT NULL DEFAULT '{}',   -- e.g. 'SDN List', 'SSI List'
    listed_on     date,                                -- earliest list publication date given
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, dataset, entity_id)
);

CREATE INDEX sanctions_entries_countries_idx ON feeds.sanctions_entries USING gin (countries);
CREATE INDEX sanctions_entries_entity_idx ON feeds.sanctions_entries (entity_id);

COMMENT ON TABLE feeds.sanctions_entries IS
    'Every entity on the full OFAC SDN and consolidated lists; replaced on each complete daily read.';
