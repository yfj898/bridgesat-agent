# Memory ablation report

- probes: 10
- tenant_id: ablation_daac94a19c2943328df1ae3595d785a2
- routes: no-memory / recent PG / similar PG / Mnemis System-1 / Mnemis dual-route

| Route | Episode recall@3 | Recall MRR | Next-action acc | Intervention acc | Fallback success | Latency avg (ms) | Latency p95 (ms) |
|---|---|---|---|---|---|---|---|
| no_memory | 0.00 | 0.00 | 0.00 | 0.40 | - | 0.0 | 0.0 |
| recent_postgres | 0.30 | 0.30 | 0.30 | 0.30 | - | 1.5 | 3.0 |
| similar_postgres | 1.00 | 1.00 | 1.00 | 1.00 | - | 1.2 | 2.2 |
| mnemis_system1 | 1.00 | 0.85 | 1.00 | 1.00 | - | 0.0 | 0.1 |
| mnemis_dual | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 802.6 | 803.6 |
