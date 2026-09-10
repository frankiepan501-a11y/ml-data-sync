"""Publish the confirmed monthly report to the finance team's fixed entry."""
import re
from urllib.parse import urlencode, urlparse

BASE = "P9awbhG9faFstxsO1KZc9b9Qnxb"
TABLE = "tblrProDcHtwD5Vr"
FIELD = "美客多毛利报表"
ROOT = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE}/tables/{TABLE}"


async def rollback(receipt, token, request):
    endpoint = ROOT + "/records/" + receipt["record_id"]
    current = (await request("GET", endpoint, token))["data"]["record"]
    if current["fields"].get(FIELD) != receipt["current"]:
        raise RuntimeError("公司汇总已被其他操作修改，停止回滚")
    await request("PUT", endpoint, token, {"fields": {FIELD: receipt["previous"]}})
    checked = (await request("GET", endpoint, token))["data"]["record"]
    if checked["fields"].get(FIELD) != receipt["previous"]:
        raise RuntimeError("公司汇总回滚核验失败")


async def publish(period, report_url, close_mode, token, request):
    if not re.fullmatch(r"month_\d{4}-(0[1-9]|1[0-2])", period):
        raise ValueError("公司汇总月份无效")
    if close_mode not in ("operating", "final"):
        raise ValueError("审核草稿不得发布到公司汇总")
    parsed = urlparse(report_url)
    if parsed.scheme != "https" or parsed.netloc != "u1wpma3xuhr.feishu.cn" or not parsed.path.startswith(("/wiki/", "/sheets/")):
        raise ValueError("公司汇总必须使用已冻结报表链接")
    month = period[6:].replace("-", "/")
    fields = await request("GET", ROOT + "/fields?page_size=100", token)
    schema = [f for f in fields["data"]["items"] if f["field_name"] == FIELD]
    if len(schema) != 1 or schema[0]["type"] != 15:
        raise RuntimeError("公司汇总美客多链接字段缺失或类型不符")
    matches, page, seen = [], "", set()
    while True:
        query = {"page_size": 500, "field_names": '["日期","美客多毛利报表"]'}
        if page:
            query["page_token"] = page
        data = (await request("GET", ROOT + "/records?" + urlencode(query), token))["data"]
        matches.extend(r for r in data["items"] if r["fields"].get("日期") == month)
        if not data.get("has_more"):
            break
        page = data.get("page_token")
        if not page or page in seen:
            raise RuntimeError("公司汇总分页异常")
        seen.add(page)
    if len(matches) != 1:
        raise RuntimeError("公司汇总月份记录缺失或重复，请财务检查日期列")
    record = matches[0]
    endpoint = ROOT + "/records/" + record["record_id"]
    current = (await request("GET", endpoint, token))["data"]["record"]
    if current["fields"].get("日期") != month:
        raise RuntimeError("公司汇总月份在发布期间变化")
    before = current["fields"].get(FIELD)
    label = "经营暂结·财务已确认" if close_mode == "operating" else "最终核销·财务已确认"
    target = {"link": report_url, "text": f"美客多毛利报表-{month}（{label}）"}
    if close_mode == "operating" and before and "最终核销" in before.get("text", ""):
        raise RuntimeError("公司汇总已有最终核销版，禁止降级为暂结")
    if before != target:
        await request("PUT", endpoint, token, {"fields": {FIELD: target}})
    checked = (await request("GET", endpoint, token))["data"]["record"]
    if checked["fields"].get("日期") != month or checked["fields"].get(FIELD) != target:
        raise RuntimeError("公司汇总链接回填后核验失败")
    return {"verified": True, "record_id": record["record_id"], "previous": before, "current": target}
