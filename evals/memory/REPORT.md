# Memory ablation report

- probes: 10
- tenant_id: ablation_2807cc5d694648e48b368771946bba66
- routes: no-memory / recent PG / similar PG / Mnemis System-1 / Mnemis dual-route

| Route | Episode recall@3 | Recall MRR | Next-action acc | Intervention acc | Fallback success | Latency avg (ms) | Latency p95 (ms) |
|---|---|---|---|---|---|---|---|
| no_memory | 0.00 | 0.00 | 0.00 | 0.40 | - | 0.0 | 0.0 |
| recent_postgres | 0.30 | 0.30 | 0.30 | 0.30 | - | 0.5 | 1.0 |
| similar_postgres | 1.00 | 1.00 | 1.00 | 1.00 | - | 0.3 | 0.7 |
| mnemis_system1 | 1.00 | 0.85 | 1.00 | 1.00 | - | 0.0 | 0.0 |
| mnemis_dual | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 801.6 | 803.2 |
