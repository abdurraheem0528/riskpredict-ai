"""Analysis of the stored public feeds (step 2).

* :mod:`jobs.analyse.tagging`   – GLiNER2 entities + event type for events with text
* :mod:`jobs.analyse.places`    – exact country-name → ISO 3166-1 alpha-3 matching
* :mod:`jobs.analyse.anomalies` – robust z-score and IsolationForest spike detection
* :mod:`jobs.analyse.store`     – reads/writes for migration 003's tables
* :mod:`jobs.analyse.runner`    – the steps behind ``python -m jobs.run --analyse``

Results go to their own tables; source fields in ``feeds.events`` and
``feeds.series`` are never changed.
"""
