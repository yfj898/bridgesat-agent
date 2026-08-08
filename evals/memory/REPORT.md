# Memory ablation report

- probes: 10
- routes: no-memory / recent SQLite / similar SQLite / Mnemis System-1 / Mnemis dual-route

| Route | Episode recall@3 | Recall MRR | Next-action acc | Intervention acc | Fallback success | Latency avg (ms) | Latency p95 (ms) |
|---|---|---|---|---|---|---|---|
| no_memory | 0.00 | 0.00 | 0.00 | 0.40 | - | 0.0 | 0.0 |
| recent_sqlite | 0.30 | 0.30 | 0.30 | 0.30 | - | 0.6 | 1.5 |
| similar_sqlite | 1.00 | 1.00 | 1.00 | 1.00 | - | 0.6 | 1.6 |
| mnemis_system1 | 1.00 | 0.85 | 1.00 | 1.00 | - | 0.0 | 0.0 |
| mnemis_dual | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 801.7 | 803.3 |
