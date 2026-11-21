STATUS = "partial"


def stamps_of(conn, store_id: str | None = None) -> dict:
    q = "SELECT id, store_id, weekday_mask, row_hash FROM dim_store_hours_hist WHERE 1=1"
    args: list = []
    if store_id:
        q += " AND store_id=?"
        args.append(store_id)
    q += " ORDER BY valid_from DESC LIMIT 50"
    hist = [dict(r) for r in conn.execute(q, args).fetchall()]
    return {
        "hist_n": len(hist),
        "stamps": [],
        "epoch_ready": False,
    }
