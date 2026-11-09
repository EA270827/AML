# Run inside tt_indicators.ipynb after all its sections:   %run -i tt_candidates_and_ruleset.py
# (A) REAL-TIME REPLAY of the candidate rules, as a near-real-time system would run them: transactions are replayed in
#     time order and each rule fires on an inward TT using only what was known at that moment.
# (B) The review list of accounts paid by senders who also funded mules, enriched.
# No new transaction pulls (one small account-master query for the review list).

T1_AGE_DAYS, T1_USD, T1_WINDOW = 90, 500_000, "7D"        # young account, large inflow by inward TT
T3_AGE_DAYS, T3_SENDERS, T3_WINDOW_DAYS = 90, 20, 30      # young account, many distinct senders
T2_COUNT, T2_MIN_USD = 3, 10_000                          # identical inward TTs, same sender, same day
BROAD_SENDER_MIN = 10                                     # a mule-linked sender who paid > this many other accounts is "broad"
COOLDOWN_DAYS = 30                                        # repeat alerts of the same rule on one account within this are one alert
HOLD_DAYS = 7                                             # outgoing payments within this many days after an alert go to review

broad = set(shared[shared.other_accounts > BROAD_SENDER_MIN].index)
ev = pd.concat([mule_tx[mule_tx.flow != ""], others[others.flow != ""]], ignore_index=True)
ev = ev[["ACID", "when", "TRAN_DATE", "TRAN_AMT", "flow", "party", "ACCT_OPN_DATE"]].dropna(subset=["when"])
ev = ev.sort_values(["ACID", "when"]).reset_index(drop=True)
mule_set = set(mule_acids)

# when did each sender first fund each mule (for "known sender": funded ANOTHER mule BEFORE this moment)
mi_ = ev[ev.ACID.isin(mule_set) & (ev.flow == "in") & (ev.party.str.len() > 3)]
first_fund = mi_.groupby(["party", "ACID"]).when.min()
fund_by_party = {p: g.droplevel(0).sort_values() for p, g in first_fund.groupby(level=0)}

def known_before(party, acct, t, exclude_broad):
    s = fund_by_party.get(party)
    if s is None or (exclude_broad and party in broad):
        return False
    s = s.drop(acct, errors="ignore")
    return bool(len(s)) and s.iloc[0] < t

# ---------- replay every inward TT ----------
fires = []
d7, d30 = pd.Timedelta(T1_WINDOW), pd.Timedelta(days=T3_WINDOW_DAYS)
for a, g in ev[ev.flow == "in"].groupby("ACID", sort=False):
    w = g.when.to_numpy(); amt = g.TRAN_AMT.to_numpy(); par = g.party.to_numpy()
    age = (g.when - g.ACCT_OPN_DATE).dt.days.to_numpy()
    same = (g.assign(big=g.TRAN_AMT >= T2_MIN_USD).groupby([g.TRAN_DATE, g.party, g.TRAN_AMT]).cumcount() + 1).to_numpy()
    lo7 = lo30 = 0; s7 = 0.0; cnt = {}
    for i in range(len(g)):
        s7 += amt[i]
        while w[lo7] <= w[i] - d7:
            s7 -= amt[lo7]; lo7 += 1
        if par[i]:
            cnt[par[i]] = cnt.get(par[i], 0) + 1
        while w[lo30] <= w[i] - d30:
            if par[lo30]:
                cnt[par[lo30]] -= 1
                if not cnt[par[lo30]]:
                    del cnt[par[lo30]]
            lo30 += 1
        hit = {"T1": age[i] <= T1_AGE_DAYS and s7 >= T1_USD,
               "T3": age[i] <= T3_AGE_DAYS and len(cnt) >= T3_SENDERS,
               "T2": amt[i] >= T2_MIN_USD and same[i] >= T2_COUNT,
               "T5": bool(par[i]) and known_before(par[i], a, w[i], False),
               "T5n": bool(par[i]) and known_before(par[i], a, w[i], True)}
        for r, h in hit.items():
            if h:
                fires.append((a, r, w[i]))
fires = pd.DataFrame(fires, columns=["ACID", "rule", "when"])
SETS = {"T1": ["T1"], "T3": ["T3"], "T5": ["T5"], "T5 narrow": ["T5n"], "T2": ["T2"],
        "RECOMMENDED: T1 or T3 or T5 narrow": ["T1", "T3", "T5n"], "RECOMMENDED + T2": ["T1", "T3", "T5n", "T2"]}
