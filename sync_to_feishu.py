# sync_to_feishu.py — 腾讯文档 → 飞书多维表格同步（v5.3）

import json
import os
import time
import re
import base64
import zlib
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.parse import urlencode

# ============================================
# 运行模式
# ============================================
DRY_RUN = False
TEST_LIMIT = 0
BUFFER_MINUTES = 5

# ============================================
# Secrets
# ============================================
TENCENT_FILE_ID = os.environ.get("TENCENT_FILE_ID", "DYXV0TXpQaW9BcnNy")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BITALBE_APP_TOKEN = os.environ.get("APP_TOKEN", "")

# ============================================
# 表1: 合作资料卡
# ============================================
TENCENT_SHEET_ID_1 = os.environ.get("TENCENT_SHEET_ID", "ss_o3cmnf")
TABLE_ID_1 = os.environ.get("TABLE_ID", "")

FIELD_MAPPING_1 = {
    "提交时间（自动）": "提交时间（自动）",
    "合作档期（必填）": "合作档期",
    "返点（必填）": "返点",
    "该号是否可以发Live图？（必填）": "该号是否可以发Live图？",
    "需在本品合作笔记下安排5条正向评论可否接受？（必填）": "需在本品合作笔记下安排5条正向评论可否接受？",
    "小红书昵称（必填）": "小红书昵称",
    "合作后是否可以高配合进行评论区维护？（必填）": "合作后是否可以高配合进行评论区维护？",
    "小红书ID（必填）": "小红书ID",
    "本品排竞期前后15天是否接受？（必填）": "本品排竞期前后15天是否接受？",
    "合作价格（必填）": "合作价格",
    "宝宝月龄": "宝宝月龄",
    "主页链接（必填）": "主页链接",
    "提交者（自动）": "提交者（源）",
}

FIELD_TYPES_1 = {
    "合作价格": "number",
    "返点": "number",
    "提交时间（自动）": "datetime",
    "合作档期": "datetime",
    "主页链接": "url",
}

ID_FIELD_1 = "小红书ID"
TIME_FIELD_1 = "提交时间（自动）"
ID_SRC_COL_1 = "小红书ID（必填）"


# ============================================
# 腾讯文档数据获取（公开接口）
# ============================================

