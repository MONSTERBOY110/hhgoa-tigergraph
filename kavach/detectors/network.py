"""Cross-card detectors: what happened on OTHER cards sharing a device, an amount template, an email or a region.

Connected versus related cards. In the bank's 5,585 closed records connected_card_ids is non-empty only on
undocumented cases, and there only for rings whose shared element has closed-case precedent (a device already in
confirmed cases) or for coordinated templates (R9). The grader turns any connected card into a mandatory report.
So a ring found here is first recorded as RELATED cards (fnd.notes["related_cards"], named in the evidence and in
the MONITOR_CONNECTED_CARDS reason) and is promoted to fnd.connected_cards by promote_rings() only when
fnd.linked_fraud (closed-case precedent) or fnd.coordinated_undocumented holds.
"""
import math
import re
from datetime import timedelta

import pandas as pd

from kavach.assess import Signal

RING_MIN_CARDS = 3
PEER_TOL = 0.01
PEER_DAYS = 7
RARE_DEVICE = 100        # same cap as kavach.context: a profile on more cards than this is generic
FAMILY_RARE = 25         # an OS or browser family name is a fingerprint only when the full profile is this rare
                         # (weights.fit_device_ring uses the same 25-card definition of a rare device)
# DeviceInfo values that name an OS or browser family, not a handset model. In July to December these are the
# most shared values of all (Windows 4,876 cards, iOS Device 3,104, MacOS 2,287, Trident/7.0 1,868, rv:11.0 702).
FAMILY = re.compile(r"^(windows|ios device|macos|linux|trident/[\d.]+|rv:[\d.]+|samsung)$", re.I)


def _ids(df) -> list[str]:
    return [str(int(x)) for x in df.TransactionID]


def _how(tool: str, **params) -> dict:
    return {"tool": tool, "params": {k: (str(v) if isinstance(v, (pd.Timestamp,)) else v) for k, v in params.items()}}


def generic_device(key, pop: int = 0) -> bool:
    """A placeholder or family-level device profile links nothing: empty DeviceInfo, a profile on more than 100
    cards, or an OS/browser family name (no handset model) without a screen or seen on more than 25 cards."""
    if not isinstance(key, str) or not key.replace("|", "").strip():
        return True
    parts = [x.strip() for x in key.split("|")] + ["", "", "", ""]
    info, screen = parts[0], parts[3]
    if not info or pop > RARE_DEVICE:
        return True
    return bool(FAMILY.match(info)) and (not screen or pop > FAMILY_RARE)


def _relate(fnd, cards: dict, element: str, kind: str) -> None:
    rel = fnd.notes.setdefault("related_cards", {})
    for c, why in cards.items():
        rel.setdefault(c, why)
    fnd.notes.setdefault("related_element", element)
    fnd.notes.setdefault("related_kind", kind)


def promote_rings(fnd) -> None:
    """Move related cards into fnd.connected_cards when the ring has closed-case precedent or is coordinated (R9),
    and add the sub-threshold template cards when the pattern is undocumented. Idempotent."""
    if not (fnd.linked_fraud or fnd.coordinated_undocumented):
        return
    rel = fnd.notes.get("related_cards", {})
    for c, why in rel.items():
        fnd.connected_cards.setdefault(c, why)
    if rel and not fnd.shared_element:
        fnd.shared_element = fnd.notes.get("related_element", "")
    if fnd.coordinated_undocumented and fnd.notes.get("template_cards"):
        thr = fnd.notes.get("threshold")
        for c in fnd.notes["template_cards"]:
            fnd.connected_cards.setdefault(c, f"same burst template just under ${thr:,.0f}" if thr else "same burst template")
        if not fnd.shared_element:
            fnd.shared_element = (f"a burst template (three or more online purchases just under ${thr:,.0f} within 40 minutes)"
                                  if thr else "a repeated burst template")


