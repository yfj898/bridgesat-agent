# Memory ablation report

- probes: 10
- tenant_id: ablation_606370fb92154dfb9687f547f5a4dd58
- routes: no-memory / recent PG / similar PG / Mnemis System-1 / Mnemis dual-route

| Route | Episode recall@3 | Recall MRR | Next-action acc | Intervention acc | Fallback success | Latency avg (ms) | Latency p95 (ms) |
|---|---|---|---|---|---|---|---|
| no_memory | 0.00 | 0.00 | 0.00 | 0.40 | - | 0.0 | 0.0 |
| recent_postgres | 0.30 | 0.30 | 0.30 | 0.30 | - | 0.5 | 1.4 |
| similar_postgres | 1.00 | 1.00 | 1.00 | 1.00 | - | 0.4 | 1.4 |
| mnemis_system1 | 1.00 | 0.85 | 1.00 | 1.00 | - | 0.0 | 0.0 |
| mnemis_dual | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 801.5 | 802.7 |
