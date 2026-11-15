# API latency

Database `var/backup/full-restored.db` (1.32 GB). Hardware: Intel64 Family 6 Model 142 Stepping 10, GenuineIntel, 8 logical CPUs, Windows-10-10.0.26300-SP0, Python 3.11.0. In-process ASGI client, warm cache, 30 sequential requests per endpoint.

| Endpoint | p50 ms | p95 ms | max ms |
|---|---|---|---|
| GET /dashboard/summary (all stores) | 326.8 | 408.9 | 432.3 |
| GET /dashboard/summary (one store) | 275.0 | 359.3 | 401.7 |
| GET /alerts (open queue, page 1) | 56.6 | 71.4 | 75.0 |
| GET /alerts/{id} | 24.7 | 50.2 | 53.3 |
| GET /reconciliations (store filter) | 37.8 | 57.4 | 63.7 |
| GET /reconciliations/{id} | 39.6 | 60.2 | 66.3 |
| GET /movements (store + sku) | 33.5 | 84.0 | 115.2 |
| GET /imports | 41.0 | 50.7 | 70.4 |
| GET /investigations (open) | 98.6 | 200.1 | 226.4 |