LABEL = {"T1": f"T1 account <= {T1_AGE_DAYS} days old, >= ${T1_USD/1e3:,.0f}k by IT in {T1_WINDOW}",
         "T3": f"T3 account <= {T3_AGE_DAYS} days old, >= {T3_SENDERS} distinct senders in {T3_WINDOW_DAYS} days",
         "T5": "T5 sender had already funded another mule", "T5 narrow": f"T5 narrow (ignores senders who paid > {BROAD_SENDER_MIN} other accounts)",
         "T2": f"T2 {T2_COUNT}rd identical inward TT today from one sender (each >= ${T2_MIN_USD/1e3:,.0f}k)"}

def alerts_after_cooldown(f):
    out = []
    for a, g in f.sort_values("when").groupby("ACID"):
        last = None
        for t in g.when:
            if last is None or t - last > pd.Timedelta(days=COOLDOWN_DAYS):
                out.append((a, t)); last = t
    return pd.DataFrame(out, columns=["ACID", "when"])

# ---------- (A1) what each mule would have looked like at its first alert ----------
mt_all = mule_tx.sort_values("when")
def at_alert(a, t):
    m = mt_all[mt_all.ACID == a]; before = m.when <= t
    ins, outs = m[m.flow == "in"], m[m.flow == "out"]
    bal = (np.where(m.is_cr, m.TRAN_AMT, -m.TRAN_AMT) * before).sum()
    out_tot = outs.TRAN_AMT.sum()
    return {"first alert": t, "days since opening": (t - m.ACCT_OPN_DATE.min()).days,
            "IT before alert $k": ins[ins.when <= t].TRAN_AMT.sum() / 1e3, "IT after $k": ins[ins.when > t].TRAN_AMT.sum() / 1e3,
            "out before alert $k": outs[outs.when <= t].TRAN_AMT.sum() / 1e3, "out after $k": outs[outs.when > t].TRAN_AMT.sum() / 1e3,
            "balance at alert $k": bal / 1e3,
            "share of outflow still to come": outs[outs.when > t].TRAN_AMT.sum() / out_tot if out_tot else np.nan}

rec = SETS["RECOMMENDED: T1 or T3 or T5 narrow"]
rows = []
for a in new_acids + old_acids:
    f = fires[(fires.ACID == a) & fires.rule.isin(rec)]
    r = {"account": str(names[a])[:26], "group": group[a],
         **{k: (fires[(fires.ACID == a) & (fires.rule == k)].when.min()) for k in ["T1", "T3", "T5n", "T2"]}}
    r.update(at_alert(a, f.when.min()) if len(f) else {"first alert": pd.NaT})
    rows.append(r)
M = pd.DataFrame(rows).set_index("account")
print("(A1) EACH MULE UNDER THE RECOMMENDED SET (T1 or T3 or T5 narrow), replayed in real time")
display(M.apply(lambda c: c.round(2) if c.dtype.kind == 'f' else c))
caught = M["first alert"].notna()
print(f"Caught: {int(caught[M.group == 'new mule'].sum())} of {int((M.group == 'new mule').sum())} new, "
      f"{int(caught[M.group == 'original mule'].sum())} of {int((M.group == 'original mule').sum())} original. "
      f"Median share of each caught mule's outflow still to come at the first alert: {M.loc[caught, 'share of outflow still to come'].median():.0%}")

# ---------- (A2) alert volume on everyone else, real-time, with cooldown ----------
oth_f = fires[~fires.ACID.isin(mule_set) & (fires.when >= TT_FROM)]
vol = []
for name, rules in SETS.items():
    al = alerts_after_cooldown(oth_f[oth_f.rule.isin(rules)])
    mf = fires[fires.ACID.isin(mule_set) & fires.rule.isin(rules)]
    vol.append({"rule set": LABEL.get(name, name),
                "new mules": int(mf.ACID.isin(new_acids).groupby(mf.ACID).any().sum()) if len(mf) else 0,
                "original mules": int(mf[mf.ACID.isin(old_acids)].ACID.nunique()),
                "alerts per month": round(len(al) / months, 1), "accounts alerted": al.ACID.nunique()})
volume_rt = pd.DataFrame(vol).set_index("rule set")
print(f"\n(A2) REAL-TIME ALERT VOLUME on non-mule accounts since {TT_FROM} ({months} complete months; "
      f"repeat alerts within {COOLDOWN_DAYS} days counted once)")
display(volume_rt)

# ---------- (A3) follow-up: outgoing payments from alerted accounts go to review before release ----------
al_rec = alerts_after_cooldown(oth_f[oth_f.rule.isin(rec)])
outs_o = ev[(ev.flow == "out") & ~ev.ACID.isin(mule_set) & (ev.when >= TT_FROM)][["ACID", "when", "TRAN_AMT"]]
held = outs_o.merge(al_rec.rename(columns={"when": "alert"}), on="ACID")
held = held[(held.when >= held.alert) & (held.when <= held.alert + pd.Timedelta(days=HOLD_DAYS))].drop_duplicates(["ACID", "when", "TRAN_AMT"])
print(f"\n(A3) HOLD-ON-OUTGOING: payments out of an alerted non-mule account within {HOLD_DAYS} days of its alert")
print(f"   {len(held) / months:.1f} payments per month for review (${held.TRAN_AMT.sum() / months / 1e3:,.0f}k per month), "
      f"on {held.ACID.nunique()} accounts over the period")
