"""Per-transaction features relative to the card's own prior history.

Computed once over all 590k transactions with window functions and stored in
data/features.parquet (the shared DuckDB file stays read-only). Detectors and
weights both read these, so the closed-case calibration and the live agent see
exactly the same signal definitions.
"""
import duckdb

from kavach.config import DATA, DUCK_PATH

FEATURES = DATA / "features.parquet"

SQL = """
with base as (
  select TransactionID, card_id, customer_id, ts, TransactionAmt amt, ProductCD, channel, risk_score,
         cast(addr1 as integer) addr1, P_emaildomain, R_emaildomain,
         case when replace(replace(coalesce(device_key,''),'|',''),' ','') = '' then null else device_key end device_key,
         id_15, id_23, M4, M5, M6, dist1, D1, card_fields_null,
         epoch(ts) es
  from src.txn
),
w as (
  select *,
    row_number() over (partition by card_id order by ts, TransactionID) - 1 prior_n,
    row_number() over (partition by card_id, ProductCD order by ts, TransactionID) - 1 prior_same_product,
    case when addr1 is null then null else row_number() over (partition by card_id, addr1 order by ts, TransactionID) - 1 end prior_same_region,
    case when device_key is null then null else row_number() over (partition by card_id, device_key order by ts, TransactionID) - 1 end prior_same_device,
    case when P_emaildomain is null then null else row_number() over (partition by card_id, P_emaildomain order by ts, TransactionID) - 1 end prior_same_pemail,
    count(*) over (partition by card_id order by es range between 172800 preceding and current row) - 1 n_prev_48h,
    count(*) over (partition by card_id order by es range between 3600 preceding and current row) - 1 n_prev_1h,
    sum(case when amt < 5 and channel = 'online' then 1 else 0 end) over (partition by card_id order by es range between 3600 preceding and 1 preceding) n_small_prev_1h,
    avg(ln(amt + 1)) over (partition by card_id order by ts, TransactionID rows between unbounded preceding and 1 preceding) prior_mean_logamt,
    stddev_samp(ln(amt + 1)) over (partition by card_id order by ts, TransactionID rows between unbounded preceding and 1 preceding) prior_sd_logamt,
    max(amt) over (partition by card_id order by ts, TransactionID rows between unbounded preceding and 1 preceding) prior_max_amt,
    lag(es) over (partition by card_id order by ts, TransactionID) prev_es,
    count(distinct card_id) over (partition by device_key) device_cards_total
  from base
)
select *,
  (es - prev_es) / 3600.0 hours_since_prev,
  case when prior_sd_logamt > 0 then (ln(amt + 1) - prior_mean_logamt) / prior_sd_logamt end amt_z
from w
"""


def build() -> None:
    con = duckdb.connect()
    con.execute(f"attach '{str(DUCK_PATH).replace(chr(92), '/')}' as src (read_only)")
    con.execute(f"copy ({SQL}) to '{str(FEATURES).replace(chr(92), '/')}' (format parquet)")
    print(con.execute(f"select count(*) from '{str(FEATURES).replace(chr(92), '/')}'").fetchone())