def fetch_tencent_docs_data(file_id, sheet_id):
    """使用 dop-api/opendoc 获取智能表格全部数据"""

    print(f"[{datetime.now():%H:%M:%S}] 正在从腾讯文档读取数据 (sheet={sheet_id})...")

    url = (
        f"https://docs.qq.com/dop-api/opendoc"
        f"?tab={sheet_id}&u=&noEscape=1"
        f"&enableSmartsheetSplit=1&supportOptimizedVer=2"
        f"&startrow=0&endrow=10000"
        f"&id={file_id}&normal=1&outformat=1"
        f"&wb=1&nowb=0&callback=clientVarsCallback&xsrf="
    )
    headers = {
        "Referer": f"https://docs.qq.com/smartsheet/{file_id}?tab={sheet_id}",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": "Mozilla/5.0 (compatible; GitHub-Actions)",
    }

    req = Request(url, headers=headers)
    resp = urlopen(req, timeout=60)
    data = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        import gzip
        data = gzip.decompress(data)
    text = data.decode("utf-8", errors="replace")

    # 解析 JSONP
    m = re.match(r'clientVarsCallback\((.*)\);?\s*$', text.strip(), re.DOTALL)
    if not m:
        raise Exception(f"opendoc 返回格式异常，前200字符:\n{text[:200]}")
    obj = json.loads(m.group(1))

    # 解码 smartsheet
    ccv = obj["clientVars"]["collab_client_vars"]
    b64 = ccv["initialAttributedText"]["text"][0]["smartsheet"]
    b64_padded = b64 + "=" * (4 - len(b64) % 4) if len(b64) % 4 else b64
    raw = base64.b64decode(b64_padded)
    decompressed = zlib.decompress(raw)
    smartsheet = json.loads(decompressed.decode("utf-8"))

    config = smartsheet[0][0]
    field_defs = config["c"]["k3"]["k3"]
    records_data = smartsheet[0][1]["c"]["k2"]["k1"]

    field_order = []
    field_names = {}
    opt_maps = {}

    for fid, fconf in field_defs.items():
        name = fconf.get("k30", fid)
        field_order.append(fid)
        field_names[fid] = name
        if "k17" in fconf and "k3" in fconf["k17"]:
            m = {}
            for opt in fconf["k17"]["k3"]:
                m[opt.get("k1", "")] = opt.get("k2", "")
            opt_maps[fid] = m
        if "k9" in fconf:
            k9 = fconf["k9"]
            if isinstance(k9, dict) and "k3" in k9:
                m = {}
                for opt in k9["k3"]:
                    m[opt.get("k1", "")] = opt.get("k2", "")
                opt_maps[fid] = m

    def parse_k36(cell):
        if "k36" not in cell:
            return None
        k36 = cell["k36"]
        if isinstance(k36, dict) and "k1" in k36:
            try:
                inner = json.loads(k36["k1"])
                data = inner.get("data", [])
                if data:
                    return data[0].get("text", data[0].get("number", ""))
            except:
                pass
        return None

    def fmt_ts(val):
        try:
            ts = int(val)
            if ts > 1000000000000:
                tz = timezone(timedelta(hours=8))
                return datetime.fromtimestamp(ts / 1000, tz).strftime("%Y-%m-%d %H:%M:%S")
        except:
            pass
        return str(val)

    def extract_value(cell):
        if not isinstance(cell, dict):
            return ""
        if len(cell) == 1 and "k1" in cell:
            cell = cell["k1"]
            if not isinstance(cell, dict):
                return str(cell)
        if "k1" in cell:
            k1 = cell["k1"]
            if isinstance(k1, list) and k1:
                item = k1[0]
                if isinstance(item, dict):
                    return item.get("k2", item.get("text", str(item)))
                return str(item)
            if isinstance(k1, str):
                return k1
            return str(k1)
        if "k2" in cell:
            return str(cell["k2"])
        if "k4" in cell:
            return fmt_ts(cell["k4"])
        if "k6" in cell:
            k6 = cell["k6"]
            if isinstance(k6, list) and k6:
                item = k6[0]
                if isinstance(item, dict):
                    return item.get("k1", item.get("k2", "(图片)"))
                return str(item)
            return "(图片)"
        if "k8" in cell:
            k8 = cell["k8"]
            if isinstance(k8, list) and k8:
                first = k8[0]
                if isinstance(first, dict):
                    return first.get("k2", "")
        if "k9" in cell:
            k9 = cell["k9"]
            return [str(x) for x in k9] if isinstance(k9, list) else str(k9)
        if "k10" in cell:
            k10 = cell["k10"]
            return str(k10[0]) if isinstance(k10, list) and k10 else str(k10)
        if "k17" in cell:
            k17 = cell["k17"]
            return str(k17[0]) if isinstance(k17, list) and k17 else str(k17)
        if "k19" in cell:
            n = parse_k36(cell)
            return n if n is not None else ""
        if "k20" in cell:
            if cell["k20"] is not None:
                return str(cell["k20"])
        n = parse_k36(cell)
        if n is not None:
            return str(n)
        if "k23" in cell:
            k23 = cell["k23"]
            return str(k23[0]) if isinstance(k23, list) and k23 else str(k23)
        if "k26" in cell:
            return str(cell["k26"])
        return ""

    def resolve_opt(fid, val):
        if fid not in opt_maps:
            return val
        mp = opt_maps[fid]
        if isinstance(val, list):
            return ", ".join(mp.get(v, v) for v in val)
        return mp.get(val, val)

    print(f"  解析 {len(records_data)} 行数据...")
    all_rows = []
    for _, row_val in records_data.items():
        row = {}
        cells = row_val.get("k1", {})
        for fid in field_order:
            col_name = field_names[fid]
            raw = extract_value(cells.get(fid, {}))
            raw = resolve_opt(fid, raw)
            row[col_name] = raw
        all_rows.append(row)

    print(f"  提取完成，{len(all_rows)} 行 {len(field_order)} 列")
    return all_rows


# ============================================
# 字段值类型转换
# ============================================

def convert_field(feishu_col_name, value, field_types):
    ft = field_types.get(feishu_col_name, "text")
    if ft == "number":
        if value == "" or value is None:
            return None
        try:
            cleaned = str(value).replace(",", "").replace(" ", "").replace("¥", "").replace("元", "")
            if cleaned == "" or cleaned == "-":
                return None
            return float(cleaned) if "." in cleaned else int(cleaned)
        except (ValueError, TypeError):
            return None
    if ft == "url":
        if value == "" or value is None:
            return None
        v = str(value).strip()
        if len(v) < 8:
            return None
        if v.lower().startswith("http://") or v.lower().startswith("https://"):
            if "." in v[8:]:
                return {"link": v}
            return None
        if re.search(r'[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,}', v):
            return {"link": "https://" + v}
        return None
    if ft == "datetime":
        if value == "" or value is None:
            return None
        try:
            tz = timezone(timedelta(hours=8))
            dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=tz)
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError, OSError):
            return None
    if value == "" or value is None:
        return None
    return str(value)


