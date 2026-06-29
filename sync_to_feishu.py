# 腾讯文档 → 飞书多维表格 自动同步脚本（GitHub Actions 版）
# v4: 双表同步

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
# 配置（从环境变量读取）
# ============================================

TENCENT_FILE_ID = os.environ.get("TENCENT_FILE_ID", "")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BITALBE_APP_TOKEN = os.environ.get("APP_TOKEN", "")

# ============================================
# 测试模式：True 时只拉 5 条验证链路，False 时全量
# ============================================
TEST_MODE = False

# ============================================
# 表1: 合作资料卡
# ============================================
TENCENT_SHEET_ID_1 = os.environ.get("TENCENT_SHEET_ID", "ss_mmtejf")
TABLE_ID_1 = os.environ.get("TABLE_ID", "")

FIELD_MAPPING_1 = {
    "提交者（自动）": "提交者（自动）",
    "提交时间（自动）": "提交时间（自动）",
    "合作档期（必填）（必填）": "合作档期",
    "返点（必填）（必填）": "返点",
    "该号是否可以发Live图？（必填）（必填）": "该号是否可以发Live图？",
    "需在本品合作笔记下安排5条正向评论可否接受？（必填）（必填）": "需在本品合作笔记下安排5条正向评论可否接受？",
    "小红书名（必填）（必填）": "小红书昵称",
    "合作后是否可以高配合进行评论区维护？（必填）（必填）": "合作后是否可以高配合进行评论区维护？",
    "小红书ID（必填）（必填）": "小红书ID",
    "本品排竞期前后15天是否接受？（必填）（必填）": "本品排竞期前后15天是否接受？",
    "合作价格（必填）（必填）": "合作价格",
    "宝宝月龄": "宝宝月龄",
    "重复标注": "重复标注",
}

FIELD_TYPES_1 = {
    "合作价格": "number",
    "返点": "number",
    "提交时间（自动）": "datetime",
    "合作档期": "datetime",
}

# ============================================
# 表2: 蒲公英数据源 → 博主信息（腾讯文档）
# ============================================
TENCENT_SHEET_ID_2 = os.environ.get("TENCENT_SHEET_ID_2", "tGdOD3")
TABLE_ID_2 = os.environ.get("TABLE_ID_2", "")

FIELD_MAPPING_2 = {
    "博主名称": "博主名称",
    "蒲公英链接": "蒲公英链接",
    "小红书号": "小红书号",
    "粉丝数": "粉丝数",
    "赞藏数": "赞藏数",
    "图文报价": "图文报价",
    "视频报价": "视频报价",
}

FIELD_TYPES_2 = {
    "图文报价": "number",
    "视频报价": "number",
    "蒲公英链接": "url",
}


def check_config():
    missing = []
    checks = [
        ("TENCENT_FILE_ID", TENCENT_FILE_ID, "腾讯文档文件 ID"),
        ("FEISHU_APP_ID", FEISHU_APP_ID, "飞书 App ID"),
        ("FEISHU_APP_SECRET", FEISHU_APP_SECRET, "飞书 App Secret"),
        ("APP_TOKEN", BITALBE_APP_TOKEN, "飞书 app_token"),
        ("TABLE_ID (表1)", TABLE_ID_1, "飞书 table_id 表1"),
        ("TABLE_ID_2 (表2)", TABLE_ID_2, "飞书 table_id 表2"),
    ]
    for key, val, desc in checks:
        if not val:
            missing.append(f"  {key}: {desc}")
    if missing:
        raise Exception("以下环境变量未设置：\n" + "\n".join(missing))


# ============================================
# 腾讯文档数据获取（公开接口）
# ============================================

def fetch_tencent_docs_data(file_id, sheet_id, track_k32_fields=None):
    """使用 dop-api/opendoc 获取智能表格全部数据
    track_k32_fields: 需追踪最后修改时间的字段名集合，每条记录会附加 _max_k32"""

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
    # 构建需追踪 k32 的字段 ID 集合
    tracked_fids = set()
    if track_k32_fields:
        for fid, fconf in field_defs.items():
            if field_names.get(fid, "") in track_k32_fields:
                tracked_fids.add(fid)
    all_rows = []
    for _, row_val in records_data.items():
        row = {}
        cells = row_val.get("k1", {})
        for fid in field_order:
            col_name = field_names[fid]
            raw = extract_value(cells.get(fid, {}))
            raw = resolve_opt(fid, raw)
            row[col_name] = raw
        # 计算关键字段最大 k32 时间戳
        if track_k32_fields:
            max_k32 = 0
            for fid in tracked_fids:
                cell = cells.get(fid, {})
                k32 = cell.get("k32", "0")
                try:
                    k32_int = int(k32)
                    if k32_int > max_k32:
                        max_k32 = k32_int
                except (ValueError, TypeError):
                    pass
            row["_max_k32"] = max_k32
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
                return v
            return None
        if re.search(r'[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,}', v):
            return "https://" + v
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
# 数据解析
# ============================================

