"""
sync_v5.py — 腾讯文档 → 飞书多维表格同步（v5.2 智能表适配）
增量策略：提交时间（回看 5 分钟缓冲）+ ID 去重
"""
import os, json, time
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone

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
# 腾讯文档 API（支持智能表 + 普通表）
# ============================================
def fetch_tencent_docs_data(file_id, sheet_id):
    url = f"https://docs.qq.com/dop-api/opendoc?id={file_id}&outformat=1&normal=1&sheet_id={sheet_id}&startrow=0&endrow=10000"
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://docs.qq.com/",
    })
    data = json.loads(urlopen(req, timeout=30).read().decode("utf-8"))
    cv = data.get("clientVars", {}).get("collab_client_vars", {})
    text = cv.get("initialAttributedText", {}).get("text", [])

    if not text:
        raise Exception("腾讯文档返回空数据，请确认链接权限为「获得链接的人可查看」")

    first = text[0]

    # --- 智能表格式 ---
    if "smartsheet" in first:
        return _parse_smartsheet(json.loads(first["smartsheet"]))

    # --- 普通表格格式（旧） ---
    return _parse_regular_table(text)


def _parse_smartsheet(ss):
    """解析智能表 smartsheet 格式"""
    cn_tz = timezone(timedelta(hours=8))

    # 1. 解析列定义 (t=3005)
    cols = {}
    records_out = []

    for batch in ss:
        for item in batch:
            if item.get("t") == 3005:
                raw_cols = item["c"].get("3", {}).get("3", {})
                for fid, info in raw_cols.items():
                    col = {"name": info.get("30", ""), "type": info.get("31", 0)}
                    if col["type"] == 17:  # 单选
                        opts = {}
                        for opt in info.get("17", {}).get("3", []):
                            opts[opt.get("1", "")] = opt.get("2", "")
                        col["options"] = opts
                    cols[fid] = col

            elif item.get("t") == 3028:
                raw_records = item["c"].get("2", {})
                for rid, record in raw_records.items():
                    # 合并所有 fr 的增量修改
                    merged = {}
                    revisions = [(int(k[2:]), v) for k, v in record.items() if k.startswith("fr")]
                    revisions.sort(key=lambda x: x[0])
                    for _, rev in revisions:
                        inner = rev.get("1", {})
                        if isinstance(inner, dict):
                            merged.update(inner)

                    if not merged:
                        continue

                    row = {}
                    for fid, c in cols.items():
                        val = merged.get(fid, {})
                        ctype = c["type"]
                        if not val:
                            row[c["name"]] = ""
                        elif ctype == 1:  # 文本
                            texts = val.get("1", [])
                            row[c["name"]] = texts[0].get("2", "") if texts else ""
                        elif ctype == 2:  # 数字
                            row[c["name"]] = str(val.get("2", ""))
                        elif ctype == 4:  # 日期
                            ts = int(val.get("4", 0))
                            if ts:
                                dt = datetime.fromtimestamp(ts / 1000, cn_tz)
                                row[c["name"]] = dt.strftime("%Y-%m-%d %H:%M:%S")
                            else:
                                row[c["name"]] = ""
                        elif ctype == 17:  # 单选
                            opts = val.get("17", [])
                            row[c["name"]] = cols[fid]["options"].get(opts[0], opts[0]) if opts else ""
                        elif ctype == 6:  # 图片
                            row[c["name"]] = "[图片]"
                        elif ctype == 10:  # 创建人
                            row[c["name"]] = ""
                        elif ctype == 26:  # 货币/数值
                            row[c["name"]] = str(val.get("26", ""))
                        else:
                            row[c["name"]] = str(val) if val else ""
                    records_out.append(row)

    print(f"  智能表: 共 {len(records_out)} 行数据")
    return records_out


def _parse_regular_table(text):
    """解析普通表格 tr/td 格式（旧版兼容）"""
    rows = text
    header_map = {}
    for item in rows:
        if isinstance(item, dict) and item.get("type") == "tr":
            cells = item.get("c", [])
            for cell in cells:
                col = cell.get("c", 0)
                val = cell.get("v", "")
                if isinstance(val, list):
                    val = "".join(str(v.get("v", "") if isinstance(v, dict) else v) for v in val)
                val = str(val).strip()
                if val and val != " ":
                    header_map[col] = val
            break

    all_rows = []
    for item in rows:
        if isinstance(item, dict) and item.get("type") == "tr":
            cells = item.get("c", [])
            row_dict = {}
            for cell in cells:
                col = cell.get("c", 0)
                val = cell.get("v", "")
                if isinstance(val, list):
                    val = "".join(str(v.get("v", "") if isinstance(v, dict) else v) for v in val)
                val = str(val).strip()
                col_name = header_map.get(col)
                if col_name and val:
                    row_dict[col_name] = val
            if row_dict:
                all_rows.append(row_dict)

    if all_rows and list(all_rows[0].values()) == list(header_map.values()):
        all_rows = all_rows[1:]

    print(f"  普通表: 共 {len(all_rows)} 行数据")
    return all_rows


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
# 字段类型转换
# ============================================
def convert_field(feishu_col, raw_value, field_types):
    if feishu_col not in field_types:
        return str(raw_value).strip() if raw_value else ""
    ft = field_types[feishu_col]
    val = str(raw_value).strip() if raw_value else ""
    if not val:
        return None
    if ft == "number":
        try:
            return int(val)
        except ValueError:
            try:
                return float(val)
            except ValueError:
                return None
    elif ft == "datetime":
        # 飞书多维表格 datetime 字段需要毫秒时间戳
        try:
            dt = datetime.strptime(str(val), "%Y-%m-%d %H:%M:%S")
            return int(dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
        except ValueError:
            return None
    elif ft == "url":
        if val.startswith("http"):
            return {"link": val, "text": val}
        return None
    return val


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
    print(f"  腾讯文档 → 飞书 同步 v5.2  |  模式: {mode_str}")
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