# ============================================
# 飞书 API
# ============================================
class FeishuAPI:
    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None

    def _get_token(self):
        if self._token:
            return self._token
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
        req = Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        resp = json.loads(urlopen(req, timeout=10).read().decode("utf-8"))
        self._token = resp.get("tenant_access_token", "")
        return self._token

    def request(self, method, url, params=None, body=None, timeout=20):
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        full_url = url
        if params:
            full_url += "?" + urlencode(params)
        data = None
        if body:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(full_url, data=data, headers=headers, method=method)
        resp = json.loads(urlopen(req, timeout=timeout).read().decode("utf-8"))
        if resp.get("code") != 0:
            raise Exception(f"飞书 API 错误: {resp.get('msg')}")
        return resp.get("data", resp)

    def get_all_records(self, app_token, table_id):
        all_items = []
        page_token = None
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self.request(
                "GET",
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                params=params,
                timeout=20,
            )
            all_items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token", "")
            if not page_token:
                break
        return all_items

    def get_existing_analysis(self, app_token, table_id, id_field):
        items = self.get_all_records(app_token, table_id)
        existing_ids = set()
        max_sync_time = 0
        for item in items:
            fields = item.get("fields", {})
            id_val = str(fields.get(id_field, "")).strip()
            if id_val:
                existing_ids.add(id_val)
            sync_ts = fields.get("同步时间", 0)
            try:
                sync_ts = int(sync_ts)
                if sync_ts > max_sync_time:
                    max_sync_time = sync_ts
            except (ValueError, TypeError):
                pass
        return existing_ids, max_sync_time

    def insert_records(self, app_token, table_id, records):
        success = 0
        total = len(records)
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        for batch_start in range(0, total, 500):
            batch = records[batch_start:batch_start + 500]
            try:
                body = {"records": [{"fields": r} for r in batch]}
                self.request("POST", url, body=body, timeout=60)
                success += len(batch)
                print(f"  进度: {success}/{total}")
            except Exception as e:
                print(f"  批量写入失败: {e}")
                for i, record in enumerate(batch):
                    try:
                        single_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
                        self.request("POST", single_url, body={"fields": record})
                        success += 1
                    except Exception as e2:
                        print(f"  单条写入失败 [{batch_start + i + 1}]: {e2}")
            time.sleep(0.5)
        return success


# ============================================
# 核心：增量过滤 + ID去重
# ============================================
def filter_and_dedup(all_rows, existing_ids, id_src_col):
    # Step 1: ID 去重（已在飞书的跳过）
    candidates = []
    skipped_ids = []
    for row in all_rows:
        id_val = str(row.get(id_src_col, "")).strip()
        if not id_val:
            candidates.append(row)
            continue
        if id_val in existing_ids:
            skipped_ids.append(id_val)
            continue
        candidates.append(row)

    print(f"\n  {'─' * 40}")
    print(f"  腾讯文档总行数: {len(all_rows)}")
    print(f"  飞书已有 ID 数: {len(existing_ids)}")
    print(f"  ID 去重跳过:     {len(skipped_ids)} 条")
    if skipped_ids:
        print(f"  跳过的 ID:       {skipped_ids[:5]}{'...' if len(skipped_ids) > 5 else ''}")
    print(f"  候选行数:        {len(candidates)} 条")

    if not candidates:
        return [], {"total": len(all_rows), "skipped_dup": len(skipped_ids), "to_write": 0, "batch_dup": 0}

    # Step 2: 批次内部 ID 去重（保留首条）
    seen_ids = set()
    deduped = []
    batch_dup = 0
    for row in candidates:
        id_val = str(row.get(id_src_col, "")).strip()
        if id_val and id_val in seen_ids:
            batch_dup += 1
            continue
        if id_val:
            seen_ids.add(id_val)
        deduped.append(row)

    if batch_dup:
        print(f"  批次内重复:      {batch_dup} 条（已去重）")
    print(f"  最终写入:        {len(deduped)} 条")

    stats = {
        "total": len(all_rows),
        "skipped_dup": len(skipped_ids),
        "to_write": len(deduped),
        "batch_dup": batch_dup,
    }
    return deduped, stats


# ============================================
# 配置校验
# ============================================
def check_config():
    errors = []
    if not TENCENT_FILE_ID:
        errors.append("TENCENT_FILE_ID 未设置")
    if not FEISHU_APP_ID:
        errors.append("FEISHU_APP_ID 未设置")
    if not FEISHU_APP_SECRET:
        errors.append("FEISHU_APP_SECRET 未设置")
    if not BITALBE_APP_TOKEN:
        errors.append("APP_TOKEN 未设置")
    if not TABLE_ID_1:
        errors.append("TABLE_ID 未设置")
    if errors:
        print("配置错误:")
        for e in errors:
            print(f"  ✗ {e}")
        exit(1)
    print("配置校验通过 ✓")