def parse_data(all_rows, field_mapping, field_types):
    records = []
    for row in all_rows:
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
# 飞书 API
# ============================================

class FeishuAPI:
    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        self.tenant_token = None
        self.token_expire = 0

    def _get_token(self):
        if self.tenant_token and time.time() < self.token_expire - 60:
            return self.tenant_token
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        body = {"app_id": self.app_id, "app_secret": self.app_secret}
        data = json.dumps(body).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = json.loads(urlopen(req, timeout=15).read().decode("utf-8"))
        if resp.get("code") != 0:
            raise Exception(f"飞书 Token 获取失败: {resp.get('msg')}")
        self.tenant_token = resp["tenant_access_token"]
        self.token_expire = time.time() + resp.get("expire", 7200)
        return self.tenant_token

    def request(self, method, url, body=None, params=None, timeout=30):
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

    def get_latest_submit_time(self, app_token, table_id):
        """获取飞书表格中「提交时间（自动）」最近的一条记录"""
        try:
            params = {"page_size": 500}
            data = self.request(
                "GET",
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                params=params,
                timeout=20,
            )
            items = data.get("items", [])
            if not items:
                print("  飞书表格为空，使用全量模式")
                return None

            latest = None
            for item in items:
                ts_val = item.get("fields", {}).get("提交时间（自动）", "")
                if not ts_val:
                    continue
                try:
                    # 飞书返回的可能是 13 位毫秒时间戳
                    if isinstance(ts_val, (int, float)) or (isinstance(ts_val, str) and ts_val.isdigit()):
                        ts_int = int(ts_val)
                        if ts_int > 1000000000000:  # 毫秒级
                            t = datetime.fromtimestamp(ts_int / 1000, tz=timezone(timedelta(hours=8)))
                        else:  # 秒级
                            t = datetime.fromtimestamp(ts_int, tz=timezone(timedelta(hours=8)))
                    else:
                        # 尝试解析字符串格式
                        t = datetime.strptime(str(ts_val), "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=timezone(timedelta(hours=8))
                        )
                    if latest is None or t > latest:
                        latest = t
                except ValueError:
                    pass

            if latest:
                print(f"  飞书最新记录时间: {latest}")
                return latest
            print("  未找到有效提交时间，使用全量模式")
            return None
        except Exception as e:
            print(f"  查询飞书最新记录失败: {e}（将使用全量模式）")
        return None

    def get_max_sync_time(self, app_token, table_id):
        """获取飞书表格中「同步时间」的最大值"""
        max_ts = 0
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
            for item in data.get("items", []):
                ts = item.get("fields", {}).get("同步时间", 0)
                try:
                    ts = int(ts)
                    if ts > max_ts:
                        max_ts = ts
                except (ValueError, TypeError):
                    pass
            if not data.get("has_more"):
                break
            page_token = data.get("page_token", "")
            if not page_token:
                break
        return max_ts

    def get_existing_data(self, app_token, table_id, fields, key_field="小红书号"):
        """获取飞书已有数据，返回 (去重集合, 值→标注信息映射)
        key_field: 作为记录唯一标识的字段名（表1用"小红书ID"，表2用"小红书号"）"""
        existing_ids = set()
        value_lookup = {}  # field_value → [(key_val, field_name), ...]
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
            for item in data.get("items", []):
                item_fields = item.get("fields", {})
                key_val = str(item_fields.get(key_field, "")).strip()
                if not key_val:
                    continue
                existing_ids.add(key_val)
                for f in fields:
                    val = str(item_fields.get(f, "")).strip()
                    if val:
                        if val not in value_lookup:
                            value_lookup[val] = []
                        # 避免同一记录重复记录
                        existing_pairs = value_lookup[val]
                        if not any(p[0] == key_val and p[1] == f for p in existing_pairs):
                            existing_pairs.append((key_val, f))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token", "")
            if not page_token:
                break
        return existing_ids, value_lookup


