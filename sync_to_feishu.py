# 腾讯文档 → 飞书多维表格 自动同步脚本（GitHub Actions 版）
# v2: 使用 dop-api/opendoc 公开接口，无需 TENCENT_ACCESS_TOKEN

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
TENCENT_SHEET_ID = os.environ.get("TENCENT_SHEET_ID", "ss_zc8fjj")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BITALBE_APP_TOKEN = os.environ.get("APP_TOKEN", "")
BITABLE_TABLE_ID = os.environ.get("TABLE_ID", "")

# ============================================
# ⬇️ 字段映射：腾讯文档列名 → 飞书多维表格列名
# 请根据实际 Feishu 表格列名修改右侧值
# ============================================
FIELD_MAPPING = {
    "提交时间（自动）": "提交时间",
    "小红书ID（必填）": "小红书ID",
    "小红书名字（必填）": "博主名称",
    "合作价格（必填）": "合作价格",
    "返点（必填）": "返点",
    "状态": "状态",
    "合作形式": "合作形式",
    "合作档期（必填）": "合作档期",
    "计算返点": "计算返点",
    "计算报价": "计算报价",
    "粉丝数": "粉丝数",
    "赞藏数": "赞藏数",
    "视频报价": "视频报价",
    "图文报价": "图文报价",
    "蒲公英链接": "蒲公英链接",
    "宝宝月龄": "宝宝月龄",
    "博主ID": "博主ID",
    "订单状态": "订单状态",
    "需在本品合作笔记下安排5条正向评论可否接受？（必填）": "是否评论",
    "合作后是否可以高配合进行评论区维护？（必填）": "是否维护",
    "该号是否可以发Live图？（必填）": "是否Live图",
    "本品排竞期前后15天是否接受？（必填）": "是否排竞",
    "提交者（自动）": "提交者",
    "备注": "备注",
    "备注1": "备注1",
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
# 腾讯文档数据获取（v2: 公开接口，无需 Token）
# ============================================

def fetch_tencent_docs_data(file_id):
    """使用 dop-api/opendoc 公开接口获取智能表格所有数据，返回 CSV 字符串"""
    print(f"[{datetime.now():%H:%M:%S}] 正在从腾讯文档读取数据...")

    sheet_id = TENCENT_SHEET_ID
    url = (
        f"https://docs.qq.com/dop-api/opendoc"
        f"?tab={sheet_id}&u=&noEscape=1"
        f"&enableSmartsheetSplit=1&supportOptimizedVer=2"
        f"&startrow=0&endrow=2000"  # 一次拉取全部行
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
    # 修复 base64 padding
    b64_padded = b64 + "=" * (4 - len(b64) % 4) if len(b64) % 4 else b64
    raw = base64.b64decode(b64_padded)
    decompressed = zlib.decompress(raw)
    smartsheet = json.loads(decompressed.decode("utf-8"))

    # 字段配置
    config = smartsheet[0][0]
    field_defs = config["c"]["k3"]["k3"]
    records_data = smartsheet[0][1]["c"]["k2"]["k1"]

    # 构建字段列表和选项映射
    field_order = []
    field_names = {}
    opt_maps = {}

    for fid, fconf in field_defs.items():
        name = fconf.get("k30", fid)
        field_order.append(fid)
        field_names[fid] = name

        # 单选/多选选项映射
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

    # 辅助函数
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
            return "[图片]"
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

    # 提取所有行
    print(f"  解析 {len(records_data)} 行数据...")
    all_rows = []
    for _, row_val in records_data.items():
        row = {}
        cells = row_val.get("k1", {})
        for fid in field_order:
            col_name = field_names[fid]
            if fid in cells:
                raw = extract_value(cells[fid])
                raw = resolve_opt(fid, raw)
                row[col_name] = raw
            else:
                row[col_name] = ""
        all_rows.append(row)

    # 输出 CSV
    output = io.StringIO()
    headers = [field_names[fid] for fid in field_order]
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerows(all_rows)
    csv_str = output.getvalue()

    print(f"  ✓ 提取完成，{len(all_rows)} 行 {len(headers)} 列")
    return csv_str


# ============================================
# 数据解析
# ============================================

def parse_data(raw_text, field_mapping):
    """解析 CSV 字符串，按 field_mapping 提取并重命名字段"""
    reader = csv.DictReader(io.StringIO(raw_text))
    records = []
    for row in reader:
        record = {}
        for src_col, dst_col in field_mapping.items():
            val = row.get(src_col, "").strip()
            if val:
                record[dst_col] = val
        if any(v for v in record.values()):
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
        resp = json.loads(urlopen(req).read().decode("utf-8"))
        if resp.get("code") != 0:
            raise Exception(f"飞书 Token 获取失败: {resp.get('msg')}")
        self.tenant_token = resp["tenant_access_token"]
        self.token_expire = time.time() + resp.get("expire", 7200)
        return self.tenant_token

    def request(self, method, url, body=None, params=None):
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
        resp = json.loads(urlopen(req).read().decode("utf-8"))
        if resp.get("code") != 0:
            raise Exception(f"飞书 API 错误: {resp.get('msg')}")
        return resp.get("data", resp)


# ============================================
# 同步逻辑
# ============================================

def insert_records(api, app_token, table_id, records):
    success = 0
    total = len(records)
    for i, record in enumerate(records, 1):
        try:
            url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
            body = {"fields": {k: v for k, v in record.items() if v}}
            api.request("POST", url, body=body)
            success += 1
            if i % 10 == 0:
                print(f"  进度: {i}/{total}")
            time.sleep(0.12)
        except Exception as e:
            print(f"  写入失败 [{i}/{total}]: {e}")
    return success


def run_sync():
    check_config()
    print("=" * 50)
    print(f"腾讯文档 → 飞书多维表格 同步开始")
    print(f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 50)

    raw_data = fetch_tencent_docs_data(TENCENT_FILE_ID)
    records = parse_data(raw_data, FIELD_MAPPING)
    print(f"解析到 {len(records)} 条数据")

    if not records:
        print("无数据，结束。")
        return 0, 0

    print("开始写入飞书...")
    api = FeishuAPI(FEISHU_APP_ID, FEISHU_APP_SECRET)
    synced = insert_records(api, BITALBE_APP_TOKEN, BITABLE_TABLE_ID, records)

    print("=" * 50)
    print(f"同步完成！写入 {synced}/{len(records)} 条")
    print("=" * 50)
    return synced, len(records)


if __name__ == "__main__":
    synced, total = run_sync()
    print(f"\n::notice:: 同步 {synced}/{total} 条")