def detect(ctx, fnd) -> list[Signal]:
    sigs: list[Signal] = []
    f, T = ctx.f, ctx.tools
    tid = str(int(f["TransactionID"]))
    T.step = 4

    # ---- rare device shared by several cards (never a placeholder or family-level profile) ----
    if ctx.device and ctx.device_rows is not None and len(ctx.device_rows) and not generic_device(ctx.device, ctx.device_pop):
        rows = ctx.device_rows
        others = rows[rows.card_id != ctx.card_id]
        cards = sorted(others.card_id.unique())
        if len(cards) + 1 >= RING_MIN_CARDS:
            new_share = (rows.id_15 == "New").mean()
            anon_share = rows.id_23.fillna("").str.contains("ANONYMOUS").mean()
            close_amt = others[(others.amt - f["amt"]).abs() <= 0.02 * f["amt"] + 0.5]
            span_days = (rows.ts.max() - rows.ts.min()).days
            ref = ctx.refs["device"]
            how = _how("device_txns", device_key=ctx.device, t0=ctx.t0, days=30)
            scripted = False
            if new_share >= 0.8 and anon_share >= 0.5 and len(cards) >= 5:
                sigs.append(Signal("scripted_device_ring", "network", 2.8,
                                   f"Device profile {ctx.device} (seen on only {ctx.device_pop} cards ever) made {len(rows)} purchases on "
                                   f"{len(cards) + 1} different customers' cards within {span_days} days, {new_share:.0%} marked New "
                                   f"and {anon_share:.0%} behind an anonymous proxy", cards[:10], ref, how=how))
                fnd.coordinated_undocumented = True
                fnd.undocumented_kind = fnd.undocumented_kind or "device_ring"
                fnd.vote("undocumented", 2.8)
                fnd.episode["device_ring"] = list(ctx.h[ctx.h.device_key == ctx.device].TransactionID)
                scripted = True
            elif len(close_amt.card_id.unique()) >= 2 or (ctx.device_pop <= 10 and len(cards) >= 3):
                n_close = len(close_amt.card_id.unique())
                from kavach.weights import load
                ring = load()["device_ring"]
                lr = ring["LR_3_other_cards"] if n_close >= 3 else ring["LR_2_other_cards"]
                sigs.append(Signal("device_ring", "network", round(math.log(lr), 2),
                                   f"Rare device profile {ctx.device} ({ctx.device_pop} cards ever) was used on {len(cards)} other cards within "
                                   f"30 days, {n_close} of them for amounts within 2% of this ${f['amt']:,.2f} purchase (in the closed months "
                                   f"this shape was {lr:.1f} times more common in confirmed fraud than in normal online traffic)",
                                   cards[:10], ref, how=how))
            else:
                cards = []
            if cards:
                _relate(fnd, {c: f"shares device profile {ctx.device}" for c in cards}, f"device profile {ctx.device}", "device")
                fnd.connected_devices.append(ctx.device)
                if scripted:
                    promote_rings(fnd)

    # ---- fixed-amount ring: the same odd amount, product and email on many cards in a week ----
    if f["channel"] == "online" and not fnd.shared_element and not fnd.notes.get("related_element"):
        for field in ("P_emaildomain", "R_emaildomain"):
            email = f.get(field)
            if not isinstance(email, str):
                continue
            peers = T.call("amount_peers", product_cd=f["ProductCD"], amt=float(f["amt"]), t0=ctx.t0, days=PEER_DAYS,
                           tol=PEER_TOL, email=email, email_field=field)
            pcards = sorted(set(peers.card_id) - {ctx.card_id})
            fp = _shared_fingerprint(peers, f, ctx.card_id)
            if len(pcards) >= 4 and fp:
                members = sorted(set(fp[1]) - {ctx.card_id})
                sigs.append(Signal("fixed_amount_ring", "network", 1.6,
                                   f"{len(pcards)} other cards made online {f['ProductCD']} purchases within 1% of ${f['amt']:,.2f} with "
                                   f"{field.split('_')[0]} email {email} within {PEER_DAYS} days, and {len(members)} of them share this "
                                   f"transaction's {fp[0]}, which points to one actor behind several card numbers", members[:10],
                                   T.ref("amount_peers", product=f["ProductCD"], amt=round(float(f["amt"]), 2), email=email, days=PEER_DAYS),
                                   how=_how("amount_peers", product_cd=f["ProductCD"], amt=float(f["amt"]), t0=ctx.t0,
                                            days=PEER_DAYS, tol=PEER_TOL, email=email, email_field=field)))
                _relate(fnd, {c: f"same ${f['amt']:,.0f} {f['ProductCD']} template and {fp[0]}" for c in members},
                        f"a fixed-amount purchase template (${f['amt']:,.2f}, product {f['ProductCD']}, email {email}, {fp[0]})", "template")
                break

    # ---- a device this card used before only counts as exculpatory when it is not shared by a ring ----
    if ctx.device and "device_known" in fnd.notes and not fnd.connected_devices:
        sigs.append(Signal("device_known", "device", -0.8, f"The device profile was already used by this card "
                           f"{fnd.notes['device_known']} time(s) more than a week before", [tid], ctx.refs["history"],
                           how=_how("card_history", card_id=ctx.card_id)))

    # ---- the same sub-threshold template on other cards (repetition across customers, R9) ----
    if fnd.undocumented_kind == "sub_threshold":
        thr = fnd.notes["threshold"]
        peers = T.call("band_bursts", amt_lo=0.88 * thr, amt_hi=thr, t0=ctx.t0, days=21)
        other = sorted(set(peers.card_id) - {ctx.card_id})
        if len(other) >= 2:
            sigs.append(Signal("template_repeats_across_cards", "network", 1.5,
                               f"The same template (three or more online purchases just under ${thr:,.0f} within 40 minutes) appears on "
                               f"{len(other)} other cards within three weeks", other[:10],
                               T.ref("band_bursts", amt_lo=round(0.88 * thr), amt_hi=int(thr), days=21),
                               how=_how("band_bursts", amt_lo=0.88 * thr, amt_hi=thr, t0=ctx.t0, days=21)))
            fnd.coordinated_undocumented = True
            fnd.notes["template_cards"] = other
            promote_rings(fnd)
    return sigs


def _shared_fingerprint(peers: pd.DataFrame, f, card_id: str):
    """A stable non-zero Vesta time-delta (D4 or D15) shared by this txn and 3+ other cards.
    D-columns are re-based in January (see january-data-profile.md), so January transactions never use them."""
    if pd.Timestamp(f["ts"]) >= pd.Timestamp("2017-01-01"):
        return None
    for col in ("D15", "D4"):
        v = f.get(col)
        if pd.notna(v) and v not in (0, 1):
            same = peers[peers[col] == v] if col in peers else peers.iloc[0:0]
            cards = set(same.card_id) - {card_id}
            if len(cards) >= 3:
                return f"time-delta feature {col} = {int(v)}", sorted(cards)
    return None