# ============================================
# 同步逻辑
# ============================================

BATCH_SIZE = 500

def insert_records(api, app_token, table_id, records):
    """批量写入，每次最多 500 条"""
    success = 0
    total = len(records)
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"

    for batch_start in range(0, total, BATCH_SIZE):
        batch = records[batch_start:batch_start + BATCH_SIZE]
        batch_end = min(batch_start + len(batch), total)
        try:
            body = {"records": [{"fields": r} for r in batch]}
            api.request("POST", url, body=body, timeout=60)
            success += len(batch)
            print(f"  进度: {success}/{total}")
        except Exception as e:
            print(f"  批量写入失败 [{batch_start+1}-{batch_end}]: {e}")
            for i, record in enumerate(batch):
                try:
                    single_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
                    api.request("POST", single_url, body={"fields": record})
                    success += 1
                except Exception as e2:
                    print(f"  写入失败 [{batch_start + i + 1}/{total}]: {e2}")
        time.sleep(0.5)
    return success


def filter_new_records(all_rows, since_time):
    """表1专用：只保留 提交时间 > since_time 的行"""
    if since_time is None:
        return all_rows
    new_rows = []
    for row in all_rows:
        ts_str = row.get("提交时间（自动）", "")
        try:
            row_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            row_time = row_time.replace(tzinfo=timezone(timedelta(hours=8)))
            if row_time > since_time:
                new_rows.append(row)
        except (ValueError, TypeError):
            new_rows.append(row)
    return new_rows


