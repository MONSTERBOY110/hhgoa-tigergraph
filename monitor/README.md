# Monitor findings

Kavach's autonomous monitor sweeps November and December for activity that no alert pointed at, using three general sweeps (sub-threshold bursts, scripted device rings, rare-device rings), and investigates each candidate with the same agent and policy engine as the exam cases. The files use the answer schema plus a `monitor` block. These are extra findings for the Innovation score and are not part of the 20 answers in `cases/`.

Run: `python -m kavach monitor`

| File | Sweep | Card | Verdict | p | Pattern | Exposure | SAR | Why it was picked |
|---|---|---|---|---|---|---|---|---|
| [MON-001](MON-001.json) | sub_threshold_burst | C10751-K1 | fraud | 0.92 | undocumented | $1,906.11 | yes | Monitor sweep: 4 online purchases just under $500 within 40 minutes on card C10751-K1. |
| [MON-002](MON-002.json) | sub_threshold_burst | C03633-K1 | fraud | 0.97 | undocumented | $1,935.29 | yes | Monitor sweep: 4 online purchases just under $500 within 40 minutes on card C03633-K1. |
| [MON-003](MON-003.json) | scripted_device_ring | C07472-K1 | fraud | 0.95 | undocumented | $265.85 | yes | Monitor sweep: device profile SM-G935F Build/NRD90M \| Android 7.0 \| chrome 62.0 for android \| 1920x1080 used on 28 cards in November and December, 100% New and 100% behind an anonymous proxy. |
| [MON-004](MON-004.json) | rare_device_ring | C09588-K1 | fraud | 0.96 | card_not_present_fraud | $58.38 | yes | Monitor sweep: rare device profile GT-I9060M Build/KTU84P \|  \| chrome 65.0 for android \|  paid about $58 on 8 cards within 7 days. |
| [MON-005](MON-005.json) | rare_device_ring | C02851-K2 | fraud | 0.95 | card_not_present_fraud | $8.73 | yes | Monitor sweep: rare device profile Moto G (4) Build/NPJS25.93-14-8.1-4 \|  \| chrome generic \|  paid about $9 on 7 cards within 7 days. |
