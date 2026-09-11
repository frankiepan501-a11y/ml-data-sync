"""One bounded, network-free parsing process; stdout contains only a summary."""
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.final_billing import read_local_export


def parse(path, fid):
    with closing(sqlite3.connect(path)) as c:
        row = c.execute("SELECT seller,kind,name,content FROM files WHERE id=?", (fid,)).fetchone()
    if not row:
        return {"state": "failed", "reason": "原件不存在"}
    seller, kind, name, content = row
    if kind != "charges":
        return {"state": "needs_review", "reason": "已收存证明；账期、单据清单和正式出账状态仍需核实"}
    if seller == "1502236229":
        return {"state": "needs_review", "reason": "CBT原件已收，待独立模板适配；不能据此认定核销通过"}
    if not name.lower().endswith(".xlsx"):
        return {"state": "needs_review", "reason": "已收；自动读取费用明细需要平台原始XLSX文件"}
    try:
        result = read_local_export(content, seller, "BRL" if seller == "2378517428" else "MXN")
        return {"state": "parsed", "rows": len(result["rows"]), "duplicates": result["duplicates"],
                "month_counts": result["month_counts"], "reason": "费用明细已读取；店铺归属、正式账期与单据完整性仍待核实"}
    except Exception as exc:
        # Known validation errors contain row/field information, never credentials.
        reason = str(exc)[:250] if isinstance(exc, ValueError) else "文件损坏或模板无法自动读取，请核对官方导出原件"
        return {"state": "failed", "reason": reason}


if __name__ == "__main__":
    print(json.dumps(parse(sys.argv[1], sys.argv[2]), ensure_ascii=True))
