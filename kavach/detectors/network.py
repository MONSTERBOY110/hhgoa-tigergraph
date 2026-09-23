"""Cross-card detectors: what happened on OTHER cards sharing a device, an amount template, an email or a region."""
import math
from datetime import timedelta

import pandas as pd

from kavach.assess import Signal

RING_MIN_CARDS = 3
PEER_TOL = 0.01
PEER_DAYS = 7


def _ids(df) -> list[str]:
    return [str(int(x)) for x in df.TransactionID]


def detect(ctx, fnd) -> list[Signal]:
    sigs: list[Signal] = []
    f, T = ctx.f, ctx.tools
    tid = str(int(f["TransactionID"]))
    T.step = 4

    # ---- rare device shared by several cards ----
    if ctx.device and ctx.device_rows is not None and len(ctx.device_rows):
        rows = ctx.device_rows
        others = rows[rows.card_id != ctx.card_id]
        cards = sorted(others.card_id.unique())
        if len(cards) + 1 >= RING_MIN_CARDS:
            new_share = (rows.id_15 == "New").mean()
            anon_share = rows.id_23.fillna("").str.contains("ANONYMOUS").mean()
            close_amt = others[(others.amt - f["amt"]).abs() <= 0.02 * f["amt"] + 0.5]
            span_days = (rows.ts.max() - rows.ts.min()).days
            ref = ctx.refs["device"]
            if new_share >= 0.8 and anon_share >= 0.5 and len(cards) >= 5:
                sigs.append(Signal("scripted_device_ring", "network", 2.8,
                                   f"Device profile {ctx.device} (seen on only {ctx.device_pop} cards ever) made {len(rows)} purchases on "
                                   f"{len(cards) + 1} different customers' cards within {span_days} days, {new_share:.0%} marked New "
                                   f"and {anon_share:.0%} behind an anonymous proxy", cards[:10], ref))
                fnd.coordinated_undocumented = True
                fnd.undocumented_kind = fnd.undocumented_kind or "device_ring"
                fnd.vote("undocumented", 2.8)
                fnd.episode["device_ring"] = list(ctx.h[ctx.h.device_key == ctx.device].TransactionID)
            elif len(close_amt.card_id.unique()) >= 2 or (ctx.device_pop <= 10 and len(cards) >= 3):
                n_close = len(close_amt.card_id.unique())
                from kavach.weights import load
                ring = load()["device_ring"]
                lr = ring["LR_3_other_cards"] if n_close >= 3 else ring["LR_2_other_cards"]
                sigs.append(Signal("device_ring", "network", round(math.log(lr), 2),
                                   f"Rare device profile {ctx.device} ({ctx.device_pop} cards ever) was used on {len(cards)} other cards within "
                                   f"30 days, {n_close} of them for amounts within 2% of this ${f['amt']:,.2f} purchase (in the closed months "
                                   f"this shape was {lr:.1f} times more common in confirmed fraud than in normal online traffic)",
                                   cards[:10], ref))
            else:
                cards = []
            if cards:
                for c in cards:
                    fnd.connected_cards[c] = f"shares device profile {ctx.device}"
                fnd.connected_devices.append(ctx.device)
                fnd.shared_element = f"device profile {ctx.device}"

    # ---- fixed-amount ring: the same odd amount, product and email on many cards in a week ----
    if f["channel"] == "online" and not fnd.shared_element:
        for field in ("P_emaildomain", "R_emaildomain"):
            email = f.get(field)
            if not isinstance(email, str):
                continue
            peers = T.call("amount_peers", product_cd=f["ProductCD"], amt=float(f["amt"]), t0=ctx.t0, days=PEER_DAYS,
                           tol=PEER_TOL, email=email, email_field=field)
            pcards = sorted(set(peers.card_id) - {ctx.card_id})
            fp = _shared_fingerprint(peers, f)
            if len(pcards) >= 4 and fp:
                members = sorted(set(fp[1]) - {ctx.card_id})
                sigs.append(Signal("fixed_amount_ring", "network", 1.6,
                                   f"{len(pcards)} other cards made online {f['ProductCD']} purchases within 1% of ${f['amt']:,.2f} with "
                                   f"{field.split('_')[0]} email {email} within {PEER_DAYS} days, and {len(members)} of them share this "
                                   f"transaction's {fp[0]}, which points to one actor behind several card numbers", members[:10],
                                   T.ref("amount_peers", product=f["ProductCD"], amt=round(float(f["amt"]), 2), email=email, days=PEER_DAYS)))
                for c in members:
                    fnd.connected_cards.setdefault(c, f"same ${f['amt']:,.0f} {f['ProductCD']} template and {fp[0]}")
                fnd.shared_element = f"a fixed-amount purchase template (${f['amt']:,.2f}, product {f['ProductCD']}, email {email}, {fp[0]})"
                break

    # ---- a device this card used before only counts as exculpatory when it is not shared by a ring ----
    if ctx.device and "device_known" in fnd.notes and not fnd.connected_devices:
        sigs.append(Signal("device_known", "device", -0.8, f"The device profile was already used by this card "
                           f"{fnd.notes['device_known']} time(s) before", [tid], ctx.refs["history"]))

    # ---- the same sub-threshold template on other cards (repetition across customers, R9) ----
    if fnd.undocumented_kind == "sub_threshold":
        thr = fnd.notes["threshold"]
        peers = T.call("band_bursts", amt_lo=0.88 * thr, amt_hi=thr, t0=ctx.t0, days=21)
        other = sorted(set(peers.card_id) - {ctx.card_id})
        if len(other) >= 2:
            sigs.append(Signal("template_repeats_across_cards", "network", 1.5,
                               f"The same template (three or more online purchases just under ${thr:,.0f} within 40 minutes) appears on "
                               f"{len(other)} other cards within three weeks", other[:10],
                               T.ref("band_bursts", amt_lo=round(0.88 * thr), amt_hi=int(thr), days=21)))
            fnd.coordinated_undocumented = True
            fnd.notes["template_cards"] = other
    return sigs


def _shared_fingerprint(peers: pd.DataFrame, f):
    """A stable non-zero Vesta time-delta (D4 or D15) or a non-generic device shared by this txn and 3+ other cards."""
    for col in ("D15", "D4"):
        v = f.get(col)
        if pd.notna(v) and v not in (0, 1):
            same = peers[peers[col] == v] if col in peers else peers.iloc[0:0]
            cards = set(same.card_id) - {f["card_id"]}
            if len(cards) >= 3:
                return f"time-delta feature {col} = {int(v)}", sorted(cards)
    return None
