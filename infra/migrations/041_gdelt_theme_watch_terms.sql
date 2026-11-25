-- 041: default GDELT watch terms built from GKG themes, one per risk category.
--
-- The phrase terms of 002 match an article only by its title, URL, organisations,
-- locations or theme names, and found ~5 articles per 30-minute run. These match
-- the GKG theme codes GDELT assigns to each article (theme:CODE). Only the GDELT
-- file feed reads them: the DOC API fallback cannot send AND-ed terms, and
-- NewsData and Currents skip theme: terms.
-- Every code is in GDELT's LOOKUP-GKGTHEMES.TXT (checked 2026-11-25). A broad
-- theme is ANDed with a narrower one (space = AND, ( ... OR ... ) = any of), so all
-- ten together match ~15 articles per 15-minute file (measured on four real files).
-- Keep in sync with jobs/watch_defaults.py (checked by tests). Idempotent.

INSERT INTO feeds.watch_terms (kind, term, scope, origin) VALUES
    ('gdelt_query', 'theme:CYBER_ATTACK (theme:SOC_RANSOM OR theme:WB_2457_CYBER_CRIME)', '', 'default'),
    ('gdelt_query', 'theme:MARITIME_INCIDENT (theme:MARITIME_PIRACY OR theme:MANMADE_DISASTER_STRICKEN_VESSEL)', '', 'default'),
    ('gdelt_query', 'theme:WB_167_PORTS (theme:STRIKE OR theme:BLOCKADE OR theme:CLOSURE)', '', 'default'),
    ('gdelt_query', 'theme:STRIKE (theme:ECON_UNIONS OR theme:TAX_FNCACT_WORKERS OR theme:TAX_FNCACT_UNION_MEMBERS)', '', 'default'),
    ('gdelt_query', 'theme:SANCTIONS (theme:ECON_TRADE_DISPUTE OR theme:WB_2290_OIL_AND_GAS_EXPORT OR theme:WB_778_NON_TARIFF_MEASURES)', '', 'default'),
    ('gdelt_query', '(theme:NATURAL_DISASTER_EARTHQUAKE OR theme:NATURAL_DISASTER_FLOOD OR theme:NATURAL_DISASTER_FLOODING OR theme:NATURAL_DISASTER_CYCLONE OR theme:NATURAL_DISASTER_HURRICANE OR theme:NATURAL_DISASTER_TYPHOON) (theme:WB_167_PORTS OR theme:MANMADE_DISASTER_POWER_DISRUPTION)', '', 'default'),
    ('gdelt_query', 'theme:ARMEDCONFLICT (theme:MARITIME_INCIDENT OR theme:BLOCKADE) (theme:WB_167_PORTS OR theme:CRISISLEX_C04_LOGISTICS_TRANSPORT)', '', 'default'),
    ('gdelt_query', 'theme:SHORTAGE theme:CRISISLEX_C04_LOGISTICS_TRANSPORT (theme:WB_1353_PHARMACEUTICAL_SUPPLY_CHAIN OR theme:WB_194_AGRICULTURAL_SUPPLY_CHAIN OR theme:WB_167_PORTS)', '', 'default'),
    ('gdelt_query', 'theme:ECON_TRADE_DISPUTE (theme:SANCTIONS OR theme:WB_778_NON_TARIFF_MEASURES)', '', 'default'),
    ('gdelt_query', 'theme:EPU_CATS_SOVEREIGN_DEBT_CURRENCY_CRISES theme:ECON_CENTRALBANK (theme:ECON_CURRENCY_EXCHANGE_RATE OR theme:ECON_CURRENCY_RESERVES)', '', 'default')
ON CONFLICT (kind, scope, term) DO NOTHING;