mh = []
for a in [x for x in new_acids + old_acids if pd.notna(M.loc[str(names[x])[:26], "first alert"])]:
    t = M.loc[str(names[a])[:26], "first alert"]
    o = mt_all[(mt_all.ACID == a) & (mt_all.flow == "out")]
    mh.append({"account": str(names[a])[:26], "would be held $k (next 7 days)":
               o[(o.when >= t) & (o.when <= t + pd.Timedelta(days=HOLD_DAYS))].TRAN_AMT.sum() / 1e3,
               "all outflow after alert $k": o[o.when > t].TRAN_AMT.sum() / 1e3})
if mh:
    print("   For the mules it catches:")
    display(pd.DataFrame(mh).set_index("account").round(0))

# ---------- (B) Review list: accounts paid by senders who also funded mules ----------
t1s = set(fires[fires.rule == "T1"].ACID); t3s = set(fires[fires.rule == "T3"].ACID)
ids = list(cand.index)
R = pd.DataFrame()
if ids:
    if SYNTHETIC:
        mst = syn_all[syn_all.ACID.isin(ids)].drop_duplicates("ACID")[["ACID", "FORACID", "ACCT_NAME", "ACCT_OPN_DATE"]]
    else:
        mst = read_chunks(f"SELECT ACID, FORACID, ACCT_NAME, CIF_ID, ACCT_OPN_DATE, ACCT_CLS_FLG, SCHM_CODE FROM {GAM} "
                          "WHERE ACID IN ({ids})", ids, fin())
    R = mst.set_index("ACID")
    ci = itc[itc.ACID.isin(ids)]
    ring = set(shared.index) - broad
    R["ring senders"] = [", ".join(sorted(set(ci[ci.ACID == a].party) & ring))[:70] for a in R.index]
    R["n ring senders"] = [len(set(ci[ci.ACID == a].party) & ring) for a in R.index]
    R["n broad senders"] = [len(set(ci[ci.ACID == a].party) & broad) for a in R.index]
    R["from shared senders $k"] = cand.received_k.reindex(R.index)
    bf = book_feat.reindex(R.index)
    R["all IT $k"] = bf["IT total $k"].round(0); R["all senders"] = bf["distinct senders"]
    R["out / IT"] = bf["out / IT"].round(2); R["out within 72h"] = bf["IT money out within 72h"].round(2)
    R["days open at first IT"] = bf["days open at first IT"]
    R["T1"] = R.index.isin(list(t1s)); R["T3"] = R.index.isin(list(t3s))
    try:
        sm = pd.read_csv("outputs/early_signals_scan/scan_metrics.csv", usecols=["ACID", "first_hit_day"])
        R["awakening rule"] = R.index.map(sm.set_index("ACID").first_hit_day.notna()).fillna(False)
    except Exception:
        R["awakening rule"] = "n/a"
    try:
        nr = pd.read_csv("outputs/network_onboarding/accounts_in_reach.csv", usecols=["ACID", "hop"])
        R["linked to mule owners"] = R.index.map(nr.drop_duplicates("ACID").set_index("ACID").hop).fillna("")
    except Exception:
        R["linked to mule owners"] = "n/a"
    try:
        if SYNTHETIC:
            raise RuntimeError("synthetic")
        leads = pd.read_sql("SELECT * FROM aml_top_leads", rep()); leads.columns = [c.lower() for c in leads.columns]
        R["production lead"] = R.index.isin(set(leads["acid"].astype(str)))
    except Exception:
        R["production lead"] = "n/a"
    R["tier"] = np.select([R["n ring senders"] >= 2, (R["n ring senders"] == 1) & (R["from shared senders $k"] >= 10)],
                          ["A: 2+ ring senders", "B: 1 ring sender, >= $10k"], "C: broad senders or tiny amounts")
    R = R.sort_values(["tier", "from shared senders $k"], ascending=[True, False])
    print(f"\n(B) REVIEW LIST: {len(R)} accounts paid by senders who also funded mules — linked by who paid them, not confirmed mules")
    display(R)

M.to_csv(OUT / "rt_mules_at_first_alert.csv"); volume_rt.to_csv(OUT / "rt_alert_volume.csv")
if len(R):
    R.to_csv(OUT / "tt_review_list.csv")
print("Saved rt_mules_at_first_alert.csv, rt_alert_volume.csv" + (", tt_review_list.csv" if len(R) else ""), "to", OUT.resolve())