def run_sync_table(api, label, sheet_id, table_id, field_mapping, field_types, use_incremental, track_k32_fields=None):
    """同步单个表"""
    print()
    print("=" * 50)
    print(f"[{label}] 同步开始")
    print(f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 50)

    all_rows = fetch_tencent_docs_data(TENCENT_FILE_ID, sheet_id, track_k32_fields=track_k32_fields)

    if TEST_MODE:
        all_rows = all_rows[:5]
        print(f"  [测试模式] 仅处理前 {len(all_rows)} 条数据")

    if use_incremental:
        # 双重增量检测：k32（单元格修改时间）+ 提交时间
        max_sync_ts = api.get_max_sync_time(BITALBE_APP_TOKEN, table_id)
        latest_submit = api.get_latest_submit_time(BITALBE_APP_TOKEN, table_id)
        if max_sync_ts or latest_submit:
            new_rows = []
            k32_count = 0
            submit_count = 0
            for row in all_rows:
                k32_pass = row.get("_max_k32", 0) > max_sync_ts
                if k32_pass:
                    k32_count += 1
                submit_pass = False
                if latest_submit:
                    ts_str = row.get("提交时间（自动）", "")
                    try:
                        row_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        row_time = row_time.replace(tzinfo=timezone(timedelta(hours=8)))
                        if row_time > latest_submit:
                            submit_pass = True
                            submit_count += 1
                    except (ValueError, TypeError):
                        pass
                if k32_pass or submit_pass:
                    new_rows.append(row)
            print(f"  增量模式: {len(new_rows)} / {len(all_rows)} 条待写入 (k32: {k32_count}, 提交时间: {submit_count})")
        else:
            new_rows = all_rows
            print(f"  全量模式: {len(new_rows)} 条待写入")
        # 表1查重：小红书昵称 + 小红书ID
        # 左边是腾讯文档列名（用于从行数据取值），右边是飞书列名（用于查飞书已有记录）
        check_mapping_1 = {
            "小红书名（必填）（必填）": "小红书昵称",
            "小红书ID（必填）（必填）": "小红书ID",
        }
        feishu_fields = list(check_mapping_1.values())
        print(f"  正在查询飞书已有记录（查重）...")
        _, value_lookup_1 = api.get_existing_data(BITALBE_APP_TOKEN, table_id, feishu_fields, key_field="小红书ID")
        dup_count = 0
        for row in new_rows:
            annotations = []
            for src_col, feishu_col in check_mapping_1.items():
                val = str(row.get(src_col, "")).strip()
                if val and val in value_lookup_1:
                    matches = value_lookup_1[val]
                    for match_key, match_field in matches:
                        annotations.append((feishu_col, match_key))
            if annotations:
                merged = {}
                for col, mk in annotations:
                    merged.setdefault(col, set()).add(mk)
                parts = []
                for col in feishu_fields:
                    if col in merged:
                        parts.append(f"{col}→{','.join(sorted(merged[col]))}")
                row["重复标注"] = "；".join(parts)
                dup_count += 1
        if dup_count:
            print(f"  标注重复 {dup_count} 条（详见飞书「重复标注」列）")
    else:
        # 表2：k32 增量过滤 + 小红书号去重 + 核心字段查重标注
        check_fields = ["博主名称", "蒲公英链接", "小红书号"]
        # 第一步：基于 k32 做增量过滤
        max_sync_ts = api.get_max_sync_time(BITALBE_APP_TOKEN, table_id)
        if max_sync_ts:
            tz_cn = timezone(timedelta(hours=8))
            sync_dt = datetime.fromtimestamp(max_sync_ts / 1000, tz_cn).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  飞书最大同步时间: {sync_dt}")
            candidates = []
            for row in all_rows:
                if row.get("_max_k32", 0) > max_sync_ts:
                    candidates.append(row)
            print(f"  k32 增量过滤: {len(candidates)} / {len(all_rows)} 条候选")
        else:
            candidates = all_rows
            print(f"  飞书无同步记录，全量模式: {len(candidates)} 条")
        # 第二步：小红书号去重 + 查重标注
        print(f"  正在查询飞书已有记录...")
        existing_ids, value_lookup = api.get_existing_data(BITALBE_APP_TOKEN, table_id, check_fields)
        print(f"  飞书已有 {len(existing_ids)} 条记录")
        new_rows = []
        dup_annotated = 0
        for row in candidates:
            xhs_id = str(row.get("小红书号", "")).strip()
            # 全列查重标注（含小红书号自身匹配）
            annotations = []
            if xhs_id and xhs_id in existing_ids:
                # 小红书号已存在，也写入但做"已有"标记
                annotations.append(("小红书号", xhs_id))
            for f in check_fields:
                val = str(row.get(f, "")).strip()
                if val and val in value_lookup:
                    matches = value_lookup[val]
                    for match_xhs, match_field in matches:
                        if match_xhs != xhs_id:
                            annotations.append((f, match_xhs))
            if annotations:
                merged = {}
                for col, mxhs in annotations:
                    merged.setdefault(col, set()).add(mxhs)
                parts = []
                for col in check_fields:
                    if col in merged:
                        parts.append(f"{col}→{','.join(sorted(merged[col]))}")
                row["重复标注"] = "；".join(parts)
                dup_annotated += 1
            new_rows.append(row)
        if dup_annotated:
            print(f"  标注重复 {dup_annotated} 条（详见飞书「重复标注」列）")
        print(f"  待写入 {len(new_rows)} 条")

    if not new_rows:
        print("无新数据，结束。")
        return 0, len(all_rows)

    records = parse_data(new_rows, field_mapping, field_types)
    now_ts = int(time.time() * 1000)
    for i, r in enumerate(records):
        r["同步时间"] = now_ts
        # 重复标注（所有记录统一带该字段，无重复则为空）
        annotation = new_rows[i].get("重复标注", "") if i < len(new_rows) else ""
        if annotation:
            r["重复标注"] = annotation
    print(f"  开始写入飞书（{len(records)} 条）...")
    synced = insert_records(api, BITALBE_APP_TOKEN, table_id, records)

    print("=" * 50)
    print(f"[{label}] 同步完成！写入 {synced}/{len(records)} 条")
    print("=" * 50)
    return synced, len(records)


def run_sync():
    check_config()
    api = FeishuAPI(FEISHU_APP_ID, FEISHU_APP_SECRET)

    # 表1: 合作资料卡（增量模式 + k32 检测）
    s1, t1 = run_sync_table(
        api, "表1-合作资料卡",
        TENCENT_SHEET_ID_1, TABLE_ID_1,
        FIELD_MAPPING_1, FIELD_TYPES_1,
        use_incremental=True,
        track_k32_fields=set(FIELD_MAPPING_1.keys()),
    )

    # 表2: 蒲公英数据源（全量去重模式）
    s2, t2 = run_sync_table(
        api, "表2-博主信息",
        TENCENT_SHEET_ID_2, TABLE_ID_2,
        FIELD_MAPPING_2, FIELD_TYPES_2,
        use_incremental=False,
        track_k32_fields=set(FIELD_MAPPING_2.keys()),
    )

    print(f"\n::notice:: 表1 同步 {s1}/{t1} 条，表2 同步 {s2}/{t2} 条")


if __name__ == "__main__":
    run_sync()
