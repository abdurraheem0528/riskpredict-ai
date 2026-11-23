-- 002: default watch terms — generic public topics and widely used packages.
-- Not business data. Disable or delete rows freely; this file only runs once.
-- Keep in sync with jobs/watch_defaults.py (checked by tests).

INSERT INTO feeds.watch_terms (kind, term, scope) VALUES
    ('gdelt_query', '("port strike" OR "dockworkers strike" OR "port closure")', ''),
    ('gdelt_query', '("export ban" OR "export restrictions" OR "tariff increase")', ''),
    ('gdelt_query', '("supply chain disruption" OR "factory fire" OR "plant shutdown")', ''),
    ('gdelt_query', '("new sanctions" OR "sanctions imposed")', ''),
    ('gdelt_query', '("ransomware attack" OR "cyberattack" OR "data breach")', ''),
    ('gdelt_query', '("shipping disruption" OR "vessel attacked" OR "canal closed")', ''),
    ('gdelt_query', '("currency crisis" OR "devaluation" OR "capital controls")', ''),
    ('gdelt_query', '("chip shortage" OR "semiconductor shortage" OR "raw material shortage")', ''),
    ('osv_package', 'requests', 'PyPI'),
    ('osv_package', 'django', 'PyPI'),
    ('osv_package', 'lodash', 'npm'),
    ('osv_package', 'axios', 'npm'),
    ('osv_package', 'org.apache.logging.log4j:log4j-core', 'Maven'),
    ('osv_package', 'golang.org/x/net', 'Go')
ON CONFLICT (kind, scope, term) DO NOTHING;