# ============================================
# 单表同步
# ============================================
def sync_single_table(api, label, sheet_id, table_id, field_mapping, field_types,
                      id_field, time_field, id_src_col):
    print()
    print("=" * 60)
    print(f"  [{label}] 同步分析")
    print(f"  时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    all_rows = fetch_tencent_docs_data(TENCENT_FILE_ID, sheet_id)
    if TEST_LIMIT > 0:
        all_rows = all_rows[:TEST_LIMIT]
        print(f"  [测试] 仅处理前 {len(all_rows)} 条")

    existing_ids, max_sync_time = api.get_existing_analysis(
        BITALBE_APP_TOKEN, table_id, id_field
    )

    if time_field and max_sync_time > 0:
        cn_tz = timezone(timedelta(hours=8))
        sync_dt = datetime.fromtimestamp(max_sync_time / 1000, cn_tz)
        cutoff = sync_dt - timedelta(minutes=BUFFER_MINUTES)
        print(f"\n  上次同步时间: {sync_dt:%Y-%m-%d %H:%M:%S}")
        print(f"  回看缓冲:       {BUFFER_MINUTES} 分钟")
        print(f"  过滤起点:       {cutoff:%Y-%m-%d %H:%M:%S}")

        filtered = []
        before_cutoff = 0
        for row in all_rows:
            ts_str = row.get(time_field, "")
            try:
                row_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=cn_tz)
                if row_time > cutoff:
                    filtered.append(row)
                else:
                    before_cutoff += 1
            except (ValueError, TypeError):
                filtered.append(row)
        print(f"  时间过滤:       跳过 {before_cutoff} 条 (早于 {cutoff:%H:%M})，进入 {len(filtered)} 条")
        all_rows = filtered
    elif max_sync_time == 0:
        print(f"\n  飞书表格为空 → 全量模式")

    new_rows, stats = filter_and_dedup(all_rows, existing_ids, id_src_col)

    if new_rows:
        print(f"\n  --- 待写入数据预览 ---")
        for i, row in enumerate(new_rows[:5]):
            id_val = str(row.get(id_src_col, ""))
            print(f"  [{i+1}] ID={id_val}")
        if len(new_rows) > 5:
            print(f"  ... 共 {len(new_rows)} 条")

    print(f"\n  待写入: {stats['to_write']} 条  |  跳过: {stats['skipped_dup']} 条  |  总计: {stats['total']} 条")

    if not new_rows:
        print("  无新数据。")
        return 0, 0, stats

    if DRY_RUN:
        print(f"\n  ⚠ DRY_RUN 模式: 以上 {len(new_rows)} 条不会实际写入飞书")
        return 0, len(new_rows), stats

    records = parse_data(new_rows, field_mapping, field_types)
    now_ts = int(time.time() * 1000)
    for r in records:
        r["同步时间"] = now_ts

    print(f"  开始写入飞书（{len(records)} 条）...")
    synced = api.insert_records(BITALBE_APP_TOKEN, table_id, records)

    print("=" * 60)
    print(f"  [{label}] 写入完成！成功 {synced}/{len(records)} 条")
    print("=" * 60)
    return synced, len(records), stats


# ============================================
# 解析数据
# ============================================
def parse_data(rows, field_mapping, field_types):
    records = []
    for row in rows:
        record = {}
        for src_col, dst_col in field_mapping.items():
            raw = row.get(src_col, "")
            converted = convert_field(dst_col, raw, field_types)
            if converted is not None:
                record[dst_col] = converted
        if record:
            records.append(record)
    return records


# ============================================
# 主流程
# ============================================
def run_sync():
    check_config()
    api = FeishuAPI(FEISHU_APP_ID, FEISHU_APP_SECRET)

    mode_str = "DRY_RUN (不写入)" if DRY_RUN else "正式写入"
    print(f"\n{'#' * 60}")
    print(f"  腾讯文档 → 飞书 同步 v5.3  |  模式: {mode_str}")
    print(f"  增量策略: 提交时间(回看{BUFFER_MINUTES}分钟) + ID去重")
    print(f"{'#' * 60}")

    s1, t1, st1 = sync_single_table(
        api, "表1-合作资料卡",
        TENCENT_SHEET_ID_1, TABLE_ID_1,
        FIELD_MAPPING_1, FIELD_TYPES_1,
        id_field=ID_FIELD_1,
        time_field=TIME_FIELD_1,
        id_src_col=ID_SRC_COL_1,
    )

    print(f"\n{'#' * 60}")
    print(f"  汇总")
    print(f"  表1: 写入 {s1}/{t1} 条  (跳过 {st1.get('skipped_dup', 0)} 条)")
    if DRY_RUN:
        print(f"\n  ⚠ 以上为 DRY_RUN 分析结果，未实际写入")
        print(f"  ⚠ 确认无误后，将文件顶部 DRY_RUN=True 改为 False，重新运行")
    print(f"{'#' * 60}")


if __name__ == "__main__":
    run_sync()
