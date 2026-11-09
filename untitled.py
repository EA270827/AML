# Profile the 3 new mules on portable indicators, then test simple rules on the whole book (no Finacle queries)
W = 30                      # look at the first 30 days after the first meaningful credit
FIRST_CREDIT_MIN = 1_000    # a "meaningful" credit: at least $1,000 on one day

c = tx[tx.stream == "customer"]
acc = sorted(set(c.ACID))
ai = {a: i for i, a in enumerate(acc)}
CR = np.zeros((len(acc), WINDOW_DAYS)); DR = np.zeros_like(CR)
g = c.groupby(["ACID", "day", "dir"]).TRAN_AMT.sum().reset_index()
for d, M in (("cr", CR), ("dr", DR)):
    s = g[g.dir == d]; M[s.ACID.map(ai).to_numpy(), s.day.to_numpy().astype(int)] = s.TRAN_AMT.to_numpy()

first = np.where((CR >= FIRST_CREDIT_MIN).any(1), (CR >= FIRST_CREDIT_MIN).argmax(1), -1)
days = np.arange(WINDOW_DAYS)
inwin = (days >= first[:, None]) & (days < first[:, None] + W) & (first[:, None] >= 0)
cr_w, dr_w = (CR * inwin).sum(1), (DR * inwin).sum(1)

def out_within(k):
    '''Share of window credits that left within k days (first in, first out, by day).'''
    cc, cd = CR.cumsum(1), DR.cumsum(1)
    prev = np.concatenate([np.zeros((len(acc), 1)), cc[:, :-1]], 1)
    later = np.concatenate([cd[:, k:], np.repeat(cd[:, -1:], k, 1)], 1) if k else cd
    covered = np.clip(later - prev, 0, CR)
    return np.divide((covered * inwin).sum(1), cr_w, out=np.full(len(acc), np.nan), where=cr_w > 0)

bal = (CR - DR).cumsum(1)
active = (CR + DR) * inwin >= 100
kept = np.array([np.median(np.abs(bal[i][active[i]])) / max(CR[i][active[i]].mean(), 1) if active[i].any() else np.nan
                 for i in range(len(acc))])
prof = pd.DataFrame({
    "first meaningful credit (day)": first, f"credits in first {W} days ($k)": cr_w / 1e3,
    f"debits in first {W} days ($k)": dr_w / 1e3,
    "share out same day": out_within(0), "share out within 2 days": out_within(2),
    "share out within 7 days": out_within(7),
    "largest credit day / window credits": np.divide((CR * inwin).max(1), cr_w, out=np.full(len(acc), np.nan), where=cr_w > 0),
    "active days in window": active.sum(1),
    "balance kept vs a day's credits": kept,
    "biggest single day in+out ($k)": ((CR + DR) * inwin).max(1) / 1e3,
    "turnover in first 30 days of life ($)": (CR[:, :30] + DR[:, :30]).sum(1),
}, index=pd.Index(acc, name="ACID"))

# counterparties and channels in the window, from narrations (rough)
cw = c[c.ACID.map(lambda a: first[ai[a]]).le(c.day) & c.day.lt(c.ACID.map(lambda a: first[ai[a]] + W)) &
       c.ACID.map(lambda a: first[ai[a]] >= 0)].copy()
cw["party"] = (cw.TRAN_PARTICULAR.fillna("").str.upper().str.replace(r"^\d+/", "", regex=True)
                 .str.replace(r"\d{4,}", "#", regex=True).str.replace(r"\s+", " ", regex=True).str.strip())
cw["round_k"] = (cw.TRAN_AMT % 1000 == 0)
cw["tt"] = cw.party.str.contains(r"\bTT\b|SWIFT|INWARD|OUTWARD|RTGS|TELEGRAPHIC", regex=True)
agg = cw.groupby(["ACID", "dir"]).agg(n=("TRAN_AMT", "size"), parties=("party", "nunique"),
                                      round_share=("round_k", "mean"), tt_share=("tt", "mean")).unstack("dir")
for d, lab in (("cr", "credits"), ("dr", "debits")):
    prof[f"number of {lab}"] = agg.get(("n", d))
    prof[f"distinct narrations on {lab}"] = agg.get(("parties", d))
    prof[f"{lab} that are round thousands"] = agg.get(("round_share", d))
    prof[f"{lab} by TT/SWIFT/RTGS"] = agg.get(("tt_share", d))

grp = scan.set_index("ACID").group
names = scan.set_index("ACID").ACCT_NAME
new_ids = [a for a in scan[scan.group == "new mule"].ACID if a in prof.index]
view = prof.loc[new_ids].T
view.columns = [str(names[a])[:22] for a in new_ids]
view["10 original mules (median)"] = prof[prof.index.map(grp).isin(["reference"])].median()
view["ordinary, active (median)"] = prof[prof.index.map(grp).isin(["other"]) & (prof[f"credits in first {W} days ($k)"] > 0)].median()
print("PROFILE OF THE 3 NEW MULES (first 30 days after their first meaningful credit)")
display(view.round(2))

# for each new mule, its biggest flows in the window
print("\nBiggest flows in the window, by narration:")
for a in new_ids:
    t = cw[cw.ACID == a].groupby(["dir", "party"]).TRAN_AMT.agg(["sum", "size"]).sort_values("sum", ascending=False).head(6)
    print(f"\n{names[a]}"); print((t.assign(sum=lambda d: (d["sum"] / 1e3).round(0)).rename(columns={"sum": "$k", "size": "count"})).to_string())

# --- simple rules another system could run, tested on the whole book ---
rules = {
    "R1 large early inflow: >= $500k credited within 30 days of first credit": prof[f"credits in first {W} days ($k)"] >= 500,
    "R2 pass-through: R1 and >= 90% of it out within 2 days": (prof[f"credits in first {W} days ($k)"] >= 500) & (prof["share out within 2 days"] >= 0.9),
    "R3 quiet then sudden: < $1k in first 30 days of life, then one day >= $500k in and out":
        (prof["turnover in first 30 days of life ($)"] < 1_000) & (prof["biggest single day in+out ($k)"] >= 1_000) & (prof["share out same day"] >= 0.9),
    "R2 or R3": None,
}
rules["R2 or R3"] = rules[list(rules)[1]] | rules[list(rules)[2]]
gp = prof.index.map(grp)
rows = []
for name, hit in rules.items():
    o = hit[gp == "other"]
    rows.append({"rule": name,
                 "new mules (3)": ", ".join(str(names[a])[:12] for a in new_ids if hit.get(a, False)) or "none",
                 "original 10": f"{int(hit[gp == 'reference'].sum())} of {int((gp == 'reference').sum())}",
                 "ordinary accounts": f"{int(o.sum()):,} ({o.mean():.1%})",
                 "per month": round(o.sum() / max(len(full), 1), 1)})
print("\nRULES TESTED ON THE WHOLE BOOK (ordinary accounts with any customer activity)")
display(pd.DataFrame(rows).set_index("rule"))