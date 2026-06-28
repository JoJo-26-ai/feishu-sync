# 腾讯文档 → 飞书多维表格 自动同步脚本（GitHub Actions 版）
# v3: 增量同步 + 字段类型自动转换

import json
import os
import time
import re
import base64
import zlib
import csv
import io
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.parse import urlencode


# ============================================
# 配置（从环境变量读取）
# ============================================

TENCENT_FILE_ID = os.environ.get("TENCENT_FILE_ID", "")
TENCENT_SHEET_ID = os.environ.get("TENCENT_SHEET_ID", "ss_mmtejf")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BITALBE_APP_TOKEN = os.environ.get("APP_TOKEN", "")
BITABLE_TABLE_ID = os.environ.get("TABLE_ID", "")

# ============================================
# 测试模式：True 时只拉 5 条验证链路，False 时全量
# ============================================
TEST_MODE = True   # 跑通后改为 False 即可

# ============================================
# 字段映射：腾讯文档列名 → 飞书多维表格列名
# ============================================
FIELD_MAPPING = {
    "创建人": "创建人",
    "提交者（自动）": "提交者（自动）",
    "提交时间（自动）": "提交时间（自动）",
    "合作档期（必填）（必填）": "合作档期（必填）（必填）",
    "返点（必填）（必填）": "返点（必填）（必填）",
    "没问题请签名，有问题联系我哦": "没问题请签名，有问题联系我哦",
    "该号是否可以发Live图？（必填）（必填）": "该号是否可以发Live图？（必填）（必填）",
    "需在本品合作笔记下安排5条正向评论可否接受？（必填）": "需在本品合作笔记下安排5条正向评论可否接受？（必填）",
    "小红书名（必填）（必填）": "小红书名（必填）（必填）",
    "合作后是否可以高配合进行评论区维护？（必填）": "合作后是否可以高配合进行评论区维护？（必填）",
    "小红书ID（必填）（必填）": "小红书ID（必填）（必填）",
    "本品排竞期前后15天是否接受？（必填）（必填）": "本品排竞期前后15天是否接受？（必填）（必填）",
    "合作价格（必填）（必填）": "合作价格（必填）（必填）",
    "宝宝月龄": "宝宝月龄",
}

# ============================================
# 字段类型声明（非文本类型需要声明）
#   number: 飞书数字字段
#   url:    飞书链接字段
#   text:   默认，无需声明
# ============================================
FIELD_TYPES = {
    "合作价格（必填）（必填）": "number",
    "返点（必填）（必填）": "number",
}


def check_config():
    missing = []
    checks = [
        ("TENCENT_FILE_ID", TENCENT_FILE_ID, "腾讯文档文件 ID"),
        ("FEISHU_APP_ID", FEISHU_APP_ID, "飞书 App ID"),
        ("FEISHU_APP_SECRET", FEISHU_APP_SECRET, "飞书 App Secret"),
        ("APP_TOKEN", BITALBE_APP_TOKEN, "飞书 app_token"),
        ("TABLE_ID", BITABLE_TABLE_ID, "飞书 table_id"),
    ]
    for key, val, desc in checks:
        if not val:
            missing.append(f"  {key}: {desc}")
    if missing:
        raise Exception("以下环境变量未设置：\n" + "\n".join(missing))


# ============================================
# 腾讯文档数据获取（公开接口）
# ============================================

def fetch_tencent_docs_data(file_id):
    """使用 dop-api/opendoc 获取智能表格全部数据"""
    print(f"[{datetime.now():%H:%M:%S}] 正在从腾讯文档读取数据...")

    sheet_id = TENCENT_SHEET_ID
    url = (
        f"https://docs.qq.com/dop-api/opendoc"
        f"?tab={sheet_id}&u=&noEscape=1"
        f"&enableSmartsheetSplit=1&supportOptimizedVer=2"
        f"&startrow=0&endrow=2000"
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
        if "k4" in cell:
            return fmt_ts(cell["k4"])
        if "k6" in cell:
            return ""
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

def convert_field(feishu_col_name, value):
    ft = FIELD_TYPES.get(feishu_col_name, "text")
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
        if len(v) < 8:  # 最短合法 URL: http://a.b
            return None
        # 已经完整的 URL
        if v.lower().startswith("http://") or v.lower().startswith("https://"):
            if "." in v[8:]:  # 必须有域名
                return v
            return None
        # 补全协议头
        if re.search(r'[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,}', v):
            return "https://" + v
        return None
    if value == "" or value is None:
        return None
    return str(value)


# ============================================
# 数据解析
# ============================================

def parse_data(all_rows, field_mapping):
    records = []
    for row in all_rows:
        record = {}
        for src_col, dst_col in field_mapping.items():
            raw = row.get(src_col, "")
            converted = convert_field(dst_col, raw)
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
        """获取飞书表格中「提交时间」最近的一条记录（不依赖排序，手动遍历找最大值）"""
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


# ============================================
# 同步逻辑
# ============================================

BATCH_SIZE = 500  # 飞书批量写入单次上限

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
            # 降级为逐条重试
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
    """只保留 提交时间 > since_time 的行"""
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


def run_sync():
    check_config()
    print("=" * 50)
    print(f"腾讯文档 → 飞书多维表格 同步开始")
    print(f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 50)

    all_rows = fetch_tencent_docs_data(TENCENT_FILE_ID)

    # 测试模式：只保留前 5 条
    if TEST_MODE:
        all_rows = all_rows[:5]
        print(f"  [测试模式] 仅处理前 {len(all_rows)} 条数据")
        print("  跑通后把脚本顶部 TEST_MODE 改为 False 即可全量")

    api = FeishuAPI(FEISHU_APP_ID, FEISHU_APP_SECRET)
    latest_in_feishu = api.get_latest_submit_time(BITALBE_APP_TOKEN, BITABLE_TABLE_ID)

    if latest_in_feishu:
        print(f"  飞书最新记录时间: {latest_in_feishu}")
        new_rows = filter_new_records(all_rows, latest_in_feishu)
        print(f"  增量模式: {len(new_rows)} / {len(all_rows)} 条待写入")
    else:
        new_rows = all_rows
        print(f"  全量模式: {len(new_rows)} 条待写入")

    if not new_rows:
        print("无新数据，结束。")
        return 0, len(all_rows)

    records = parse_data(new_rows, FIELD_MAPPING)
    print(f"开始写入飞书（{len(records)} 条）...")
    synced = insert_records(api, BITALBE_APP_TOKEN, BITABLE_TABLE_ID, records)

    print("=" * 50)
    print(f"同步完成！写入 {synced}/{len(records)} 条")
    print("=" * 50)
    return synced, len(records)


if __name__ == "__main__":
    synced, total = run_sync()
    print(f"\n::notice:: 同步 {synced}/{total} 条")
